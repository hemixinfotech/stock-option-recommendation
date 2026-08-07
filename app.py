"""
app.py
------
Flask backend for the Stock Option Recommendation Dashboard.
Serves the web UI and provides REST API endpoints.

Endpoints:
  GET /                     → Main dashboard UI
  GET /api/recommendations  → All recommendations (JSON)
  GET /api/recommendations?horizon=today|tomorrow|monthly
  GET /api/refresh          → Force refresh from Twitter API
  GET /api/stats            → Summary statistics
"""

import os
import json
import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from functools import lru_cache

from flask import Flask, jsonify, request, render_template_string
from flask_cors import CORS
from dotenv import load_dotenv

from twitter_fetcher import fetch_recommendations, get_mock_data

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# ---------------------------------------------------------------------------
# In-memory data store with TTL-based caching
# ---------------------------------------------------------------------------

_CACHE: dict = {
    "data": [],
    "last_updated": None,
    "is_mock": False,
}
_CACHE_TTL_SECONDS = 300  # refresh every 5 minutes
_cache_lock = threading.Lock()


def _load_data(force: bool = False) -> list:
    """Load recommendations, using cache if fresh enough."""
    with _cache_lock:
        now = datetime.now(timezone.utc)
        last = _CACHE["last_updated"]
        cache_stale = (
            last is None
            or (now - last).total_seconds() > _CACHE_TTL_SECONDS
        )

        if force or cache_stale:
            # Always try live fetch first — ntscraper works without any API key.
            # Twitter API v2 is attempted as a secondary if ntscraper returns nothing.
            # Mock data is used only as the final fallback.
            logger.info("Fetching live recommendations (ntscraper → Twitter API → mock)…")
            fetched = fetch_recommendations()
            if fetched:
                _CACHE["data"] = fetched
                _CACHE["is_mock"] = False
                logger.info("Live data loaded: %d recommendations.", len(fetched))
            else:
                logger.warning("All live sources returned no data — using mock data.")
                _CACHE["data"] = get_mock_data()
                _CACHE["is_mock"] = True

            _CACHE["last_updated"] = now

        return _CACHE["data"]


def _filter_data(data: list, horizon: str | None) -> list:
    if horizon and horizon in ("today", "tomorrow", "monthly"):
        return [r for r in data if r.get("horizon") == horizon]
    return data


def _sort_data(data: list, sort_by: str = "followers") -> list:
    """Sort by follower count or engagement score."""
    if sort_by == "engagement":
        return sorted(
            data,
            key=lambda r: r.get("likes", 0) + r.get("retweets", 0) * 3,
            reverse=True,
        )
    # Default: sort by author follower count
    return sorted(
        data,
        key=lambda r: r.get("author", {}).get("followers", 0),
        reverse=True,
    )


# ---------------------------------------------------------------------------
# Background auto-refresh thread
# ---------------------------------------------------------------------------

def _background_refresh():
    while True:
        time.sleep(_CACHE_TTL_SECONDS)
        try:
            _load_data(force=True)
            logger.info("Background cache refreshed.")
        except Exception as exc:
            logger.error("Background refresh failed: %s", exc)


_refresh_thread = threading.Thread(target=_background_refresh, daemon=True)
_refresh_thread.start()


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

@app.route("/api/recommendations")
def api_recommendations():
    horizon = request.args.get("horizon")        # today | tomorrow | monthly | None
    sort_by = request.args.get("sort", "followers")  # followers | engagement
    search  = request.args.get("q", "").strip().upper()

    data = _load_data()
    data = _filter_data(data, horizon)

    if search:
        data = [
            r for r in data
            if search in (r.get("symbol") or "").upper()
            or search in (r.get("text") or "").upper()
            or search in (r.get("author", {}).get("name") or "").upper()
        ]

    data = _sort_data(data, sort_by)

    return jsonify({
        "success": True,
        "is_mock": _CACHE["is_mock"],
        "last_updated": _CACHE["last_updated"].isoformat() if _CACHE["last_updated"] else None,
        "count": len(data),
        "recommendations": data,
    })


@app.route("/api/refresh")
def api_refresh():
    _load_data(force=True)
    return jsonify({
        "success": True,
        "is_mock": _CACHE["is_mock"],
        "last_updated": _CACHE["last_updated"].isoformat() if _CACHE["last_updated"] else None,
        "count": len(_CACHE["data"]),
    })


@app.route("/api/stats")
def api_stats():
    data = _load_data()
    today_cnt     = sum(1 for r in data if r.get("horizon") == "today")
    tomorrow_cnt  = sum(1 for r in data if r.get("horizon") == "tomorrow")
    monthly_cnt   = sum(1 for r in data if r.get("horizon") == "monthly")
    bullish_cnt   = sum(1 for r in data if r.get("sentiment") == "BULLISH")
    bearish_cnt   = sum(1 for r in data if r.get("sentiment") == "BEARISH")

    top_authors = {}
    for r in data:
        author = r.get("author", {})
        handle = author.get("handle", "unknown")
        if handle not in top_authors:
            top_authors[handle] = {
                "name": author.get("name", ""),
                "handle": handle,
                "followers": author.get("followers", 0),
                "count": 0,
            }
        top_authors[handle]["count"] += 1

    top_authors_list = sorted(
        top_authors.values(), key=lambda x: x["followers"], reverse=True
    )[:5]

    return jsonify({
        "success": True,
        "total": len(data),
        "today": today_cnt,
        "tomorrow": tomorrow_cnt,
        "monthly": monthly_cnt,
        "bullish": bullish_cnt,
        "bearish": bearish_cnt,
        "top_authors": top_authors_list,
        "is_mock": _CACHE["is_mock"],
        "last_updated": _CACHE["last_updated"].isoformat() if _CACHE["last_updated"] else None,
    })


# ---------------------------------------------------------------------------
# Serve the frontend
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    with open(os.path.join(os.path.dirname(__file__), "templates", "index.html"), encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Railway/Render inject PORT automatically; fall back to 5000 locally
    port = int(os.getenv("PORT", os.getenv("FLASK_PORT", 5000)))
    # Disable debug in production (when PORT is set by the platform)
    debug = os.getenv("FLASK_DEBUG", "False").lower() == "true" if os.getenv("PORT") else True
    logger.info("Starting Stock Option Recommendation Server on port %d (debug=%s)", port, debug)
    # Pre-warm the cache
    _load_data()
    app.run(host="0.0.0.0", port=port, debug=debug, use_reloader=False)
