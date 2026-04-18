import logging
import os
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

OMDB_BASE = "http://www.omdbapi.com/"
REQUEST_DELAY = 0.3  # stay well under the 1,000/day free limit


def _api_key() -> Optional[str]:
    key = os.environ.get("OMDB_API_KEY", "").strip()
    return key or None


def fetch_omdb(imdb_id: str) -> Optional[dict]:
    """Fetch movie data from OMDB for the given IMDB title ID (e.g. 'tt17490712')."""
    key = _api_key()
    if not key:
        return None
    try:
        r = requests.get(
            OMDB_BASE,
            params={"i": imdb_id, "apikey": key},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("Response") == "True":
            return data
        logger.debug("OMDB returned no data for %s: %s", imdb_id, data.get("Error"))
    except Exception as e:
        logger.warning("OMDB request failed for %s: %s", imdb_id, e)
    return None


def fetch_omdb_by_title(title: str, year: Optional[str] = None) -> Optional[dict]:
    """Resolve a film by title (optional year). Returns the OMDB record incl. imdbID."""
    key = _api_key()
    if not key or not title:
        return None
    params = {"t": title, "apikey": key}
    if year:
        params["y"] = str(year)
    try:
        r = requests.get(OMDB_BASE, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("Response") == "True":
            return data
        logger.debug("OMDB title search miss for %r (%s): %s", title, year, data.get("Error"))
    except Exception as e:
        logger.warning("OMDB title search failed for %r: %s", title, e)
    return None


def resolve_missing_ids(
    pending: list[dict],
    title_cache: dict[str, str],
) -> dict[str, str]:
    """
    Resolve imdb_id by title+year for films that don't have one yet.
    `pending` entries: {"key": unique_str, "title": str, "year": str|None}.
    `title_cache`: maps key -> imdb_id (or "" for confirmed miss) to avoid repeats.
    Returns updated title_cache.
    """
    cache = dict(title_cache)
    if not _api_key():
        logger.info("OMDB_API_KEY not set — skipping title-based ID resolution")
        return cache
    resolved = 0
    for item in pending:
        k = item["key"]
        if k in cache:
            continue
        time.sleep(REQUEST_DELAY)
        data = fetch_omdb_by_title(item["title"], item.get("year"))
        imdb_id = (data or {}).get("imdbID", "")
        cache[k] = imdb_id
        if imdb_id:
            resolved += 1
    logger.info("OMDB title-search: %d/%d IDs resolved", resolved, len(pending))
    return cache


def enrich_movies(movies: list[dict], existing_cache: dict[str, dict]) -> dict[str, dict]:
    """
    Fetch OMDB data for movies that don't yet have it cached.
    Returns updated cache keyed by IMDB ID. No-op if OMDB_API_KEY is unset.
    """
    cache = dict(existing_cache)
    if not _api_key():
        logger.info("OMDB_API_KEY not set — skipping OMDB enrichment (seret IMDB scores will be used)")
        return cache
    for movie in movies:
        imdb_id = movie.get("imdb_id")
        if not imdb_id or imdb_id in cache:
            continue
        time.sleep(REQUEST_DELAY)
        data = fetch_omdb(imdb_id)
        if data:
            cache[imdb_id] = {
                "imdb_score": _parse_rating(data.get("imdbRating")),
                "runtime": data.get("Runtime", ""),
                "genre_en": data.get("Genre", ""),
                "plot_en": data.get("Plot", ""),
                "poster_omdb": data.get("Poster", ""),
                "language": data.get("Language", ""),
            }
            logger.debug("OMDB enriched %s → %.1f", imdb_id, cache[imdb_id]["imdb_score"] or 0)
    return cache


def _parse_rating(value: Optional[str]) -> Optional[float]:
    try:
        return float(value) if value and value != "N/A" else None
    except (ValueError, TypeError):
        return None
