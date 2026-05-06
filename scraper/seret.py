import difflib
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Iterable, Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE = "https://www.seret.co.il"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
}
# Seconds between requests to avoid hammering the server.
# Safe to drop below 1.0 because we only hit seret for the ~25 films
# Planet is actually showing (plus a small buffer), not the full 120+.
REQUEST_DELAY = 0.5

# Per-film detail cache: 12h TTL. Film metadata (synopsis, cast, runtime)
# rarely changes, so this lets the 2nd and 3rd daily scrapes be near-instant.
CACHE_DIR = Path(__file__).parent.parent / "data" / "seret_cache"
CACHE_TTL_SEC = 12 * 3600


@dataclass
class SeretMovie:
    seret_id: int
    title_he: str
    title_en: str
    description: str
    genres: list[str]
    content_rating: str
    release_date: str
    runtime: str
    imdb_id: Optional[str]
    imdb_score: Optional[float]
    seret_score: Optional[float]
    poster_url: Optional[str]
    detail_url: str
    directors: list[str] = field(default_factory=list)
    actors: list[str] = field(default_factory=list)
    language: str = ""


def _get(url: str, **kwargs) -> Optional[BeautifulSoup]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=15, **kwargs)
        r.encoding = r.apparent_encoding or "windows-1255"
        r.raise_for_status()
        return BeautifulSoup(r.text, "lxml")
    except Exception as e:
        logger.warning("GET %s failed: %s", url, e)
        return None


def _extract_movie_ids(soup: BeautifulSoup) -> list[int]:
    ids = set()
    for a in soup.select("a[href*='s_movies.asp?MID=']"):
        m = re.search(r"MID=(\d+)", a["href"])
        if m:
            ids.add(int(m.group(1)))
    return list(ids)


def _extract_title_map(soup: BeautifulSoup) -> dict[int, str]:
    """
    Map each MID to its visible title from a discovery page.
    seret's discovery pages list films as <a href="s_movies.asp?MID=N">Title</a>,
    so we grab the anchor text. Multiple anchors for the same film exist —
    we keep the longest (most informative) one.
    """
    out: dict[int, str] = {}
    for a in soup.select("a[href*='s_movies.asp?MID=']"):
        m = re.search(r"MID=(\d+)", a["href"])
        if not m:
            continue
        mid = int(m.group(1))
        text = a.get_text(strip=True)
        if text and len(text) > len(out.get(mid, "")):
            out[mid] = text
    return out


# ---------------------------------------------------------------------------
# Detail-page cache (12h TTL)
# ---------------------------------------------------------------------------

def _cache_path(mid: int) -> Path:
    return CACHE_DIR / f"{mid}.json"


def _cache_read(mid: int) -> Optional["SeretMovie"]:
    p = _cache_path(mid)
    if not p.exists():
        return None
    if time.time() - p.stat().st_mtime > CACHE_TTL_SEC:
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return SeretMovie(**data)
    except Exception as e:
        logger.debug("seret cache read failed for MID=%d: %s", mid, e)
        return None


def _cache_write(movie: "SeretMovie") -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(movie.seret_id).write_text(
            json.dumps(asdict(movie), ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        logger.debug("seret cache write failed for MID=%d: %s", movie.seret_id, e)


def _normalize_title(t: str) -> str:
    """Lowercase + strip punctuation + collapse whitespace. Keeps Hebrew chars."""
    t = (t or "").lower()
    t = re.sub(r"[^\w\s\u0590-\u05FF]", " ", t)
    t = re.sub(r"\b(the|a|an)\b", "", t)
    return re.sub(r"\s+", " ", t).strip()


def _filter_mids_by_planet_titles(
    title_map: dict[int, str],
    planet_titles: Iterable[str],
    cutoff: float = 0.75,
) -> set[int]:
    """
    Return the subset of MIDs whose title matches any Planet title (fuzzy).
    Keeps the scrape small — we only need detail pages for films Planet
    is actually showing. Cutoff is looser than merger.py's match cutoff
    (0.85) on purpose: false positives here just mean one extra fetch,
    whereas false negatives mean a missed film.
    """
    planet_norm = [_normalize_title(t) for t in planet_titles if t]
    planet_norm = [t for t in planet_norm if t]
    if not planet_norm:
        return set(title_map.keys())  # no filter → scrape everything (legacy behavior)

    keep: set[int] = set()
    for mid, title in title_map.items():
        q = _normalize_title(title)
        if not q:
            continue
        # Fast path: substring either way
        if any(q == p or q in p or p in q for p in planet_norm):
            keep.add(mid)
            continue
        # Fuzzy path
        if difflib.get_close_matches(q, planet_norm, n=1, cutoff=cutoff):
            keep.add(mid)
    return keep


def _extract_poster(soup: BeautifulSoup) -> Optional[str]:
    # Primary: og:image — the only source reliably tied to THIS film
    og = soup.find("meta", attrs={"property": "og:image"})
    if og and og.get("content"):
        src = og["content"]
        return src if src.startswith("http") else BASE + src.replace("../", "/")

    # Fallback: video poster attribute (can point to unrelated film!)
    video = soup.select_one("video#seretPlayer[poster]")
    if video and video.get("poster"):
        poster = video["poster"]
        return poster if poster.startswith("http") else BASE + poster

    # Last resort: first lazy-loaded image that references /images/movies/
    for img in soup.select("img.lazyload[data-src]"):
        src = img.get("data-src", "")
        if "/images/movies/" in src:
            src = src.replace("../", "/")
            return BASE + src if not src.startswith("http") else src

    return None


def _find_jsonld_movie(soup: BeautifulSoup) -> Optional[dict]:
    """Locate the schema.org Movie node inside any <script type='application/ld+json'>.

    Seret migrated all film metadata from inline `itemprop="…"` markup into
    a single JSON-LD `@graph` blob. This is the authoritative source going
    forward — it carries title (he+en), description, genres, directors,
    actors, datePublished, image, sameAs (IMDb URL), and seret's composite
    score in additionalProperty.
    """
    for s in soup.find_all("script", type="application/ld+json"):
        body = s.string or s.get_text() or ""
        if not body.strip():
            continue
        try:
            data = json.loads(body)
        except Exception:
            continue
        # Bare Movie object
        if isinstance(data, dict) and data.get("@type") == "Movie":
            return data
        # @graph array
        graph = data.get("@graph") if isinstance(data, dict) else None
        if isinstance(graph, list):
            for node in graph:
                if isinstance(node, dict) and node.get("@type") == "Movie":
                    return node
    return None


def _people_names(field) -> list[str]:
    """Normalize a JSON-LD person field — list of strings or {name: ...} dicts."""
    out: list[str] = []
    if not field:
        return out
    if isinstance(field, (str, dict)):
        field = [field]
    if not isinstance(field, list):
        return out
    for item in field:
        if isinstance(item, str):
            n = item.strip()
        elif isinstance(item, dict):
            n = (item.get("name") or "").strip()
        else:
            continue
        if n and n not in out:
            out.append(n)
    return out


def _parse_detail(mid: int) -> Optional[SeretMovie]:
    url = f"{BASE}/movies/s_movies.asp?MID={mid}"
    soup = _get(url)
    if not soup:
        return None

    movie = _find_jsonld_movie(soup)
    if not movie:
        # Page rendered but no JSON-LD Movie node — either a deleted/invalid
        # MID or seret changed their structured-data layout. Visible in logs
        # so we notice if the latter happens.
        logger.warning("seret MID=%d: no JSON-LD Movie node found", mid)
        return None

    title_he = (movie.get("name") or "").strip()
    title_en = (movie.get("alternateName") or "").strip()
    if not title_he and not title_en:
        return None  # malformed / placeholder page

    description = (movie.get("description") or "").strip()

    genres: list[str] = []
    g_field = movie.get("genre")
    if isinstance(g_field, list):
        genres = [g.strip() for g in g_field if isinstance(g, str) and g.strip()]
    elif isinstance(g_field, str) and g_field.strip():
        genres = [g_field.strip()]

    release_date = (movie.get("datePublished") or movie.get("dateCreated") or "").strip()

    # IMDb ID — JSON-LD lists external links in `sameAs`. Pick the IMDb one.
    imdb_id: Optional[str] = None
    same_as = movie.get("sameAs") or []
    if isinstance(same_as, str):
        same_as = [same_as]
    if isinstance(same_as, list):
        for ref in same_as:
            if isinstance(ref, str):
                m = re.search(r"imdb\.com/title/(tt\d+)", ref)
                if m:
                    imdb_id = m.group(1)
                    break

    # IMDb score: NEVER from seret. Real IMDb scores live in the public
    # IMDb ratings dataset, keyed by imdb_id (see scraper/imdb_scores.py).
    imdb_score: Optional[float] = None

    # Seret's composite editorial score lives in additionalProperty[].
    seret_score: Optional[float] = None
    for prop in (movie.get("additionalProperty") or []):
        if not isinstance(prop, dict):
            continue
        name = (prop.get("name") or "").lower()
        if "seret score" in name:
            try:
                val = float(prop.get("value"))
                if 0 < val <= 10:
                    seret_score = val
            except (TypeError, ValueError):
                pass
            break

    # Poster: JSON-LD `image` (string | dict | list). Fall back to og:image
    # / video poster via the existing extractor.
    poster_url: Optional[str] = None
    img = movie.get("image")
    if isinstance(img, str):
        poster_url = img
    elif isinstance(img, dict):
        poster_url = img.get("url") or img.get("contentUrl")
    elif isinstance(img, list) and img:
        first = img[0]
        if isinstance(first, str):
            poster_url = first
        elif isinstance(first, dict):
            poster_url = first.get("url") or first.get("contentUrl")
    if not poster_url:
        poster_url = _extract_poster(soup)

    directors = _people_names(movie.get("director"))
    actors = _people_names(movie.get("actor"))[:6]

    # ── Fields not currently in JSON-LD: best-effort scrape of the rendered HTML.
    content_rating = ""
    runtime = ""
    language = ""

    # Runtime: "X דקות" appears in the rendered metadata strip.
    for tag_text in soup.find_all(string=re.compile(r"\d+\s*דקות")):
        m = re.search(r"(\d+)\s*דקות", tag_text)
        if m:
            runtime = f"{m.group(1)} min"
            break

    # Content rating: meta tag still present on some pages.
    rating_meta = soup.find("meta", attrs={"itemprop": "contentRating"})
    if rating_meta and rating_meta.get("content"):
        content_rating = rating_meta["content"].strip()

    # Language: labeled row like "שפה: אנגלית".
    body_text = soup.get_text(" ", strip=True)
    m = re.search(r"שפה\s*[::]\s*([^\n|·•]{1,40})", body_text)
    if m:
        language = m.group(1).strip()

    return SeretMovie(
        seret_id=mid,
        title_he=title_he,
        title_en=title_en,
        description=description,
        genres=genres,
        content_rating=content_rating,
        release_date=release_date,
        runtime=runtime,
        imdb_id=imdb_id,
        imdb_score=imdb_score,
        seret_score=seret_score,
        poster_url=poster_url,
        detail_url=url,
        directors=directors,
        actors=actors,
        language=language,
    )


def scrape_movies(
    planet_titles: Optional[Iterable[str]] = None,
    progress=None,
) -> list[SeretMovie]:
    """Return seret metadata for films that match Planet's current lineup.

    `planet_titles` is an iterable of Hebrew/English titles from Planet's
    scrape. We use it to prune the set of seret MIDs we fetch detail pages
    for — seret lists 100+ films but Planet typically shows ~25, so this
    gives us a ~5× speedup over fetching every MID on every run.

    If `planet_titles` is None or empty, we fall back to fetching every
    MID (legacy behavior).

    Per-MID detail pages are cached on disk with a 12h TTL — subsequent
    daily runs are near-instant.

    `progress` is an optional callable(done: int, total: int) used to
    drive a UI progress bar.
    """
    discovery_urls = [
        f"{BASE}/movies/index.asp?catCase=4",    # now in theaters
        f"{BASE}/movies/index.asp?catCase=2",    # upcoming releases (David, Scream 7, Zootopia 2...)
        f"{BASE}/movies/newmovies.asp",          # new releases
        f"{BASE}/movies/comingsoonmovies.asp",   # coming soon overflow
    ]

    title_map: dict[int, str] = {}
    for durl in discovery_urls:
        soup = _get(durl)
        if soup:
            tm = _extract_title_map(soup)
            logger.info("Found %d movie IDs from %s", len(tm), durl)
            for mid, t in tm.items():
                if len(t) > len(title_map.get(mid, "")):
                    title_map[mid] = t

    logger.info("Total unique movie IDs discovered: %d", len(title_map))

    # Prune to the films Planet is actually showing.
    if planet_titles:
        wanted = _filter_mids_by_planet_titles(title_map, planet_titles)
        logger.info(
            "Filtered seret MIDs by Planet titles: %d → %d",
            len(title_map), len(wanted),
        )
        mids_to_fetch = wanted
    else:
        mids_to_fetch = set(title_map.keys())

    total = len(mids_to_fetch)
    if progress:
        progress(0, total)

    movies: list[SeretMovie] = []
    cache_hits = 0
    for idx, mid in enumerate(sorted(mids_to_fetch), start=1):
        cached = _cache_read(mid)
        if cached:
            movies.append(cached)
            cache_hits += 1
            logger.debug("Cache hit: %s (%s)", cached.title_en, cached.seret_id)
        else:
            time.sleep(REQUEST_DELAY)
            movie = _parse_detail(mid)
            if movie:
                movies.append(movie)
                _cache_write(movie)
                logger.debug("Scraped: %s (%s)", movie.title_en, movie.seret_id)
            else:
                logger.debug("Skipped MID=%d (no data)", mid)
        if progress:
            progress(idx, total)

    logger.info(
        "Seret scrape complete: %d movies (%d from cache, %d fetched)",
        len(movies), cache_hits, len(movies) - cache_hits,
    )
    return movies
