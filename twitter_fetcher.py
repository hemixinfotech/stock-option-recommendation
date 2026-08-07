"""
twitter_fetcher.py
------------------
Fetches stock option recommendation tweets from Twitter/X API v2
for Indian equity markets (NSE/BSE).

Parses tweets to extract:
  - Stock symbol / instrument name
  - Option type (CE / PE or BUY / SELL)
  - Buy/Entry price
  - Target price(s)
  - Stop-loss price
  - Author info (name, handle, follower count, avatar)
  - Tweet timestamp & URL
  - Time horizon: today | tomorrow | monthly
"""

import os
import re
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import tweepy
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Twitter client
# ---------------------------------------------------------------------------

def get_twitter_client() -> tweepy.Client:
    bearer = os.getenv("TWITTER_BEARER_TOKEN", "")
    if not bearer or bearer == "your_bearer_token_here":
        raise ValueError(
            "TWITTER_BEARER_TOKEN not set. "
            "Copy .env.example → .env and fill in your credentials."
        )
    client = tweepy.Client(
        bearer_token=bearer,
        consumer_key=os.getenv("TWITTER_API_KEY"),
        consumer_secret=os.getenv("TWITTER_API_SECRET"),
        access_token=os.getenv("TWITTER_ACCESS_TOKEN"),
        access_token_secret=os.getenv("TWITTER_ACCESS_TOKEN_SECRET"),
        wait_on_rate_limit=True,
    )
    return client


# ---------------------------------------------------------------------------
# Search queries for Indian equity option recommendations
# ---------------------------------------------------------------------------

# Curated search terms that typically accompany option tip tweets in India
SEARCH_QUERIES = [
    # NIFTY / BANKNIFTY options
    "(NIFTY OR BANKNIFTY) (CE OR PE) (target OR SL OR \"stop loss\" OR buy) -is:retweet lang:en",
    "(NIFTY OR BANKNIFTY) (CE OR PE) (target OR SL OR \"stop loss\" OR buy) -is:retweet lang:hi",
    # General NSE/BSE equity option tips
    "(NSE OR BSE) (CE OR PE) (target OR SL OR \"stop loss\" OR buy) -is:retweet",
    # Stock option calls
    "(#optionstrading OR #optionscall OR #stockoptions) (target OR SL OR buy) -is:retweet",
    # Indian market specific hashtags
    "(#NIFTY50 OR #BankNifty OR #NSE) (buy OR sell) (target OR SL) -is:retweet",
]

# ---------------------------------------------------------------------------
# Regex patterns to extract option data from tweet text
# ---------------------------------------------------------------------------

# Stock/Instrument name: e.g. NIFTY, BANKNIFTY, RELIANCE, HDFCBANK, TCS
SYMBOL_PATTERN = re.compile(
    r"\b(NIFTY(?:50|BANK|IT|MIDCAP)?|BANKNIFTY|SENSEX|"
    r"RELIANCE|HDFCBANK|TCS|INFY|WIPRO|ICICIBANK|SBIN|"
    r"BAJFINANCE|AXISBANK|KOTAKBANK|TATAMOTORS|MARUTI|"
    r"HINDUNILVR|ITC|LT|SUNPHARMA|DRREDDY|CIPLA|"
    r"TITAN|ADANIPORTS|ADANIENT|POWERGRID|NTPC|ONGC|"
    r"COALINDIA|TECHM|HCLTECH|ASIANPAINT|ULTRACEMCO|"
    r"BAJAJFINSV|NESTLEIND|BRITANNIA|EICHERMOT|HEROMOTOCO|"
    r"[A-Z]{3,10})\b",
    re.IGNORECASE,
)

# Option type: CE (Call) or PE (Put)
OPTION_TYPE_PATTERN = re.compile(r"\b(CE|PE)\b", re.IGNORECASE)

# Strike price: e.g. "22500 CE", "47000 PE"
STRIKE_PATTERN = re.compile(
    r"\b(\d{4,6})\s*(CE|PE)\b", re.IGNORECASE
)

# Buy/Entry price patterns
BUY_PRICE_PATTERN = re.compile(
    r"(?:buy(?:ing)?|entry|cmp|ltp|@)\s*(?:around|near|@|rs\.?|₹)?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

# Target price patterns (first match = T1, second = T2)
TARGET_PATTERN = re.compile(
    r"(?:tgt|target|t1|t2|tp)\s*[:\-]?\s*(?:rs\.?|₹)?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

# Stop-loss patterns
SL_PATTERN = re.compile(
    r"(?:sl|stoploss|stop\s*loss|slw?)\s*[:\-]?\s*(?:rs\.?|₹)?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

# Time horizon keywords
TODAY_KEYWORDS = re.compile(
    r"\b(today|intraday|day\s*trade|dtrade|for\s*today|eod)\b", re.IGNORECASE
)
TOMORROW_KEYWORDS = re.compile(
    r"\b(tomorrow|tmrw|next\s*day|positional\s*day)\b", re.IGNORECASE
)
MONTHLY_KEYWORDS = re.compile(
    r"\b(monthly|this\s*month|expiry|weekly|swing)\b", re.IGNORECASE
)

# Expiry date pattern: e.g. "08AUG", "15AUG25"
EXPIRY_PATTERN = re.compile(
    r"\b(\d{1,2}(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(?:\d{2})?)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Core parsing
# ---------------------------------------------------------------------------

def parse_tweet(text: str) -> dict:
    """Extract structured option data from raw tweet text."""
    text_upper = text.upper()

    # Strike + option type (most reliable combo)
    strike_matches = STRIKE_PATTERN.findall(text_upper)
    strike_price = strike_matches[0][0] if strike_matches else None
    option_type = strike_matches[0][1].upper() if strike_matches else None

    # Fallback: option type alone
    if not option_type:
        ot = OPTION_TYPE_PATTERN.search(text_upper)
        option_type = ot.group(1).upper() if ot else None

    # Symbol / instrument
    all_symbols = SYMBOL_PATTERN.findall(text_upper)
    # Prefer well-known names; deduplicate while keeping order
    seen, symbols = set(), []
    for s in all_symbols:
        su = s.upper()
        if su not in seen:
            seen.add(su)
            symbols.append(su)
    symbol = symbols[0] if symbols else None

    # Prices
    buy_matches = BUY_PRICE_PATTERN.findall(text)
    buy_price = float(buy_matches[0]) if buy_matches else None

    tgt_matches = TARGET_PATTERN.findall(text)
    targets = [float(t) for t in tgt_matches[:2]]

    sl_matches = SL_PATTERN.findall(text)
    stop_loss = float(sl_matches[0]) if sl_matches else None

    # Time horizon
    if TODAY_KEYWORDS.search(text):
        horizon = "today"
    elif TOMORROW_KEYWORDS.search(text):
        horizon = "tomorrow"
    elif MONTHLY_KEYWORDS.search(text):
        horizon = "monthly"
    else:
        # Default based on whether expiry is present
        horizon = "monthly" if EXPIRY_PATTERN.search(text_upper) else "today"

    # Expiry
    expiry_match = EXPIRY_PATTERN.search(text_upper)
    expiry = expiry_match.group(1) if expiry_match else None

    return {
        "symbol": symbol,
        "strike_price": strike_price,
        "option_type": option_type,
        "buy_price": buy_price,
        "targets": targets,
        "stop_loss": stop_loss,
        "horizon": horizon,
        "expiry": expiry,
    }


def determine_sentiment(option_type: Optional[str]) -> str:
    if not option_type:
        return "NEUTRAL"
    return "BULLISH" if option_type.upper() == "CE" else "BEARISH"


# ---------------------------------------------------------------------------
# Fetch from Twitter API
# ---------------------------------------------------------------------------

def fetch_recommendations(max_per_query: int = 30) -> list[dict]:
    """
    Fetch & parse option recommendation tweets from Twitter.
    Returns a list of enriched recommendation dicts.
    """
    try:
        client = get_twitter_client()
    except ValueError as e:
        logger.error(str(e))
        return []

    results = []
    seen_ids = set()

    for query in SEARCH_QUERIES:
        logger.info("Searching: %s", query)
        try:
            response = client.search_recent_tweets(
                query=query,
                max_results=min(max_per_query, 100),
                tweet_fields=[
                    "created_at",
                    "public_metrics",
                    "author_id",
                    "entities",
                    "context_annotations",
                ],
                user_fields=[
                    "name",
                    "username",
                    "public_metrics",
                    "profile_image_url",
                    "description",
                    "verified",
                ],
                expansions=["author_id"],
            )
        except tweepy.errors.TweepyException as exc:
            logger.warning("Twitter API error for query [%s]: %s", query, exc)
            continue

        if not response.data:
            logger.info("No tweets returned for this query.")
            continue

        # Build user lookup dict
        user_lookup: dict[int, dict] = {}
        if response.includes and response.includes.get("users"):
            for u in response.includes["users"]:
                metrics = u.public_metrics or {}
                user_lookup[u.id] = {
                    "name": u.name,
                    "handle": f"@{u.username}",
                    "username": u.username,
                    "followers": metrics.get("followers_count", 0),
                    "following": metrics.get("following_count", 0),
                    "tweet_count": metrics.get("tweet_count", 0),
                    "profile_image_url": getattr(u, "profile_image_url", ""),
                    "description": getattr(u, "description", ""),
                    "verified": getattr(u, "verified", False),
                }

        for tweet in response.data:
            if tweet.id in seen_ids:
                continue
            seen_ids.add(tweet.id)

            parsed = parse_tweet(tweet.text)

            # Skip tweets with no useful parsed data
            if not parsed["symbol"] and not parsed["strike_price"]:
                continue

            author = user_lookup.get(tweet.author_id, {})
            metrics = tweet.public_metrics or {}

            created_at = tweet.created_at
            if created_at and created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)

            rec = {
                "id": str(tweet.id),
                "text": tweet.text,
                "tweet_url": f"https://twitter.com/{author.get('username', 'i')}/status/{tweet.id}",
                "created_at": created_at.isoformat() if created_at else datetime.now(timezone.utc).isoformat(),
                # Parsed option data
                **parsed,
                "sentiment": determine_sentiment(parsed.get("option_type")),
                # Engagement
                "likes": metrics.get("like_count", 0),
                "retweets": metrics.get("retweet_count", 0),
                "replies": metrics.get("reply_count", 0),
                # Author info
                "author": author,
            }
            results.append(rec)

    logger.info("Total recommendations fetched & parsed: %d", len(results))
    return results


# ---------------------------------------------------------------------------
# Demo / mock data (used when no API keys are configured)
# ---------------------------------------------------------------------------

def get_mock_data() -> list[dict]:
    """
    Returns realistic mock data so the UI can be previewed without API keys.
    """
    now_ist = datetime.now(timezone.utc)
    mock = [
        {
            "id": "mock_001",
            "text": "🔥 BANKNIFTY 51000 CE Buy @ 180-190\n🎯 Target: T1-240, T2-300\n🛑 SL: 140\nFor Today Intraday\n#BankNifty #NSE #optionstrading",
            "tweet_url": "https://twitter.com/example",
            "created_at": now_ist.isoformat(),
            "symbol": "BANKNIFTY",
            "strike_price": "51000",
            "option_type": "CE",
            "buy_price": 185.0,
            "targets": [240.0, 300.0],
            "stop_loss": 140.0,
            "horizon": "today",
            "expiry": "08AUG",
            "sentiment": "BULLISH",
            "likes": 342,
            "retweets": 87,
            "replies": 23,
            "author": {
                "name": "NSE Options Guru",
                "handle": "@NSEOptionsGuru",
                "username": "NSEOptionsGuru",
                "followers": 125430,
                "following": 870,
                "tweet_count": 18540,
                "profile_image_url": "",
                "description": "🇮🇳 SEBI Registered | Option Trader | 10+ yrs experience | NSE/BSE",
                "verified": True,
            },
        },
        {
            "id": "mock_002",
            "text": "NIFTY 24500 PE Buy near 95-100\nTgt 140 / 175\nSL 70\nIntraday trade for today\n#NIFTY50 #OptionsTrading #NSE",
            "tweet_url": "https://twitter.com/example",
            "created_at": now_ist.isoformat(),
            "symbol": "NIFTY",
            "strike_price": "24500",
            "option_type": "PE",
            "buy_price": 97.5,
            "targets": [140.0, 175.0],
            "stop_loss": 70.0,
            "horizon": "today",
            "expiry": "08AUG",
            "sentiment": "BEARISH",
            "likes": 189,
            "retweets": 45,
            "replies": 12,
            "author": {
                "name": "Rahul Option Trader",
                "handle": "@RahulOptionTrader",
                "username": "RahulOptionTrader",
                "followers": 67200,
                "following": 1200,
                "tweet_count": 9800,
                "profile_image_url": "",
                "description": "Option buyer | Intraday & Positional | NSE certified analyst",
                "verified": False,
            },
        },
        {
            "id": "mock_003",
            "text": "📈 RELIANCE 3100 CE buy @55 for tomorrow\nT1: 80 T2: 110\nSL: 38\nPositional call\n#Reliance #NSE #StockOptions",
            "tweet_url": "https://twitter.com/example",
            "created_at": now_ist.isoformat(),
            "symbol": "RELIANCE",
            "strike_price": "3100",
            "option_type": "CE",
            "buy_price": 55.0,
            "targets": [80.0, 110.0],
            "stop_loss": 38.0,
            "horizon": "tomorrow",
            "expiry": "29AUG",
            "sentiment": "BULLISH",
            "likes": 521,
            "retweets": 134,
            "replies": 41,
            "author": {
                "name": "Stock Market India",
                "handle": "@StockMarketIndia",
                "username": "StockMarketIndia",
                "followers": 312000,
                "following": 540,
                "tweet_count": 32100,
                "profile_image_url": "",
                "description": "Premium Stock & Option Tips | NSE BSE | SEBI Reg Research Analyst",
                "verified": True,
            },
        },
        {
            "id": "mock_004",
            "text": "HDFCBANK 1700 PE @ 28 for tomorrow\nSL 20, Tgt 45/65\nSwing call keep patience\n#HDFCBANK #BankingStocks #NSE",
            "tweet_url": "https://twitter.com/example",
            "created_at": now_ist.isoformat(),
            "symbol": "HDFCBANK",
            "strike_price": "1700",
            "option_type": "PE",
            "buy_price": 28.0,
            "targets": [45.0, 65.0],
            "stop_loss": 20.0,
            "horizon": "tomorrow",
            "expiry": "29AUG",
            "sentiment": "BEARISH",
            "likes": 98,
            "retweets": 22,
            "replies": 8,
            "author": {
                "name": "Priya Sharma Trading",
                "handle": "@PriyaSharmaTrading",
                "username": "PriyaSharmaTrading",
                "followers": 44100,
                "following": 320,
                "tweet_count": 5600,
                "profile_image_url": "",
                "description": "Technical Analyst | Option Strategies | 7+ years NSE trader",
                "verified": False,
            },
        },
        {
            "id": "mock_005",
            "text": "Monthly swing: TATAMOTORS 900 CE @ 35\nExpiry 28SEP | T1 60 T2 90 T3 130\nSL 22 | Strong momentum!\n#TataMotors #NSE #Swing",
            "tweet_url": "https://twitter.com/example",
            "created_at": now_ist.isoformat(),
            "symbol": "TATAMOTORS",
            "strike_price": "900",
            "option_type": "CE",
            "buy_price": 35.0,
            "targets": [60.0, 90.0],
            "stop_loss": 22.0,
            "horizon": "monthly",
            "expiry": "28SEP",
            "sentiment": "BULLISH",
            "likes": 734,
            "retweets": 201,
            "replies": 67,
            "author": {
                "name": "Vikram Option Expert",
                "handle": "@VikramOptionExpert",
                "username": "VikramOptionExpert",
                "followers": 189500,
                "following": 680,
                "tweet_count": 24300,
                "profile_image_url": "",
                "description": "SEBI RA | Monthly swing trader | NIFTY BANKNIFTY specialist",
                "verified": True,
            },
        },
        {
            "id": "mock_006",
            "text": "BANKNIFTY 51500 PE buying at 145\nSL 110, T1 185 T2 230\nFor today EOD trade\n#BankNifty #intraday #OptionsTrading",
            "tweet_url": "https://twitter.com/example",
            "created_at": now_ist.isoformat(),
            "symbol": "BANKNIFTY",
            "strike_price": "51500",
            "option_type": "PE",
            "buy_price": 145.0,
            "targets": [185.0, 230.0],
            "stop_loss": 110.0,
            "horizon": "today",
            "expiry": "08AUG",
            "sentiment": "BEARISH",
            "likes": 267,
            "retweets": 58,
            "replies": 19,
            "author": {
                "name": "Amit Kapoor FnO",
                "handle": "@AmitKapoorFnO",
                "username": "AmitKapoorFnO",
                "followers": 88700,
                "following": 980,
                "tweet_count": 14200,
                "profile_image_url": "",
                "description": "F&O Trader | BankNifty specialist | Intraday swing calls daily",
                "verified": False,
            },
        },
        {
            "id": "mock_007",
            "text": "📊 INFY 1850 CE for Monthly swing\nBuy @ 42, Expiry 28AUG\nT1: 68 T2: 95\nStop Loss: 28\n#Infosys #IT #NSE #swing",
            "tweet_url": "https://twitter.com/example",
            "created_at": now_ist.isoformat(),
            "symbol": "INFY",
            "strike_price": "1850",
            "option_type": "CE",
            "buy_price": 42.0,
            "targets": [68.0, 95.0],
            "stop_loss": 28.0,
            "horizon": "monthly",
            "expiry": "28AUG",
            "sentiment": "BULLISH",
            "likes": 412,
            "retweets": 93,
            "replies": 31,
            "author": {
                "name": "IT Sector Expert",
                "handle": "@ITSectorExpert",
                "username": "ITSectorExpert",
                "followers": 55600,
                "following": 410,
                "tweet_count": 7900,
                "profile_image_url": "",
                "description": "IT & Tech sector analyst | NSE options | Infosys TCS Wipro specialist",
                "verified": False,
            },
        },
        {
            "id": "mock_008",
            "text": "Positional for Tomorrow - NIFTY 24800 CE\nEntry: 65-70\nTarget: 100 / 140\nSL: 45\n#NIFTY #positional #NSEOptions",
            "tweet_url": "https://twitter.com/example",
            "created_at": now_ist.isoformat(),
            "symbol": "NIFTY",
            "strike_price": "24800",
            "option_type": "CE",
            "buy_price": 67.5,
            "targets": [100.0, 140.0],
            "stop_loss": 45.0,
            "horizon": "tomorrow",
            "expiry": "08AUG",
            "sentiment": "BULLISH",
            "likes": 156,
            "retweets": 38,
            "replies": 14,
            "author": {
                "name": "Nifty Positional Calls",
                "handle": "@NiftyPositional",
                "username": "NiftyPositional",
                "followers": 38900,
                "following": 290,
                "tweet_count": 6100,
                "profile_image_url": "",
                "description": "Positional & swing option calls | Nifty & Bank Nifty | Daily analysis",
                "verified": False,
            },
        },
    ]
    return mock


# ---------------------------------------------------------------------------
# Entry point for standalone testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json as _json

    bearer = os.getenv("TWITTER_BEARER_TOKEN", "")
    if not bearer or bearer == "your_bearer_token_here":
        print("No API key configured — showing mock data:\n")
        data = get_mock_data()
    else:
        print("Fetching from Twitter API…")
        data = fetch_recommendations()

    print(_json.dumps(data, indent=2, default=str))
    print(f"\nTotal recommendations: {len(data)}")
