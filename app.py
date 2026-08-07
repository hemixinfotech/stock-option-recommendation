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
from typing import Optional

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
# In-memory data store with lock-free read pattern
# ---------------------------------------------------------------------------

_CACHE: dict = {
    "data": [],
    "last_updated": None,
    "is_mock": True,
}
_CACHE_TTL_SECONDS = 900  # refresh every 15 minutes (avoids Twitter rate limits)
_cache_lock = threading.Lock()

# Seed cache immediately with mock data — API routes always respond instantly
_CACHE["data"] = get_mock_data()
_CACHE["is_mock"] = True
logger.info("Cache pre-seeded with %d mock entries.", len(_CACHE["data"]))


def _get_data() -> list:
    """Return current cached data instantly — never blocks."""
    with _cache_lock:
        return list(_CACHE["data"])


def _do_refresh() -> None:
    """
    Fetch live data and update cache.
    The slow network fetch happens OUTSIDE the lock so API routes are never blocked.
    """
    logger.info("Background: fetching live recommendations from Twitter…")
    try:
        fetched = fetch_recommendations()
    except Exception as exc:
        logger.error("fetch_recommendations failed: %s", exc)
        fetched = []

    with _cache_lock:
        if fetched:
            _CACHE["data"] = fetched
            _CACHE["is_mock"] = False
            logger.info("Cache updated: %d live recommendations.", len(fetched))
        else:
            logger.warning("No live data returned — retaining existing cache.")
            if not _CACHE["data"]:
                _CACHE["data"] = get_mock_data()
                _CACHE["is_mock"] = True
        _CACHE["last_updated"] = datetime.now(timezone.utc)


def _filter_data(data: list, horizon: Optional[str], instrument_type: Optional[str] = None, expiry_type: Optional[str] = None) -> list:
    if horizon and horizon in ("today", "tomorrow", "monthly"):
        data = [r for r in data if r.get("horizon") == horizon]
    if instrument_type and instrument_type in ("index", "stock"):
        data = [r for r in data if r.get("instrument_type") == instrument_type]
    if expiry_type and expiry_type in ("weekly", "monthly"):
        data = [r for r in data if r.get("expiry_type") == expiry_type]
    return data


def _sort_data(data: list, sort_by: str = "followers") -> list:
    """Sort by follower count or engagement score."""
    if sort_by == "engagement":
        return sorted(
            data,
            key=lambda r: r.get("likes", 0) + r.get("retweets", 0) * 3,
            reverse=True,
        )
    return sorted(
        data,
        key=lambda r: r.get("author", {}).get("followers", 0),
        reverse=True,
    )


# ---------------------------------------------------------------------------
# Background auto-refresh thread (fetch lives here — NOT in request handlers)
# ---------------------------------------------------------------------------

def _background_refresh():
    """Fetch live data immediately on startup, then every TTL seconds."""
    while True:
        _do_refresh()
        time.sleep(_CACHE_TTL_SECONDS)


_refresh_thread = threading.Thread(target=_background_refresh, daemon=True)
_refresh_thread.start()


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

@app.route("/api/recommendations")
def api_recommendations():
    horizon         = request.args.get("horizon")
    sort_by         = request.args.get("sort", "followers")
    search          = request.args.get("q", "").strip().upper()
    instrument_type = request.args.get("instrument_type")
    expiry_type     = request.args.get("expiry_type")

    data = _get_data()
    data = _filter_data(data, horizon, instrument_type, expiry_type)

    if search:
        data = [
            r for r in data
            if search in (r.get("symbol") or "").upper()
            or search in (r.get("text") or "").upper()
            or search in (r.get("author", {}).get("name") or "").upper()
        ]

    data = _sort_data(data, sort_by)

    with _cache_lock:
        is_mock     = _CACHE["is_mock"]
        last_upd    = _CACHE["last_updated"]

    return jsonify({
        "success": True,
        "is_mock": is_mock,
        "last_updated": last_upd.isoformat() if last_upd else None,
        "count": len(data),
        "recommendations": data,
    })


@app.route("/api/refresh")
def api_refresh():
    # Trigger a background refresh (non-blocking) and return current cache
    threading.Thread(target=_do_refresh, daemon=True).start()
    with _cache_lock:
        return jsonify({
            "success": True,
            "is_mock": _CACHE["is_mock"],
            "last_updated": _CACHE["last_updated"].isoformat() if _CACHE["last_updated"] else None,
            "count": len(_CACHE["data"]),
            "message": "Refresh triggered in background.",
        })


@app.route("/api/stats")
def api_stats():
    data = _get_data()
    today_cnt     = sum(1 for r in data if r.get("horizon") == "today")
    tomorrow_cnt  = sum(1 for r in data if r.get("horizon") == "tomorrow")
    monthly_cnt   = sum(1 for r in data if r.get("horizon") == "monthly")
    bullish_cnt   = sum(1 for r in data if r.get("sentiment") == "BULLISH")
    bearish_cnt   = sum(1 for r in data if r.get("sentiment") == "BEARISH")
    index_cnt     = sum(1 for r in data if r.get("instrument_type") == "index")
    stock_cnt     = sum(1 for r in data if r.get("instrument_type") == "stock")
    weekly_cnt    = sum(1 for r in data if r.get("expiry_type") == "weekly")
    monthly_exp_cnt = sum(1 for r in data if r.get("expiry_type") == "monthly")

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

    with _cache_lock:
        is_mock  = _CACHE["is_mock"]
        last_upd = _CACHE["last_updated"]

    return jsonify({
        "success": True,
        "total": len(data),
        "today": today_cnt,
        "tomorrow": tomorrow_cnt,
        "monthly": monthly_cnt,
        "bullish": bullish_cnt,
        "bearish": bearish_cnt,
        "index_count": index_cnt,
        "stock_count": stock_cnt,
        "weekly_count": weekly_cnt,
        "monthly_expiry_count": monthly_exp_cnt,
        "top_authors": top_authors_list,
        "is_mock": is_mock,
        "last_updated": last_upd.isoformat() if last_upd else None,
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
    port = int(os.getenv("PORT", os.getenv("FLASK_PORT", 5000)))
    debug = os.getenv("FLASK_DEBUG", "False").lower() == "true" if os.getenv("PORT") else True
    logger.info("Starting Stock Option Recommendation Server on port %d (debug=%s)", port, debug)
    app.run(host="0.0.0.0", port=port, debug=debug, use_reloader=False)
