"""
Scrapes the real showtimes API for planetcinema.co.il (Vista Cinema backend).

Endpoint discovered via network inspection:
  /il/data-api-service/v1/quickbook/{tenantId}/film-events/in-cinema/{cinemaId}/at-date/{date}

Returns a JSON payload with:
  body.films  - films playing that date (id, name, length, posterLink, attributeIds, ...)
  body.events - individual showtimes (filmId, eventDateTime, auditorium, bookingLink, ...)

No headless browser needed — direct REST call with browser-like headers.
"""

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import requests

logger = logging.getLogger(__name__)

TENANT_ID = 10100
CINEMA_ID = 1025  # Ayalon
BASE_URL = "https://www.planetcinema.co.il"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0",
    "Accept": "application/json, text/plain, */*",
    "Referer": BASE_URL + "/",
    "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
}


@dataclass
class PlanetFilm:
    id: str                 # e.g. "8031s2r"
    name: str               # Hebrew title
    length_min: int
    poster_url: str
    page_url: str           # movie info page
    release_year: str
    attribute_ids: list[str]  # e.g. ["comedy", "2d", "subbed", ...]
    showtimes: dict[str, list[dict]] = field(default_factory=dict)
    # showtimes[date_str] = [{"time": "23:30", "auditorium": "...", "format": ["imax", ...]}]


def _event_url(date_str: str) -> str:
    return (
        f"{BASE_URL}/il/data-api-service/v1/quickbook/{TENANT_ID}"
        f"/film-events/in-cinema/{CINEMA_ID}/at-date/{date_str}"
        f"?attr=&lang=he_IL"
    )


def _dates_url(until_date: str) -> str:
    return (
        f"{BASE_URL}/il/data-api-service/v1/quickbook/{TENANT_ID}"
        f"/dates/in-cinema/{CINEMA_ID}/until/{until_date}"
        f"?attr=&lang=he_IL"
    )


def fetch_available_dates(days_ahead: int = 21) -> list[date]:
    """Query planet for all dates with showtimes in the next `days_ahead` days."""
    from datetime import timedelta
    until = (date.today() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    try:
        r = requests.get(_dates_url(until), headers=HEADERS, timeout=15)
        if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
            body = r.json().get("body", {})
            dates = body.get("dates", []) or r.json().get("dates", [])
            out: list[date] = []
            for s in dates:
                try:
                    out.append(date.fromisoformat(s[:10]))
                except ValueError:
                    continue
            if out:
                logger.info("Planet available dates: %d found", len(out))
                return sorted(out)
        logger.warning("Planet dates endpoint unexpected response: %s", r.status_code)
    except Exception as e:
        logger.warning("Planet dates fetch error: %s", e)
    return []


def scrape_showtimes(target_dates: list[date]) -> list[PlanetFilm]:
    """Fetch films + showtimes for each target date. Returns a list of PlanetFilm."""
    films_by_id: dict[str, PlanetFilm] = {}

    for d in target_dates:
        date_str = d.strftime("%Y-%m-%d")
        data = _fetch(date_str)
        if not data:
            continue

        body = data.get("body", {})
        raw_films = body.get("films", [])
        raw_events = body.get("events", [])

        # Register all films seen this date
        for f in raw_films:
            fid = f.get("id")
            if not fid:
                continue
            if fid not in films_by_id:
                films_by_id[fid] = PlanetFilm(
                    id=fid,
                    name=f.get("name", "").strip(),
                    length_min=int(f.get("length") or 0),
                    poster_url=f.get("posterLink", ""),
                    page_url=f.get("link", ""),
                    release_year=str(f.get("releaseYear", "")),
                    attribute_ids=list(f.get("attributeIds", [])),
                )

        # Add showtimes
        for e in raw_events:
            fid = e.get("filmId")
            if not fid or fid not in films_by_id:
                continue
            dt = e.get("eventDateTime", "")
            time_str = dt.split("T")[1][:5] if "T" in dt else ""
            if not time_str:
                continue
            films_by_id[fid].showtimes.setdefault(date_str, []).append({
                "time": time_str,
                "auditorium": e.get("auditoriumTinyName") or e.get("auditorium") or "",
                "format": [a for a in e.get("attributeIds", [])
                           if a in _FORMAT_ATTRS],
                "booking_link": e.get("bookingLink", ""),
                "sold_out": bool(e.get("soldOut", False)),
            })

        logger.info(
            "Planet %s: %d films, %d events",
            date_str, len(raw_films), len(raw_events),
        )

    # Sort showtimes within each date
    for film in films_by_id.values():
        for dkey in film.showtimes:
            film.showtimes[dkey].sort(key=lambda x: x["time"])

    films = list(films_by_id.values())
    logger.info("Planet scrape complete: %d unique films across all dates", len(films))
    return films


def _fetch(date_str: str) -> Optional[dict]:
    url = _event_url(date_str)
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
            return r.json()
        logger.warning("Planet fetch failed %s: %s", url, r.status_code)
    except Exception as e:
        logger.warning("Planet fetch error %s: %s", url, e)
    return None


# Attribute IDs that represent a cinema format (vs genre / language)
_FORMAT_ATTRS = {
    "2d", "3d", "4dx", "imax", "screenx", "vip", "vip-light",
    "dolby", "atmos", "dolby-atmos", "upgrade",
}

# Attribute IDs that represent a genre we want to surface
GENRE_ATTRS = {
    "action", "adventure", "animation", "comedy", "crime", "documentary",
    "drama", "family", "fantasy", "horror", "musical", "mystery",
    "romance", "sci-fi", "thriller", "war", "western", "biography",
    "kids",
}
