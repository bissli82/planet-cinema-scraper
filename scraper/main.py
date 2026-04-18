"""
Orchestrator: runs the full scrape pipeline and writes data/movies.json.
Also manages the APScheduler that triggers scrapes twice a day.
"""

import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
MOVIES_FILE = DATA_DIR / "movies.json"
OMDB_CACHE_FILE = DATA_DIR / "omdb_cache.json"
OMDB_TITLE_CACHE_FILE = DATA_DIR / "omdb_title_cache.json"


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def get_target_dates() -> list[date]:
    """
    Returns [today, this_thursday, next_thursday].
    If today IS Thursday, returns [today, next_thursday].
    """
    today = date.today()
    days_to_thu = (3 - today.weekday()) % 7  # Thursday = weekday 3
    this_thu = today + timedelta(days=days_to_thu) if days_to_thu > 0 else today
    next_thu = this_thu + timedelta(days=7)

    dates = [today]
    if this_thu != today:
        dates.append(this_thu)
    dates.append(next_thu)
    return dates


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run_scrape() -> None:
    logger.info("=== Scrape started ===")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # --- Planetcinema: discover all available dates, then fetch showtimes ---
    from scraper.planetcinema import fetch_available_dates, scrape_showtimes
    target_dates = fetch_available_dates(days_ahead=21)
    if not target_dates:
        # Fallback to the legacy today/thursdays window
        target_dates = get_target_dates()
    logger.info("Target dates: %s", [d.isoformat() for d in target_dates])

    planet_films = scrape_showtimes(target_dates)
    logger.info("Planet: %d films showing at Ayalon across target dates", len(planet_films))

    # --- Seret (metadata enrichment) ---
    from scraper.seret import scrape_movies
    seret_movies = scrape_movies()
    logger.info("Seret: %d movies for enrichment", len(seret_movies))

    # --- OMDB title-search fallback: resolve missing imdb_ids by title+year ---
    # Some seret pages (e.g. הדרמה) lack the IMDB widget entirely, so we
    # look them up via OMDB's title search. Cached by seret_id to avoid repeats.
    from scraper.omdb import resolve_missing_ids, enrich_movies
    title_cache = _load_json(OMDB_TITLE_CACHE_FILE, {})
    pending = []
    for s in seret_movies:
        if s.imdb_id or not s.title_en:
            continue
        # Try to extract a year from release_date (formats vary: "23/10/2025", "2025", etc.)
        year = None
        if s.release_date:
            import re as _re
            m = _re.search(r"(19|20)\d{2}", s.release_date)
            if m:
                year = m.group(0)
        pending.append({"key": str(s.seret_id), "title": s.title_en, "year": year})
    if pending:
        title_cache = resolve_missing_ids(pending, title_cache)
        _save_json(OMDB_TITLE_CACHE_FILE, title_cache)
        # Apply resolved IDs back to seret_movies
        applied = 0
        for s in seret_movies:
            if not s.imdb_id:
                rid = title_cache.get(str(s.seret_id))
                if rid:
                    s.imdb_id = rid
                    applied += 1
        logger.info("OMDB title-search: applied %d resolved IDs to seret_movies", applied)

    # --- OMDB (optional fallback for missing IMDB scores) ---
    omdb_cache = _load_json(OMDB_CACHE_FILE, {})
    omdb_cache = enrich_movies(
        [{"imdb_id": m.imdb_id} for m in seret_movies],
        omdb_cache,
    )
    _save_json(OMDB_CACHE_FILE, omdb_cache)

    # --- IMDB enrichment (Cinemagoer, open source) ---
    from scraper.imdb_scores import enrich_imdb
    imdb_cache_file = DATA_DIR / "imdb_cache.json"
    imdb_cache = _load_json(imdb_cache_file, {})
    imdb_ids = [m.imdb_id for m in seret_movies if m.imdb_id]
    imdb_cache = enrich_imdb(imdb_ids, imdb_cache)
    _save_json(imdb_cache_file, imdb_cache)

    # --- Planet detail page enrichment (synopsis for planet-only films) ---
    from scraper.planet_details import enrich_planet_only
    planet_details = enrich_planet_only(planet_films)

    # --- Merge ---
    from scraper.merger import merge
    movies = merge(planet_films, seret_movies, omdb_cache, imdb_cache, planet_details)

    output = {
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
        "target_dates": [d.isoformat() for d in target_dates],
        "movies": movies,
    }
    _save_json(MOVIES_FILE, output)
    logger.info("=== Scrape complete: %d movies written ===", len(movies))


# ---------------------------------------------------------------------------
# Scheduler (used when running as part of the web server)
# ---------------------------------------------------------------------------

def start_scheduler() -> None:
    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler(timezone="Asia/Jerusalem")
    scheduler.add_job(run_scrape, "cron", hour="7,19", minute=0, id="scrape")
    scheduler.start()
    logger.info("Scheduler started (scrapes at 07:00 and 19:00 Jerusalem time)")


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Could not load %s: %s", path, e)
    return default


def _save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run_scrape()
