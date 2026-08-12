"""
config.py
---------
Configuration management for the Telegram Stock Recommendation System.
Loads environment variables cleanly using python-dotenv with default fallbacks.
"""

import os
import logging
from typing import List
from dotenv import load_dotenv

load_dotenv()

# Setup system logger
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
)
logger = logging.getLogger("stock_recommendation")

# Telegram API Configuration
# Obtain credentials from https://my.telegram.org
raw_api_id = os.getenv("TELEGRAM_API_ID", "0").strip()
try:
    TELEGRAM_API_ID: int = int(raw_api_id)
except ValueError:
    TELEGRAM_API_ID: int = 0

TELEGRAM_API_HASH: str = os.getenv("TELEGRAM_API_HASH", "").strip()
TELEGRAM_PHONE: str = os.getenv("TELEGRAM_PHONE", "").strip()
TELEGRAM_SESSION_NAME: str = os.getenv("TELEGRAM_SESSION_NAME", "telegram_advisory_session").strip()
TELEGRAM_SESSION_STRING: str = os.getenv("TELEGRAM_SESSION_STRING", "").strip()

# Target Telegram Channels / Groups to monitor
# Supports usernames (e.g., "@nifty_traders") or channel IDs (e.g., "-100123456789")
raw_channels = os.getenv("TELEGRAM_CHANNELS", "@nifty_options_calls, @stock_advisory_india")
TELEGRAM_CHANNELS: List[str] = [
    ch.strip() for ch in raw_channels.split(",") if ch.strip()
]

# Gemini API Configuration for Hybrid Parsing
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL_NAME: str = os.getenv("GEMINI_MODEL_NAME", "gemini-2.5-flash")

# Database Configuration (SQLite default, PostgreSQL compatible)
DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///recommendations.db")

# Flask Web Server Settings
FLASK_PORT: int = int(os.getenv("FLASK_PORT", os.getenv("PORT", "5000")))
FLASK_DEBUG: bool = os.getenv("FLASK_DEBUG", "True").lower() in ("true", "1", "t")
