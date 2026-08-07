# Stock Option Recommendation Dashboard - NSE/BSE

Real-time stock option recommendations for Indian equity markets sourced from Twitter/X.

## Features

- Twitter API v2 integration - searches for NIFTY, BANKNIFTY, NSE stock option tip tweets
- Smart parsing - extracts Buy Price, Target (T1/T2), Stop Loss, Strike Price, Option Type (CE/PE)
- Time horizon filters - Today (Intraday), Tomorrow (Positional), Monthly (Swing)
- Author info - analyst name, Twitter handle, follower count, verified status
- Market sentiment - live Bullish/Bearish gauge
- Auto-refresh every 5 minutes
- Demo mode - works with realistic mock data if no API key is provided

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure Twitter API credentials (optional)

```bash
copy .env.example .env
```

Edit `.env` and fill in:
```
TWITTER_BEARER_TOKEN=your_actual_bearer_token
TWITTER_API_KEY=your_api_key
TWITTER_API_SECRET=your_api_secret
TWITTER_ACCESS_TOKEN=your_access_token
TWITTER_ACCESS_TOKEN_SECRET=your_access_token_secret
```

Without credentials, the app runs with realistic demo data.

### 3. Run the server

```bash
python app.py
```

### 4. Open browser

Navigate to: http://localhost:5000

## Getting Twitter API Credentials

1. Go to https://developer.twitter.com/en/portal/dashboard
2. Create a new Project and App
3. Under Keys and Tokens, copy your Bearer Token (minimum required)

### API Tier Requirements

| Feature | Free | Basic ($100/mo) | Pro ($5000/mo) |
|---------|------|-----------------|----------------|
| Recent Search (7 days) | Limited | 10K tweets/mo | Unlimited |
| User lookup | Yes | Yes | Yes |
| Full archive search | No | No | Yes |

## Project Structure

```
stock-option-recommendation/
├── app.py                # Flask backend & API routes
├── twitter_fetcher.py    # Twitter API client + tweet parser
├── requirements.txt      # Python dependencies
├── .env.example          # Credentials template
├── .env                  # Your credentials (git-ignored)
└── templates/
    └── index.html        # Full dashboard UI
```

## API Endpoints

| Endpoint | Description |
|---------|-------------|
| GET / | Dashboard UI |
| GET /api/recommendations | All recommendations (JSON) |
| GET /api/recommendations?horizon=today | Filter by time horizon |
| GET /api/recommendations?sort=engagement | Sort by engagement |
| GET /api/recommendations?q=BANKNIFTY | Search by symbol |
| GET /api/refresh | Force refresh from Twitter |
| GET /api/stats | Summary statistics |

## Parsed Data Fields

Each recommendation includes:
- symbol - Stock symbol (NIFTY, BANKNIFTY, RELIANCE, etc.)
- option_type - CE (Call) or PE (Put)
- strike_price - Strike price
- buy_price - Entry/buy price
- targets - Array of target prices [T1, T2]
- stop_loss - Stop loss price
- horizon - today / tomorrow / monthly
- expiry - Option expiry date
- sentiment - BULLISH (CE) or BEARISH (PE)
- author.name - Analyst name
- author.handle - Twitter handle
- author.followers - Follower count
- author.verified - Verification status

## Disclaimer

These recommendations are from Twitter/X social media only. NOT SEBI-registered investment advice. Options trading involves significant risk. Always do your own research.
