# CLAUDE.md — context for Claude

Notes for anyone (Claude or human) picking up this repo.

## What this is

Flask web app that scrapes Israeli cinema data (seret.co.il +
planetcinema.co.il) and shows a Hebrew dashboard of what's playing at Planet
Ayalon. Data refreshes on a timer inside the container; the frontend is
vanilla JS + inline CSS, no build step.

## Layout

```
scraper/     pipeline (planet → seret → omdb → imdb dataset → merge)
web/         Flask app + single template
data/        cached JSON (movies.json, omdb_cache, imdb_cache, posters/)
```

`run_scrape()` in `scraper/main.py` is the orchestrator. Stages broadcast
progress via `update_progress()` so `/api/scrape-status` can drive the UI
progress bar.

## Local dev

```
docker compose up --build       # binds :5000 to host
```

The repo's `docker-compose.yml` is dev-only. Production is deployed via
GitHub Actions on push to `main`; its compose file and secrets live on the
deploy target and aren't in the repo.

## Endpoints

- `GET  /`                     → dashboard
- `GET  /api/movies`           → merged movies.json
- `GET  /api/scrape-status`    → progress bar state
- `GET  /poster/<planet_id>`   → proxy + on-disk cache for poster images
- `POST /api/refresh`          → trigger a scrape (POST-only on purpose —
                                 no accidental triggers from crawlers /
                                 link-unfurlers)

## Data-quality notes (learned the hard way)

- **IMDb scores.** seret.co.il shows two IMDb-looking numbers. The
  `IMDb 6.5/10` badge near the poster is the real one, rendered live by
  IMDb's widget JS (not in the static HTML we scrape). The
  `קהל / IMDb / כוונה` stat strip is seret's **internal** metric — NOT
  IMDb's rating. Don't scrape the stat strip. Real IMDb scores come from
  the public IMDb ratings dataset (`scraper/imdb_scores.py`), keyed by the
  `imdb_id` extracted from the widget markup. OMDB is a reasonable fallback
  when the dataset is missing an entry.
- **Title matching.** OMDB's `?t=` search is loose — it'll happily return
  an unrelated film for a generic name (e.g. `"Two Women"` hitting a
  French-Canadian 2025 film). `_titles_match()` in `scraper/omdb.py` guards
  against this via exact-normalize, subtitle-expansion, and a difflib ratio.
- **Poster hotlinking.** seret / planet occasionally 403 or stall when
  hotlinked. Posters go through `/poster/<planet_id>` which caches bytes on
  disk (`data/posters/`) and serves with a 30-day `Cache-Control: immutable`,
  so upstream flakiness can't randomly blank a thumbnail.

## Things to NOT do

- Force-push `main` without asking (auto-deploy runs against `main`).
- Make `/api/refresh` GET-accessible.
- Change the score-source chain without re-reading the IMDb note above.

## Open followups

- Swap Dockerfile base from
  `mcr.microsoft.com/playwright/python:v1.44.0-jammy` (~2 GB) to
  `python:3.11-slim` (~150 MB). Playwright is no longer used by the code —
  just still pinned in `requirements.txt`.
- Add auth to `/api/refresh` (e.g. via reverse-proxy middleware).
- Front Flask with gunicorn instead of the dev server (Werkzeug warns on
  startup; gunicorn is already in `requirements.txt`).
