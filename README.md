# Planet Cinema Ayalon Dashboard

A self-hosted dashboard that tells you **what's actually playing at Planet Cinema Ayalon** — today, this Thursday, next Thursday, and every date in between — sorted by IMDB score so the good stuff floats to the top.

Runs in a single Docker container. Refreshes twice a day. Hebrew RTL UI with posters, synopses, cast, runtime, content rating, and showtimes per date.

![stack](https://img.shields.io/badge/python-3.10-blue) ![stack](https://img.shields.io/badge/flask-gunicorn-lightgrey) ![stack](https://img.shields.io/badge/docker-ready-0db7ed)

---

## Why

Planet Cinema's own site is a Single Page App that makes it painful to scan what's worth watching — no IMDB scores, no easy sort, no synopsis on hover, and you have to click through each film to see showtimes. Seret.co.il has the metadata but no showtimes. This project stitches the two together and adds IMDB ratings from IMDB's official public dataset, giving you one page you can glance at daily.

---

## What it does

The pipeline (runs at 07:00 and 19:00 Jerusalem time):

1. **Discover dates** — hits Planet Cinema's `/dates/in-cinema/1025/until/...` endpoint to find every date that actually has screenings.
2. **Fetch showtimes** — for each date, pulls the Vista Cinema quickbook API for cinema 1025 (Ayalon): title, hall, format (IMAX / 4DX / VIP / standard), start times.
3. **Scrape seret.co.il metadata** — for each film on seret's _now playing_ / _coming soon_ / _new releases_ / `catCase=2` pages: Hebrew + English titles, synopsis, genres, runtime, content rating, IMDB ID, directors, cast, language, poster (from `og:image`, which is the only reliably per-film source on seret).
4. **Resolve missing IMDB IDs via OMDB** — some seret pages (e.g. הדרמה) don't embed the IMDB widget. If an `OMDB_API_KEY` is configured, we query OMDB by title + year to fill in the gaps. Results cached per seret_id so each film is only queried once.
5. **IMDB scores** — downloads IMDB's official [public dataset](https://developer.imdb.com/non-commercial-datasets/) (`title.ratings.tsv.gz`, ~8 MB, refreshed daily). Stream-parses it for only the IDs we care about. No API key, no WAF blocking, no rate limits — the right solution for a cloud-hosted scraper.
6. **Planet-only synopsis enrichment** — for films on Planet that aren't in seret's catalog (e.g. some pre-releases), scrapes Planet's film detail page for the embedded JSON (`"synopsis":"..."`).
7. **Merge** — matches seret ↔ planet by fuzzy Hebrew/English title (exact normalized first, then `difflib.get_close_matches` at 0.85 with a length-similarity guard to prevent absurd matches like "מומיה" → "סופר מריו גלקסי").
8. **Write `data/movies.json`**, read by the Flask app.

---

## The UI

- **Date tabs** — horizontally scrollable, one per date Planet has screenings, with the Hebrew day-of-week letter above each.
- **Sort toggle** — IMDB score descending (default) or title A→Z.
- **Movie cards** — poster, IMDB badge (⭐ X.Y), Hebrew title prominent + English subtitle, genre chips, runtime · rating, first line of synopsis.
- **Hover overlay** — full synopsis, directors, cast, language, runtime, both seret and IMDB scores.
- **Showtime pills** per selected date (hidden on cards that don't play on that date).
- **Poster fallback chain** — planet URL → seret `og:image` (with `referrerPolicy="no-referrer"` to bypass seret's Referer block) → OMDB poster → colored initial tile.

---

## Quickstart

```bash
git clone https://github.com/bissli82/planet-cinema-scraper.git
cd planet-cinema-scraper
cp .env.example .env
# Optional: edit .env and add your free OMDB key (omdbapi.com, 1000/day).
# Without a key, everything still works — only the title-search
# fallback for films lacking an IMDB widget is skipped.
docker compose up -d --build
open http://localhost:5000
```

The scraper runs automatically on startup (if `data/movies.json` is missing or stale) and then on cron at 07:00 and 19:00 Asia/Jerusalem.

### Manual refresh

```bash
curl -X POST http://localhost:5000/api/refresh
```

### Fresh rebuild (picks up code changes)

```bash
docker compose up -d --build
```

---

## Project layout

```
planet-cinema-scraper/
├── docker-compose.yml
├── Dockerfile                  # python:3.10-slim + lxml deps
├── requirements.txt
├── .env.example                # OMDB_API_KEY=
├── scraper/
│   ├── main.py                 # orchestrator + APScheduler
│   ├── planetcinema.py         # Vista Cinema API client (cinema 1025)
│   ├── planet_details.py       # synopsis/cast scrape for planet-only films
│   ├── seret.py                # seret.co.il metadata scraper
│   ├── omdb.py                 # OMDB by-id + by-title-year fallbacks
│   ├── imdb_scores.py          # IMDB public dataset stream-parser
│   └── merger.py               # fuzzy title match + schema assembly
├── web/
│   ├── app.py                  # Flask: / and /api/movies, /api/refresh
│   └── templates/index.html    # single-page Hebrew RTL UI
└── data/                       # volume-mounted; caches + movies.json
    ├── movies.json             # served to the UI
    ├── imdb_ratings.tsv.gz     # IMDB dataset (24h TTL)
    ├── imdb_cache.json         # resolved scores per imdb_id
    ├── omdb_cache.json         # OMDB by-id payloads
    ├── omdb_title_cache.json   # seret_id → imdb_id (title search)
    └── planetcinema_endpoint.json  # discovered Vista API URL
```

All `data/*.json` and the IMDB dump are gitignored — they regenerate on next scrape.

---

## Configuration

| env var          | purpose                                                                     | required |
|------------------|-----------------------------------------------------------------------------|----------|
| `OMDB_API_KEY`   | Resolve missing IMDB IDs by title+year when seret doesn't embed the widget. | No       |

All other tuning (cinema ID, scrape times, date window) lives in code — Ayalon's cinema code `1025` and the twice-daily cron are intentional constants, not knobs.

---

## Design notes & gotchas

- **IMDB public dataset, not Cinemagoer.** The original plan was Cinemagoer; it returned 0 hits from inside Docker because IMDB's AWS WAF blocks cloud IPs. The public dataset is a single 8MB gzip refreshed daily — perfect for this workload.
- **Seret poster source matters.** `video#seretPlayer[poster]` is shared across seret pages and frequently points to _the wrong film_ (the מומיה page was serving Super Mario's poster). Use `og:image` — it's the only per-film reliable source.
- **Seret blocks external Referer on poster image requests.** Server returns 200, but the browser gets 403 because of the Referer check. Fix: `<img referrerpolicy="no-referrer">`.
- **Planet detail pages are SPAs** but their initial HTML contains embedded JSON with the synopsis. We walk the string (not regex) to handle escaped quotes correctly.
- **Fuzzy title matching is tight on purpose.** Cutoff 0.85 with a length-similarity guard. Too loose and you get "מומיה" ↔ "סופר מריו גלקסי" — a real failure I hit.
- **Twice-daily cron is enough.** Planet publishes new week's showtimes on Thursdays; intra-day changes are rare. A 07:00 / 19:00 schedule catches everything with minimal load on the upstream sites.

---

## Deploying to a VPS

1. Point a subdomain at your VPS.
2. `git clone` + `docker compose up -d --build` on the server.
3. Put nginx (or Caddy) in front for TLS and reverse-proxy `:80/:443 → :5000`.
4. Set `OMDB_API_KEY` in `.env` on the server.

---

## Release notes

### 2026-04-19
- Language (שפה) now shown in hover overlay and as a chip on each card — sourced from OMDB's `Language` field with seret.co.il as primary.
- Planet Cinema and IMDb link icons moved to the bottom of the hover overlay.

### 2026-04-18
- IMDb link badge added to hover overlay, next to the Planet Cinema ticket icon.
- Planet Cinema ticket-link icon now deep-links to the active date's showtime listing instead of the generic film page.
- Planet Cinema ticket-link icon added to hover overlay.

### 2026-04-17 — initial release
- Single Docker container serving a Hebrew RTL dashboard for Planet Cinema Ayalon.
- Scrapes showtimes from Vista Cinema API, metadata from seret.co.il, IMDB scores from the official public dataset, and OMDB for poster/plot/language enrichment.
- Date tabs, IMDB sort, genre filter, hover overlay with full cast/synopsis/showtimes.

---

## License

Personal project — no explicit license. Use the code as reference; don't redistribute the scraped data.
