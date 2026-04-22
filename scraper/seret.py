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


def _parse_detail(mid: int) -> Optional[SeretMovie]:
    url = f"{BASE}/movies/s_movies.asp?MID={mid}"
    soup = _get(url)
    if not soup:
        return None

    title_he = ""
    title_en = ""
    description = ""
    genres: list[str] = []
    content_rating = ""
    release_date = ""
    runtime = ""
    imdb_id = None
    imdb_score: Optional[float] = None
    seret_score: Optional[float] = None

    t = soup.select_one("span[itemprop='name']")
    if t:
        title_he = t.get_text(strip=True)

    t = soup.select_one("span[itemprop='alternatename']")
    if t:
        title_en = t.get_text(strip=True)

    if not title_he and not title_en:
        return None  # skip invalid/deleted movie pages

    t = soup.select_one("span[itemprop='description']")
    if t:
        description = t.get_text(strip=True)

    for g in soup.select("span[itemprop='genre']"):
        text = g.get_text(strip=True)
        if text:
            genres.append(text)

    t = soup.select_one("span[itemprop='contentRating']")
    if t:
        content_rating = t.get_text(strip=True)

    t = soup.select_one("span[itemprop='datePublished']")
    if t:
        release_date = t.get_text(strip=True)

    # Runtime: prefer itemprop duration (e.g. <span itemprop="duration" datetime="PT98M">98</span>)
    dur = soup.select_one("span[itemprop='duration']")
    if dur:
        minutes = dur.get_text(strip=True)
        if minutes.isdigit():
            runtime = f"{minutes} min"
    if not runtime:
        for tag in soup.find_all(string=re.compile(r"\d+\s*דקות")):
            m = re.search(r"(\d+)\s*דקות", tag)
            if m:
                runtime = f"{m.group(1)} min"
                break

    # IMDB ID from rating widget
    imdb_widget = soup.select_one("span.imdbRatingPlugin[data-title]")
    if imdb_widget:
        imdb_id = imdb_widget.get("data-title")  # e.g. "tt17490712"

    # NOTE: seret's page has two IMDb-looking numbers — the "IMDb 6.5/10" badge
    # next to the poster (rendered live by IMDb's widget JS, NOT in static HTML)
    # and a seret-internal stats line ("קהל: 6.8 · IMDb: 7.7 · כוונה: 3.8") which
    # is seret's proprietary metric, NOT IMDb's real rating. A previous version
    # of this code scraped the latter thinking it was IMDb's score — it's not.
    # Real IMDb scores come from the public dataset (scraper/imdb_scores.py)
    # keyed by the imdb_id extracted above, which IS reliable.
    imdb_score = None

    # Seret's own score — appears in an SVG badge as <text font-weight="900">X.X</text>
    for t in soup.select("svg text"):
        if t.get("font-weight") == "900":
            try:
                val = float(t.get_text(strip=True))
                if 0 < val <= 10:
                    seret_score = val
                    break
            except ValueError:
                continue

    poster_url = _extract_poster(soup)

    directors: list[str] = []
    for d in soup.select("span[itemprop='director'] span[itemprop='name'], a[itemprop='director'], span[itemprop='director']"):
        name = d.get_text(strip=True)
        if name and name not in directors:
            directors.append(name)

    actors: list[str] = []
    for a in soup.select("span[itemprop='actor'] span[itemprop='name'], a[itemprop='actor'], span[itemprop='actor']"):
        name = a.get_text(strip=True)
        if name and name not in actors:
            actors.append(name)
        if len(actors) >= 6:
            break

    language = ""
    lang_el = soup.select_one("span[itemprop='inLanguage']")
    if lang_el:
        language = lang_el.get_text(strip=True)
    if not language:
        # Seret often lists language in a labeled row like "שפה: אנגלית"
        m = re.search(r"שפה\s*[::]\s*([^\n|·•]{1,40})", soup.get_text(" ", strip=True))
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
