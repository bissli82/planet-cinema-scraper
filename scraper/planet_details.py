"""
Scrape the planet film detail page for synopsis when the film isn't on seret.

Planet's detail page is JS-rendered, but the initial HTML already contains
the film payload as an embedded JSON blob (keys: synopsis, shortSynopsis,
cast, director, ...). We pull those via simple string walking — no browser.
"""

import json
import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0",
    "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
}
REQUEST_DELAY = 0.5


def _extract_string(text: str, key: str) -> Optional[str]:
    needle = '"' + key + '":"'
    idx = text.find(needle)
    if idx < 0:
        return None
    start = idx + len(needle)
    i = start
    backslash = "\\"
    while i < len(text):
        ch = text[i]
        if ch == backslash:
            i += 2
            continue
        if ch == '"':
            break
        i += 1
    raw = text[start:i]
    try:
        return json.loads('"' + raw + '"')
    except Exception:
        return None


def _extract_string_list(text: str, key: str, limit: int = 10) -> list[str]:
    """For keys like 'cast' whose value is a JSON array of strings."""
    needle = '"' + key + '":['
    idx = text.find(needle)
    if idx < 0:
        return []
    start = idx + len(needle)
    depth = 1
    i = start
    while i < len(text) and depth:
        ch = text[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        i += 1
    raw = "[" + text[start:i - 1] + "]"
    try:
        arr = json.loads(raw)
        return [str(x) for x in arr if isinstance(x, (str, int))][:limit]
    except Exception:
        return []


def fetch_planet_details(page_url: str) -> dict:
    """Returns {synopsis, cast, directors, language} or {} on failure."""
    if not page_url:
        return {}
    try:
        r = requests.get(page_url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return {}
        text = r.text
        synopsis = _extract_string(text, "synopsis") or _extract_string(text, "shortSynopsis") or ""
        # Planet often ships synopsis with HTML tags like <p>...</p> — strip them
        if synopsis:
            import re as _re
            synopsis = _re.sub(r"<[^>]+>", " ", synopsis)
            synopsis = _re.sub(r"\s+", " ", synopsis).strip()
        cast = _extract_string_list(text, "cast", limit=6)
        directors = _extract_string_list(text, "directors")
        return {
            "synopsis": synopsis,
            "cast": cast,
            "directors": directors,
        }
    except Exception as e:
        logger.debug("Planet detail fetch failed %s: %s", page_url, e)
        return {}


def enrich_planet_only(films: list) -> dict[str, dict]:
    """
    For each planet film, return a dict {film_id: details}. Called for all
    planet films, but caller should only apply for unmatched ones.
    """
    out: dict[str, dict] = {}
    for f in films:
        time.sleep(REQUEST_DELAY)
        details = fetch_planet_details(getattr(f, "page_url", ""))
        if details:
            out[f.id] = details
    logger.info("Planet detail enrichment: %d films fetched", len(out))
    return out
