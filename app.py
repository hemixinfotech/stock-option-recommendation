"""
app.py
------
Flask backend for the Stock & Option Advisory Aggregation Platform.
Serves the web dashboard and provides REST API endpoints for recommendations, statistics,
and live test parsing.
"""

import os
import logging
from typing import Optional

from flask import Flask, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv

from config import FLASK_PORT, FLASK_DEBUG
from storage import (
    init_db, get_recommendations, get_recommendation_stats,
    save_recommendation, delete_recommendation, clear_all_recommendations
)
from parser import SignalParser

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("stock_recommendation.app")

app = Flask(__name__)
CORS(app)

# Initialize Database & Signal Parser
init_db()
signal_parser = SignalParser(use_gemini_fallback=True)


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

@app.route("/api/recommendations")
def api_recommendations():
    """
    Fetch filtered recommendations from database.
    Query parameters:
    - category: OPTION | BTST | INVESTMENT | REPORT
    - symbol: e.g., NIFTY, TATASTEEL
    - q: search keyword in raw_text or symbol
    - limit: max items (default 100)
    """
    category = request.args.get("category")
    symbol = request.args.get("symbol")
    search = request.args.get("q", "").strip().upper()
    limit = int(request.args.get("limit", 100))

    data = get_recommendations(category=category, symbol=symbol, limit=limit)

    if search:
        data = [
            r for r in data
            if search in (r.get("symbol") or "").upper()
            or search in (r.get("raw_text") or "").upper()
            or search in (r.get("source_channel") or "").upper()
        ]

    return jsonify({
        "success": True,
        "count": len(data),
        "recommendations": data
    })


@app.route("/api/recommendations/<int:rec_id>", methods=["DELETE"])
def api_delete_recommendation(rec_id):
    """Delete a single recommendation by ID."""
    success = delete_recommendation(rec_id)
    return jsonify({"success": success, "id": rec_id})


@app.route("/api/recommendations/clear", methods=["POST", "DELETE"])
def api_clear_all_recommendations():
    """Clear all stored recommendations from database."""
    deleted_count = clear_all_recommendations()
    return jsonify({"success": True, "deleted_count": deleted_count})


@app.route("/api/stats")
def api_stats():
    """Summary statistics across recommendations."""
    stats = get_recommendation_stats()
    return jsonify({
        "success": True,
        "stats": stats
    })


@app.route("/api/parse", methods=["POST"])
def api_parse_test():
    """
    Test endpoint for real-time message parsing.
    Accepts JSON body: {"text": "...", "source_channel": "..."}
    """
    body = request.get_json() or {}
    text = body.get("text", "")
    source_channel = body.get("source_channel", "API Test")

    if not text:
        return jsonify({"success": False, "error": "Field 'text' is required"}), 400

    parsed = signal_parser.parse_message(text=text, source_channel=source_channel)
    # Persist parsed signal to database so it immediately appears in the UI
    saved_record = save_recommendation(parsed)
    return jsonify({
        "success": True,
        "saved": saved_record is not None,
        "parsed": parsed.model_dump()
    })


@app.route("/")
def index():
    """Serve main web application dashboard."""
    with open(os.path.join(os.path.dirname(__file__), "templates", "index.html"), encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Service Launcher
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    logger.info("Starting Web Dashboard on port %d (debug=%s)", FLASK_PORT, FLASK_DEBUG)
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=FLASK_DEBUG, use_reloader=False)
