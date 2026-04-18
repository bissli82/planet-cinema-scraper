import json
import logging
import os
import sys
import threading
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template

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

MOVIES_FILE = Path(__file__).parent.parent / "data" / "movies.json"

_scrape_lock = threading.Lock()


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
