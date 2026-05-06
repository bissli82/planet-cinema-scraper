import difflib
import logging
import os
import re
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


def _normalize_title(title: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — for safe comparison."""
    t = (title or "").lower()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _titles_match(query: str, result: str, min_ratio: float = 0.85) -> bool:
    """
    Guard against OMDB's loose title-search returning an unrelated film.
    Two real-world failures we guard against:
      - 'Two Women' → 'Deux femmes en or' (French-Canadian 2025, unrelated)
      - 'Two Women' → 'Between Two Women' (US 2025 short, unrelated despite
        containing the query as a substring)

    Accept when:
      1. Normalized titles are equal, or
      2. The result is the query plus a subtitle after a delimiter
         (': ', ' - ', ' — ') — e.g. 'The Mummy: Resurrection' matches
         'The Mummy', or
      3. difflib ratio >= min_ratio.
    Otherwise reject.
    """
    q_raw = (query or "").strip().lower()
    r_raw = (result or "").strip().lower()
    q = _normalize_title(query)
    r = _normalize_title(result)
    if not q or not r:
        return False
    if q == r:
        return True
    # Subtitle expansion: result starts with query + delimiter
    for delim in [": ", " - ", " — ", " – "]:
        if r_raw.startswith(q_raw + delim):
            return True
    return difflib.SequenceMatcher(None, q, r).ratio() >= min_ratio


def _title_variants(title: str) -> list[str]:
    """Generate progressively-cleaned variants of a Planet/seret title.

    Planet titles often arrive in messy forms — Russian-prefixed bilingual
    strings ("ДЬЯВОЛ НОСИТ PRADA 2 - The Devil Wears Prada 2"), trailing
    colons from truncation ("That Time I Got Reincarnated as a Slime:"),
    parenthesized year suffixes ("Foo (1986)"), and "+" double-feature
    combinations. OMDB's ?t= search rarely matches the raw form but
    usually hits one of these cleaned variants. Order matters: the
    full original title goes first (most specific) so we don't match
    a too-broad variant when a precise one would have worked.
    """
    if not title:
        return []
    seen: list[str] = []

    def _push(s: str) -> None:
        s = s.strip()
        if s and s not in seen:
            seen.append(s)

    original = title.strip()
    _push(original)
    # Strip trailing "(YYYY)"
    _push(re.sub(r"\s*\(\d{4}\)\s*$", "", original))
    # Strip trailing colon ("...Slime:" → "...Slime")
    _push(original.rstrip(":"))
    # Right-of-" - " (handles "RUSSIAN - English" pattern)
    if " - " in original:
        right = original.split(" - ", 1)[1].strip()
        right = re.sub(r"\s*\(\d{4}\)\s*$", "", right).rstrip(":").strip()
        _push(right)
    # First half of a "+" double-feature combination
    if " + " in original:
        first = original.split(" + ", 1)[0].strip()
        _push(re.sub(r"\s*\(\d{4}\)\s*$", "", first))
    return seen


# Hardcoded IMDb IDs for Hebrew-only classics OMDB can't index. Keyed by
# planet's Hebrew title with the "(YYYY)" suffix stripped — that suffix
# is how Planet distinguishes anniversary re-releases from a canonical
# entry. Keep small; if it grows past ~15 entries move to data/.
HEBREW_TITLE_OVERRIDES = {
    "אהבה בשחקים": "tt0092099",          # Top Gun (1986)
    "אהבה בשחקים מאווריק": "tt1745960",  # Top Gun: Maverick (2022)
}


def _hebrew_override(title: str) -> Optional[str]:
    """Look up a planet Hebrew title in the manual override map."""
    if not title:
        return None
    norm = re.sub(r"\s*\(\d{4}\)\s*$", "", title.strip()).strip()
    return HEBREW_TITLE_OVERRIDES.get(norm)


def fetch_omdb_by_title(title: str, year: Optional[str] = None) -> Optional[dict]:
    """Resolve a film by title (optional year). Returns the OMDB record incl. imdbID.

    Rejects results whose title isn't sufficiently similar to the query —
    OMDB's ?t= endpoint matches loosely on translated/alternate titles
    and will happily return an unrelated film for generic names.
    """
    key = _api_key()
    if not key or not title:
        return None
    params = {"t": title, "apikey": key, "type": "movie"}
    if year:
        params["y"] = str(year)
    try:
        r = requests.get(OMDB_BASE, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("Response") != "True":
            logger.debug("OMDB title search miss for %r (%s): %s",
                         title, year, data.get("Error"))
            return None
        returned_title = data.get("Title", "")
        if not _titles_match(title, returned_title):
            logger.info(
                "OMDB title-search rejected: query=%r → OMDB returned %r (imdbID=%s) — titles don't match",
                title, returned_title, data.get("imdbID"),
            )
            return None
        return data
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


def resolve_planet_only_ids(
    movies: list[dict],
    title_cache: dict[str, str],
) -> tuple[list[str], dict[str, str]]:
    """Resolve missing imdb_ids for films that have no seret match.

    For each merged movie without an `imdb_id`:
      1. Check the manual Hebrew-title override map (no API call) — handles
         classics OMDB can't index by Hebrew title.
      2. Try OMDB's title search across cleaned title variants (Russian
         prefix stripping, trailing colons, year suffixes, "+" combos).
      3. Cache hits AND confirmed misses by `planet_<id>` so subsequent
         scrapes skip the API for known-empty entries.

    Mutates `movies` in place by setting `imdb_id` on resolved entries.
    Returns `(new_imdb_ids, updated_title_cache)`. The new ids list lets
    the caller batch-fetch their scores from the IMDb dataset.
    """
    new_ids: list[str] = []
    cache = dict(title_cache)
    have_key = bool(_api_key())

    for movie in movies:
        if movie.get("imdb_id"):
            continue
        title_he = movie.get("title_he") or ""
        title_en = movie.get("title_en") or ""
        if not title_he and not title_en:
            continue

        # Manual override path (free, no API call). Always tried, even
        # without an OMDB key — that's the whole point of the map. Keyed
        # on Hebrew title since these are Hebrew-only classics.
        manual = _hebrew_override(title_he)
        if manual:
            movie["imdb_id"] = manual
            new_ids.append(manual)
            logger.info("planet-only: hebrew override %r → %s", title_he, manual)
            continue

        if not have_key:
            continue

        cache_key = f"planet_{movie.get('planet_id')}"
        if cache_key in cache:
            cached = cache[cache_key]
            if cached:
                movie["imdb_id"] = cached
                new_ids.append(cached)
            # else: cached miss, skip silently
            continue

        # Year for OMDB's `y=` disambiguator
        year: Optional[str] = None
        ry = movie.get("release_year") or ""
        m = re.search(r"(19|20)\d{2}", str(ry))
        if m:
            year = m.group(0)

        # Try English title first (cleanest input for OMDB) then Hebrew
        # as a secondary path. The variant cleanup runs over each.
        imdb_id = ""
        attempted_variants: list[str] = []
        for source_title in (title_en, title_he):
            if imdb_id or not source_title:
                continue
            for variant in _title_variants(source_title):
                if variant in attempted_variants:
                    continue
                attempted_variants.append(variant)
                time.sleep(REQUEST_DELAY)
                data = fetch_omdb_by_title(variant, year)
                candidate = (data or {}).get("imdbID") or ""
                if candidate:
                    imdb_id = candidate
                    logger.info(
                        "planet-only: %r (variant %r, year %s) → %s (%s)",
                        source_title, variant, year, imdb_id,
                        (data or {}).get("Title"),
                    )
                    break

        cache[cache_key] = imdb_id  # cache miss as "" too
        if imdb_id:
            movie["imdb_id"] = imdb_id
            new_ids.append(imdb_id)

    logger.info(
        "Planet-only OMDB resolve: %d ids resolved across %d unmatched films",
        len(new_ids),
        sum(1 for m in movies if not m.get("matched_seret")),
    )
    return new_ids, cache
