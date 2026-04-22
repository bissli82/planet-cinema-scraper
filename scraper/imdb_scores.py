"""
IMDB score enrichment using IMDB's official public datasets.

https://developer.imdb.com/non-commercial-datasets/
  title.ratings.tsv.gz — updated daily, ~8MB, contains every title's rating.

No API key, no WAF blocking, no rate limits. Just download, parse, look up.
We refresh the file if the on-disk copy is older than 24 hours.
"""

import gzip
import io
import logging
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

DATASET_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"
DATA_DIR = Path(__file__).parent.parent / "data"
DATASET_FILE = DATA_DIR / "imdb_ratings.tsv.gz"
DATASET_TTL_SEC = 24 * 3600  # refresh daily


def _download_dataset() -> bool:
    """Download the ratings TSV if missing or stale. Returns True on success."""
    if DATASET_FILE.exists():
        age = time.time() - DATASET_FILE.stat().st_mtime
        if age < DATASET_TTL_SEC:
            return True
    try:
        logger.info("IMDB: downloading ratings dataset (~8MB)")
        r = requests.get(DATASET_URL, timeout=60)
        if r.status_code != 200:
            logger.warning("IMDB dataset download failed: %s", r.status_code)
            return False
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        DATASET_FILE.write_bytes(r.content)
        logger.info("IMDB: dataset saved (%d bytes)", len(r.content))
        return True
    except Exception as e:
        logger.warning("IMDB dataset download error: %s", e)
        return False


def _load_ratings(wanted_ids: set[str]) -> dict[str, dict]:
    """
    Stream-parse the TSV and return {imdb_id: {imdb_score, num_votes}} for
    any IDs in wanted_ids. Streaming keeps memory usage tiny.
    """
    if not DATASET_FILE.exists():
        return {}
    out: dict[str, dict] = {}
    try:
        with gzip.open(DATASET_FILE, "rt", encoding="utf-8") as f:
            next(f, None)  # skip header
            for line in f:
                tconst, _rest = line.split("\t", 1)
                if tconst in wanted_ids:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) >= 3:
                        try:
                            out[tconst] = {
                                "imdb_score": float(parts[1]),
                                "num_votes": int(parts[2]),
                            }
                        except ValueError:
                            continue
                    if len(out) == len(wanted_ids):
                        break
    except Exception as e:
        logger.warning("IMDB dataset parse error: %s", e)
    return out


def enrich_imdb(imdb_ids: list[str], existing_cache: dict[str, dict]) -> dict[str, dict]:
    """
    Look up IMDB scores for the given IDs via the public dataset.
    Returns updated cache keyed by imdb_id.
    """
    cache = dict(existing_cache)
    wanted = {i for i in imdb_ids if i and not cache.get(i, {}).get("imdb_score")}
    if not wanted:
        return cache

    if not _download_dataset():
        return cache

    found = _load_ratings(wanted)
    for imdb_id, data in found.items():
        cache[imdb_id] = {**cache.get(imdb_id, {}), **data}

    logger.info("IMDB: %d/%d scores resolved from dataset", len(found), len(wanted))
    return cache
