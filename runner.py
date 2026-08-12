"""
runner.py
---------
Main entrypoint CLI runner for the Stock Recommendation Platform.
Supports:
1. Running Telegram Async Listener standalone
2. Running Flask Web Server standalone
3. Running both Listener and Web Server concurrently
4. Testing Signal Parser on sample raw text
"""

import sys
import argparse
import logging
import threading

from storage import init_db
from telegram_listener import run_telegram_listener
from parser import SignalParser
from app import app as flask_app, FLASK_PORT, FLASK_DEBUG

logger = logging.getLogger("stock_recommendation.runner")


def run_all():
    """Run Web Server and Telegram Listener in concurrent threads."""
    init_db()

    # Start Flask Web Server in daemon thread
    flask_thread = threading.Thread(
        target=lambda: flask_app.run(host="0.0.0.0", port=FLASK_PORT, debug=False, use_reloader=False),
        daemon=True
    )
    flask_thread.start()
    logger.info("Web Dashboard started on http://localhost:%d", FLASK_PORT)

    # Start Telegram Listener on main thread loop
    logger.info("Starting Telegram Listener service...")
    run_telegram_listener()


def test_parser(text: str):
    """Run SignalParser on a sample raw text input and print JSON result."""
    parser = SignalParser(use_gemini_fallback=True)
    res = parser.parse_message(text=text, source_channel="CLI_Test")
    import json
    print("\n--- Parsed JSON Output ---")
    print(json.dumps(res.model_dump(), indent=2))


def main():
    parser = argparse.ArgumentParser(description="Stock Recommendation Platform CLI")
    parser.add_argument(
        "--mode",
        choices=["listener", "web", "all", "test", "verify"],
        default="all",
        help="Mode to run: 'listener', 'web', 'all', 'test', 'verify' (Check Telegram channels)"
    )
    parser.add_argument(
        "--text",
        type=str,
        default="BUY NIFTY 24500 CE ENTRY 140-145 SL 120 TGT 165/185 EXPIRY 29AUG2024",
        help="Sample raw text message for test mode"
    )

    args = parser.parse_args()

    init_db()

    if args.mode == "listener":
        logger.info("Starting Telegram Listener service standalone...")
        run_telegram_listener()
    elif args.mode == "web":
        logger.info("Starting Flask Web Server on port %d...", FLASK_PORT)
        flask_app.run(host="0.0.0.0", port=FLASK_PORT, debug=FLASK_DEBUG, use_reloader=False)
    elif args.mode == "test":
        test_parser(args.text)
    elif args.mode == "verify":
        import asyncio
        from verify_channels import check_channels
        asyncio.run(check_channels())
    else:
        run_all()


if __name__ == "__main__":
    main()
