import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, render_template, send_file

load_dotenv()

# Allow imports from the project root
sys.path.insert(0, str(Path(__file__).parent.parent))

app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
MOVIES_FILE = DATA_DIR / "movies.json"
POSTER_DIR = DATA_DIR / "posters"

# Browsers cache this long — posters for a given film effectively never
# change, so a month is fine and shields upstream origins from repeat hits.
POSTER_BROWSER_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
# When every candidate fails we drop a 0-byte `.miss` file. Re-try after
# this long so a poster that shows up on the next scrape eventually lands.
POSTER_NEG_CACHE_TTL = 60 * 60  # 1 hour
POSTER_FETCH_TIMEOUT = 15
POSTER_MAX_BYTES = 5 * 1024 * 1024  # 5 MB ceiling — posters are ~50-200 KB

_scrape_lock = threading.Lock()
# Serialize concurrent fetches of the SAME poster so we don't stampede
# upstream when a card first appears in many viewers' viewports at once.
_poster_locks: dict[str, threading.Lock] = {}
_poster_locks_guard = threading.Lock()


def _load_movies() -> dict:
    try:
        if MOVIES_FILE.exists():
            return json.loads(MOVIES_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("Could not read movies.json: %s", e)
    return {"movies": [], "scraped_at": None, "target_dates": []}


@app.route("/")
def index():
    data = _load_movies()
    return render_template(
        "index.html",
        scraped_at=data.get("scraped_at", ""),
        target_dates=data.get("target_dates", []),
    )


@app.route("/api/movies")
def api_movies():
    return jsonify(_load_movies())


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Trigger a manual scrape in a background thread."""
    if not _scrape_lock.acquire(blocking=False):
        return jsonify({"status": "already_running"}), 202

    def _run():
        try:
            from scraper.main import run_scrape
            run_scrape()
        finally:
            _scrape_lock.release()

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started"}), 202


@app.route("/api/scrape-status")
def api_scrape_status():
    """Current scrape state — polled by the UI for the progress bar."""
    from scraper.main import get_scrape_state
    return jsonify(get_scrape_state())


# ---------------------------------------------------------------------------
# Poster proxy + cache
# ---------------------------------------------------------------------------
# Background: the frontend used to hotlink posters directly from
# seret.co.il / planetcinema.co.il / img.omdbapi.com. Those hosts occasionally
# stall, rate-limit hotlinkers, or return short-lived errors — which showed
# up to users as randomly-missing thumbnails. This route fetches each poster
# once, caches the bytes to disk, and serves with an aggressive Cache-Control
# so the browser never re-asks. Upstream flakiness no longer affects users
# after the first successful fetch.

def _poster_candidates_for(planet_id: str) -> list[str]:
    """Build the ordered fallback chain from the cached movies.json."""
    data = _load_movies()
    movie = next(
        (m for m in data.get("movies", []) if str(m.get("planet_id")) == planet_id),
        None,
    )
    if not movie:
        return []
    urls: list[str] = []
    # Primary (seret if matched, else planet — picked by merger).
    if movie.get("poster_url"):
        urls.append(movie["poster_url"])
    # Planet URL as secondary in case the primary was seret's and seret is
    # flaky. Dedup if the primary WAS already the planet URL.
    planet_url = movie.get("poster_url_planet")
    if planet_url and planet_url not in urls:
        urls.append(planet_url)
    # OMDB's poster proxy as a last resort.
    imdb_id = movie.get("imdb_id")
    if imdb_id:
        key = os.environ.get("OMDB_API_KEY", "").strip()
        if key:
            urls.append(f"https://img.omdbapi.com/?i={imdb_id}&h=600&apikey={key}")
    return urls


def _looks_like_image(body: bytes) -> bool:
    """Magic-byte sniff — belt-and-braces when Content-Type is missing/lying."""
    return (
        body[:3] == b"\xff\xd8\xff"  # JPEG
        or body[:8] == b"\x89PNG\r\n\x1a\n"  # PNG
        or body[:6] in (b"GIF87a", b"GIF89a")  # GIF
        or body[:4] == b"RIFF"  # WebP container
    )


def _fetch_poster_bytes(url: str) -> bytes | None:
    """Fetch one candidate URL. Returns bytes on success, None on any failure."""
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; CinemaDashboard/1.0)",
        "Accept": "image/*,*/*;q=0.8",
    }
    # Some origins 403 hotlinkers without a sensible Referer.
    if "seret.co.il" in url:
        headers["Referer"] = "https://www.seret.co.il/"
    elif "planetcinema.co.il" in url:
        headers["Referer"] = "https://www.planetcinema.co.il/"
    try:
        r = requests.get(url, timeout=POSTER_FETCH_TIMEOUT, headers=headers, stream=True)
        r.raise_for_status()
        # Early reject on content-length if the server gave us one.
        clen = r.headers.get("Content-Length")
        if clen and clen.isdigit() and int(clen) > POSTER_MAX_BYTES:
            logger.warning("poster too large (%s bytes) at %s", clen, url)
            return None
        body = r.content  # already-limited by requests; small in practice
        if len(body) > POSTER_MAX_BYTES:
            return None
        ct = r.headers.get("Content-Type", "").lower()
        if not ct.startswith("image/") and not _looks_like_image(body):
            logger.debug("poster not an image (ct=%s) at %s", ct, url)
            return None
        return body
    except Exception as e:
        logger.info("poster fetch failed for %s: %s", url, e)
        return None


def _poster_lock_for(planet_id: str) -> threading.Lock:
    """One lock per id — keeps the first concurrent request as the lone fetcher."""
    with _poster_locks_guard:
        lock = _poster_locks.get(planet_id)
        if lock is None:
            lock = threading.Lock()
            _poster_locks[planet_id] = lock
        return lock


def _serve_cached_poster(path: Path):
    resp = send_file(path, mimetype="image/jpeg", max_age=POSTER_BROWSER_MAX_AGE)
    # send_file's max_age covers the basics; overwrite so `immutable` is set
    # (tells browsers not to even conditionally revalidate).
    resp.headers["Cache-Control"] = f"public, max-age={POSTER_BROWSER_MAX_AGE}, immutable"
    return resp


def _fetch_and_cache_poster(planet_id: str) -> Path | None:
    """Walk candidates, write first hit to disk. Returns the cached path or None."""
    POSTER_DIR.mkdir(parents=True, exist_ok=True)
    cache = POSTER_DIR / f"{planet_id}.jpg"
    miss = POSTER_DIR / f"{planet_id}.miss"

    urls = _poster_candidates_for(planet_id)
    if not urls:
        miss.touch()
        return None

    for url in urls:
        body = _fetch_poster_bytes(url)
        if body:
            tmp = cache.with_suffix(".tmp")
            tmp.write_bytes(body)
            tmp.replace(cache)  # atomic swap
            if miss.exists():
                miss.unlink()
            return cache

    miss.touch()
    return None


@app.route("/poster/<planet_id>")
def poster(planet_id: str):
    # Basic sanitization — planet_id should be digits; reject anything else
    # so we can never construct weird cache paths from user input.
    if not planet_id.isalnum() or len(planet_id) > 32:
        abort(400)

    cache = POSTER_DIR / f"{planet_id}.jpg"
    miss = POSTER_DIR / f"{planet_id}.miss"

    # Fast path: already cached.
    if cache.exists() and cache.stat().st_size > 0:
        return _serve_cached_poster(cache)

    # Negative cache: recent miss → don't re-hammer upstream every pageview.
    if miss.exists() and (time.time() - miss.stat().st_mtime) < POSTER_NEG_CACHE_TTL:
        abort(404)

    # Slow path: fetch. Only one thread does the work per id at a time.
    lock = _poster_lock_for(planet_id)
    with lock:
        # Re-check under lock — another thread may have just filled the cache.
        if cache.exists() and cache.stat().st_size > 0:
            return _serve_cached_poster(cache)
        path = _fetch_and_cache_poster(planet_id)

    if path is None:
        abort(404)
    return _serve_cached_poster(path)


def prefetch_posters() -> None:
    """Warm the on-disk cache for every movie in the current JSON.

    Called after a successful scrape so the first visitor after a refresh
    sees zero-latency thumbnails. Quietly best-effort: failures just mean
    that film will fetch on demand like before.
    """
    data = _load_movies()
    movies = data.get("movies", [])
    if not movies:
        return
    POSTER_DIR.mkdir(parents=True, exist_ok=True)
    done = 0
    for m in movies:
        pid = str(m.get("planet_id") or "").strip()
        if not pid:
            continue
        cache = POSTER_DIR / f"{pid}.jpg"
        if cache.exists() and cache.stat().st_size > 0:
            continue
        # Reuse the same locked fetch path so a concurrent live request
        # doesn't double-fetch.
        lock = _poster_lock_for(pid)
        if not lock.acquire(blocking=False):
            continue
        try:
            if _fetch_and_cache_poster(pid) is not None:
                done += 1
        finally:
            lock.release()
    logger.info("Poster prefetch: cached %d/%d", done, len(movies))


def _initial_scrape_if_needed():
    """Run the scraper once on startup if no data file exists yet."""
    if not MOVIES_FILE.exists():
        logger.info("No movies.json found — running initial scrape")
        try:
            from scraper.main import run_scrape
            run_scrape()
        except Exception as e:
            logger.error("Initial scrape failed: %s", e)
    else:
        logger.info("movies.json found — skipping initial scrape")


if __name__ == "__main__":
    # Start background scheduler
    from scraper.main import start_scheduler
    start_scheduler()

    # Run initial scrape in background so the server starts immediately
    threading.Thread(target=_initial_scrape_if_needed, daemon=True).start()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
