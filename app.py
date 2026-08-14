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
    saved_record = None
    if parsed.category != "IGNORE":
        saved_record = save_recommendation(parsed)
    if saved_record:
        try:
            if saved_record.category != "REPORT":
                from notifier import send_telegram_alert
                send_telegram_alert(saved_record)
            else:
                logger.info("Skipping Telegram alert for REPORT category from API parse.")
        except Exception as notify_err:
            logger.error("Failed to send push alert from API parse: %s", notify_err)

    return jsonify({
        "success": True,
        "saved": saved_record is not None,
        "parsed": parsed.model_dump()
    })


@app.route("/api/test-alert", methods=["POST", "GET"])
def api_test_alert():
    """Test endpoint to trigger a sample push alert notification."""
    from notifier import send_telegram_alert_verbose
    test_data = {
        "symbol": "NIFTY",
        "category": "OPTION",
        "action": "BUY",
        "option_type": "CE",
        "strike_price": 24500,
        "entry_range": [140, 145],
        "targets": [165, 185],
        "stop_loss": 120,
        "timeframe": "INTRADAY",
        "source_channel": "UI Test Alert",
        "raw_text": "BUY NIFTY 24500 CE ENTRY 140-145 SL 120 TGT 165/185 <Test HTML & Special Characters>"
    }
    sent, details = send_telegram_alert_verbose(test_data)
    return jsonify({
        "success": sent,
        "message": details
    })


# ---------------------------------------------------------------------------
# Background Listener Launcher for Production WSGI / Live Hosting
# ---------------------------------------------------------------------------

import threading
from config import TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_CHANNELS, TELEGRAM_SESSION_STRING

_listener_thread = None
_listener_status = {"started": False, "error": None}


@app.route("/api/health")
def api_health():
    """Health check endpoint showing backend and Telegram listener status."""
    is_alive = _listener_thread is not None and _listener_thread.is_alive()
    err_msg = _listener_status.get("error")
    if not is_alive and not err_msg:
        if not TELEGRAM_SESSION_STRING:
            err_msg = "TELEGRAM_SESSION_STRING is missing in Railway environment variables."
        else:
            err_msg = "Listener process stopped or failed to connect."

    return jsonify({
        "success": True,
        "listener_running": is_alive,
        "channels_count": len(TELEGRAM_CHANNELS),
        "status": "active" if is_alive else "idle_or_disabled",
        "error": err_msg
    })


@app.route("/")
def index():
    """Serve main web application dashboard."""
    with open(os.path.join(os.path.dirname(__file__), "templates", "index.html"), encoding="utf-8") as f:
        return f.read()


def start_telegram_listener_background():
    global _listener_thread
    if _listener_thread is not None and _listener_thread.is_alive():
        return

    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH or TELEGRAM_API_ID == 0:
        logger.warning("Telegram API credentials not configured. Background listener will not start.")
        _listener_status["error"] = "Missing TELEGRAM_API_ID or TELEGRAM_API_HASH"
        return

    def _run():
        try:
            from telegram_listener import run_telegram_listener
            _listener_status["started"] = True
            _listener_status["error"] = None
            run_telegram_listener()
        except Exception as exc:
            _listener_status["error"] = str(exc)
            logger.error("Background Telegram Listener crashed or failed to start: %s", exc)

    _listener_thread = threading.Thread(target=_run, daemon=True, name="TelegramListenerThread")
    _listener_thread.start()
    logger.info("Background Telegram Listener thread launched.")

# Auto-start background listener when app module is imported/loaded in server
start_telegram_listener_background()


# ---------------------------------------------------------------------------
# Service Launcher
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    logger.info("Starting Web Dashboard on port %d (debug=%s)", FLASK_PORT, FLASK_DEBUG)
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=FLASK_DEBUG, use_reloader=False)

