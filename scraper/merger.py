"""
Merges planetcinema films (source of truth: what's actually showing at Ayalon)
with seret.co.il metadata (for IMDB score, description, English title).
"""

import difflib
import logging
import re
from typing import Optional

from scraper.planetcinema import GENRE_ATTRS, PlanetFilm
from scraper.seret import SeretMovie

logger = logging.getLogger(__name__)


def _normalize(title: str) -> str:
    title = (title or "").lower()
    title = re.sub(r"[^\w\s\u0590-\u05FF]", "", title)  # keep Hebrew + word chars
    title = re.sub(r"\b(the|a|an)\b", "", title)
    return re.sub(r"\s+", " ", title).strip()


def _match_seret(planet_name: str, seret_lookup: dict[str, SeretMovie]) -> Optional[SeretMovie]:
    """Return best-matching SeretMovie by Hebrew or English title (fuzzy, strict)."""
    if not planet_name:
        return None
    q = _normalize(planet_name)
    if not q:
        return None
    # Exact normalized hit first
    if q in seret_lookup:
        return seret_lookup[q]
    # Fuzzy: require a high ratio AND length similarity to avoid absurd matches
    candidates = difflib.get_close_matches(q, seret_lookup.keys(), n=3, cutoff=0.85)
    for c in candidates:
        # guard against very different lengths (e.g. "מומיה" vs "סופר מריו גלקסי")
        if abs(len(c) - len(q)) / max(len(c), len(q)) < 0.5:
            return seret_lookup[c]
    return None


def merge(
    planet_films: list[PlanetFilm],
    seret_movies: list[SeretMovie],
    omdb_cache: dict[str, dict],
    imdb_cache: Optional[dict[str, dict]] = None,
    planet_details: Optional[dict[str, dict]] = None,
) -> list[dict]:
    imdb_cache = imdb_cache or {}
    planet_details = planet_details or {}
    # Build a normalized-title → SeretMovie lookup (both Hebrew and English titles)
    seret_lookup: dict[str, SeretMovie] = {}
    for s in seret_movies:
        for t in (s.title_he, s.title_en):
            if t:
                seret_lookup[_normalize(t)] = s

    results: list[dict] = []

    for pf in planet_films:
        s = _match_seret(pf.name, seret_lookup)

        imdb_id = s.imdb_id if s else None
        imdb_info = imdb_cache.get(imdb_id or "", {}) or {}
        omdb = omdb_cache.get(imdb_id or "", {})
        # IMDB public dataset is authoritative — it's the actual IMDb rating,
        # updated daily. OMDB is a decent fallback (it also sources from IMDb
        # but lags and often returns N/A for new films). We deliberately do NOT
        # use seret's `imdb_score` any more: what seret displays under "IMDb:"
        # in its stats strip is a seret-internal metric, not IMDb's real score.
        imdb_score = (
            imdb_info.get("imdb_score")
            or omdb.get("imdb_score")
        )

        # Extract genres: prefer seret's Hebrew genres; fall back to planet attribute tags
        genres = []
        if s and s.genres:
            genres = s.genres
        else:
            genres = [a for a in pf.attribute_ids if a in GENRE_ATTRS]

        # Formats (4DX, IMAX, VIP, etc) — unique across all showtimes of the film
        format_set = set()
        for shows in pf.showtimes.values():
            for sh in shows:
                for fmt in sh.get("format", []):
                    format_set.add(fmt)

        pdet = planet_details.get(pf.id, {}) or {}
        directors = (s.directors if s and s.directors else []) or imdb_info.get("directors", []) or pdet.get("directors", [])
        actors = (s.actors if s and s.actors else []) or imdb_info.get("cast", []) or pdet.get("cast", [])
        language = (s.language if s and s.language else "") or omdb.get("language", "")
        description = (
            (s.description if s else "")
            or imdb_info.get("plot", "")
            or omdb.get("plot_en", "")
            or pdet.get("synopsis", "")
        )
        runtime = (
            (s.runtime if s and s.runtime else "")
            or imdb_info.get("runtime", "")
            or (f"{pf.length_min} min" if pf.length_min else "")
        )

        results.append({
            "planet_id": pf.id,
            "title_he": s.title_he if s else pf.name,
            "title_en": s.title_en if s else "",
            "imdb_id": imdb_id,
            "imdb_score": imdb_score,
            "seret_score": s.seret_score if s else None,
            "genres": genres,
            "description": description,
            "runtime": runtime,
            "content_rating": s.content_rating if s else "",
            "release_year": pf.release_year,
            # `poster_url` is the primary candidate (seret if we have a match,
            # else planet). `poster_url_planet` is the planet URL retained as
            # a fallback — the web layer's /poster/<id> route walks both (plus
            # OMDB) when the primary 404s or times out, so a flaky seret CDN
            # doesn't mean "no poster at all".
            "poster_url": (s.poster_url if s and s.poster_url else pf.poster_url),
            "poster_url_planet": pf.poster_url,
            "page_url": pf.page_url,
            "detail_url": s.detail_url if s else pf.page_url,
            "formats": sorted(format_set),
            "showtimes": pf.showtimes,
            "matched_seret": bool(s),
            "directors": directors,
            "actors": actors,
            "language": language,
        })

    # Sort: by IMDB desc, then by title
    results.sort(key=lambda x: (-(x["imdb_score"] or 0), x["title_he"] or ""))

    matched_count = sum(1 for r in results if r["matched_seret"])
    logger.info("Merge: %d films, %d matched to seret (%.0f%%)",
                len(results), matched_count,
                100 * matched_count / max(len(results), 1))

    return results
