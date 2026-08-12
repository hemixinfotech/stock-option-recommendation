# ✈️ Telegram Stock & Option Advisory Aggregator Engine

A high-performance, asynchronous Python platform that monitors public and private Telegram channels/groups in real-time, extracts stock and option recommendations, standardizes raw advisory messages using a hybrid Regex + Gemini LLM parser, and categorizes signals into structured investment modules.

---

## 🏗️ Architecture & Core Components

1. **Telegram Async Listener (`telegram_listener.py`)**: Powered by **Telethon**. Listens asynchronously to incoming text, media captions, image updates, and PDF research reports with session persistence, automatic reconnection, and `FloodWait` rate-limit handling.
2. **Hybrid Signal Parser (`parser.py`)**: 
   - **Regex Engine**: High-speed pattern matching for standard calls (NIFTY/BANKNIFTY CE/PE options, BTST holds, targets, stop-loss).
   - **Gemini API LLM Fallback**: Invoked automatically for complex, unstructured, or long-form research text when regex confidence is low.
3. **Data Layer & Models (`models.py`, `storage.py`)**:
   - **Pydantic Schemas**: Strict runtime data validation and serialization.
   - **SQLAlchemy ORM**: SQLite/PostgreSQL storage with automatic SHA256 message deduplication.
4. **Web Dashboard & API (`app.py`, `templates/index.html`)**: Interactive dark-mode dashboard displaying filtered signals, stats, and a live message parser tester.
5. **CLI Launcher (`runner.py`)**: Single entrypoint to run the listener, dashboard, or test parsing standalone.

---

## 📂 Project Directory Structure

```
stock-option-recommendation/
├── config.py                # Environment configuration & settings loader
├── models.py                # Pydantic schemas & SQLAlchemy ORM database models
├── parser.py                # Hybrid Regex + Gemini LLM advisory signal parser engine
├── storage.py               # Database initialization, deduplication, & query helpers
├── telegram_listener.py     # Asynchronous Telethon channel & group listener
├── app.py                   # Flask REST API backend & web dashboard server
├── runner.py                # CLI runner (all, listener, web, or test mode)
├── templates/
│   └── index.html           # Dashboard UI with live signal cards & interactive tester
├── requirements.txt         # System Python dependencies
├── .env.example             # Environment configuration template
└── README.md                # System documentation & setup guide
```

---

## 🏷️ Investment Buckets & Standardized JSON Schema

Every ingested signal is validated into one of 4 buckets:
1. `OPTION`: NIFTY/BANKNIFTY/Stock Calls & Puts (CE/PE, strike price, entry, targets, stop-loss).
2. `BTST`: Buy Today, Sell Tomorrow short-term momentum holds.
3. `INVESTMENT`: Long-term fundamental stock recommendations (multi-month targets).
4. `REPORT`: Stock research notes, market updates, attached PDFs/images.

### Standardized JSON Format
```json
{
  "symbol": "TATASTEEL",
  "category": "OPTION",
  "action": "BUY",
  "option_type": "CE",
  "strike_price": 160.0,
  "expiry": "29AUG2024",
  "entry_range": [158.0, 160.0],
  "targets": [165.0, 170.0, 175.0],
  "stop_loss": 154.0,
  "raw_text": "BUY TATASTEEL 160 CE ENTRY 158-160 TGT 165/170/175 SL 154 EXPIRY 29AUG2024",
  "source_channel": "@OptionTradersHub",
  "timestamp": "2026-08-12T20:30:00+00:00"
}
```

---

## 🚀 Quick Start Guide

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure Credentials

Copy `.env.example` to `.env`:
```bash
copy .env.example .env
```

Edit `.env` and enter your credentials:
```env
# Get API_ID & API_HASH from https://my.telegram.org
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_PHONE=+919876543210
TELEGRAM_CHANNELS=@nifty_options_calls, @stock_advisory_india

# Optional Gemini API Key for LLM fallback parsing
GEMINI_API_KEY=your_gemini_api_key
```

### 3. Run the Platform

#### Run Web Dashboard + Telegram Listener Concurrently:
```bash
python runner.py --mode all
```

#### Run Web Dashboard Only:
```bash
python runner.py --mode web
```

#### Run Telegram Listener Only:
```bash
python runner.py --mode listener
```

#### Test Signal Parser via CLI:
```bash
python runner.py --mode test --text "BUY NIFTY 24500 CE ENTRY 140-145 SL 120 TGT 165/185"
```

---

## 🔌 REST API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `GET /` | GET | Main web dashboard UI |
| `GET /api/recommendations` | GET | List filtered recommendations (query params: `category`, `symbol`, `q`) |
| `GET /api/stats` | GET | Summary statistics across categories and channels |
| `POST /api/parse` | POST | Test raw signal text live (`{"text": "...", "source_channel": "..."}`) |

---

## 🛡️ License & Disclaimer

These recommendations are aggregated from Telegram channels for research and informational purposes only. This system does NOT provide SEBI-registered investment advice.
