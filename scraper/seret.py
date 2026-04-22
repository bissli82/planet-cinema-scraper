import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE = "https://www.seret.co.il"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
}
# Seconds between requests to avoid hammering the server
REQUEST_DELAY = 1.0


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

    # IMDB score: appears in seret's rating-breakdown tooltip as "IMDb: 6.4" or "IMDb: 7"
    page_text = soup.get_text(" ", strip=True)
    m = re.search(r"IMDb\s*[:\s]\s*(\d+(?:\.\d+)?)", page_text)
    if m:
        try:
            val = float(m.group(1))
            if 0 < val <= 10:
                imdb_score = val
        except ValueError:
            pass

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


def scrape_movies(progress=None) -> list[SeretMovie]:
    """Return all movies currently in theaters + recent new releases from seret.co.il.

    `progress` is an optional callable(done: int, total: int) used by the
    orchestrator to drive a UI progress bar as each movie page is fetched.
    """
    discovery_urls = [
        f"{BASE}/movies/index.asp?catCase=4",    # now in theaters
        f"{BASE}/movies/index.asp?catCase=2",    # upcoming releases (David, Scream 7, Zootopia 2...)
        f"{BASE}/movies/newmovies.asp",          # new releases
        f"{BASE}/movies/comingsoonmovies.asp",   # coming soon overflow
    ]

    all_ids: set[int] = set()
    for durl in discovery_urls:
        soup = _get(durl)
        if soup:
            ids = _extract_movie_ids(soup)
            logger.info("Found %d movie IDs from %s", len(ids), durl)
            all_ids.update(ids)

    logger.info("Total unique movie IDs to scrape: %d", len(all_ids))
    total = len(all_ids)
    if progress:
        progress(0, total)

    movies: list[SeretMovie] = []
    for idx, mid in enumerate(sorted(all_ids), start=1):
        time.sleep(REQUEST_DELAY)
        movie = _parse_detail(mid)
        if movie:
            movies.append(movie)
            logger.debug("Scraped: %s (%s)", movie.title_en, movie.seret_id)
        else:
            logger.debug("Skipped MID=%d (no data)", mid)
        if progress:
            progress(idx, total)

    logger.info("Seret scrape complete: %d movies", len(movies))
    return movies
