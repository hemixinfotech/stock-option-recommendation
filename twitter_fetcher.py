"""
twitter_fetcher.py
------------------
Fetches stock option recommendation tweets for Indian equity markets (NSE/BSE).

Strategy (in order):
  1. ntscraper  — FREE, no API key needed, scrapes via Nitter instances
  2. Twitter API v2 (tweepy) — requires Basic tier ($100/mo) for search
  3. Mock data  — always works as final fallback

Parses tweets to extract:
  - Stock symbol / instrument name
  - Option type (CE / PE)
  - Buy/Entry price, Target price(s), Stop-loss
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

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Search terms for Indian equity option recommendations
# ---------------------------------------------------------------------------

# Queries for ntscraper (hashtag / keyword based)
NTSCRAPER_QUERIES = [
    "BANKNIFTY CE PE target SL",
    "NIFTY CE PE target SL",
    "NSE CE PE buy target stoploss",
    "#optionstrading NIFTY target SL",
    "#BankNifty CE PE buy target",
    "NIFTY50 CE PE intraday target",
    "RELIANCE CE PE target SL buy",
    "HDFCBANK CE PE target buy",
]

# Queries for Twitter API v2 (Basic tier only)
TWITTER_API_QUERIES = [
    "(NIFTY OR BANKNIFTY) (CE OR PE) (target OR SL OR \"stop loss\") -is:retweet",
    "(NSE OR BSE) (CE OR PE) (target OR SL OR buy) -is:retweet",
    "(#optionstrading OR #BankNifty OR #NIFTY50) (target OR SL OR buy) -is:retweet",
]

# ---------------------------------------------------------------------------
# Regex patterns to extract option data from tweet text
# ---------------------------------------------------------------------------

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

OPTION_TYPE_PATTERN = re.compile(r"\b(CE|PE)\b", re.IGNORECASE)

STRIKE_PATTERN = re.compile(r"\b(\d{4,6})\s*(CE|PE)\b", re.IGNORECASE)

BUY_PRICE_PATTERN = re.compile(
    r"(?:buy(?:ing)?|entry|cmp|ltp|@)\s*(?:around|near|@|rs\.?|₹)?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

TARGET_PATTERN = re.compile(
    r"(?:tgt|target|t1|t2|tp)\s*[:\-]?\s*(?:rs\.?|₹)?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

SL_PATTERN = re.compile(
    r"(?:sl|stoploss|stop\s*loss|slw?)\s*[:\-]?\s*(?:rs\.?|₹)?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

TODAY_KEYWORDS    = re.compile(r"\b(today|intraday|day\s*trade|dtrade|for\s*today|eod)\b", re.IGNORECASE)
TOMORROW_KEYWORDS = re.compile(r"\b(tomorrow|tmrw|next\s*day|positional\s*day)\b", re.IGNORECASE)
MONTHLY_KEYWORDS  = re.compile(r"\b(monthly|this\s*month|expiry|weekly|swing)\b", re.IGNORECASE)
EXPIRY_PATTERN    = re.compile(
    r"\b(\d{1,2}(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(?:\d{2})?)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Core parser (shared by all fetching strategies)
# ---------------------------------------------------------------------------

def parse_tweet(text: str) -> dict:
    """Extract structured option data from raw tweet text."""
    text_upper = text.upper()

    strike_matches = STRIKE_PATTERN.findall(text_upper)
    strike_price   = strike_matches[0][0] if strike_matches else None
    option_type    = strike_matches[0][1].upper() if strike_matches else None

    if not option_type:
        ot = OPTION_TYPE_PATTERN.search(text_upper)
        option_type = ot.group(1).upper() if ot else None

    seen, symbols = set(), []
    for s in SYMBOL_PATTERN.findall(text_upper):
        if s not in seen:
            seen.add(s)
            symbols.append(s)
    symbol = symbols[0] if symbols else None

    buy_matches = BUY_PRICE_PATTERN.findall(text)
    buy_price   = float(buy_matches[0]) if buy_matches else None

    tgt_matches = TARGET_PATTERN.findall(text)
    targets     = [float(t) for t in tgt_matches[:2]]

    sl_matches = SL_PATTERN.findall(text)
    stop_loss  = float(sl_matches[0]) if sl_matches else None

    if TODAY_KEYWORDS.search(text):
        horizon = "today"
    elif TOMORROW_KEYWORDS.search(text):
        horizon = "tomorrow"
    elif MONTHLY_KEYWORDS.search(text):
        horizon = "monthly"
    else:
        horizon = "monthly" if EXPIRY_PATTERN.search(text_upper) else "today"

    expiry_match = EXPIRY_PATTERN.search(text_upper)
    expiry = expiry_match.group(1) if expiry_match else None

    return {
        "symbol":       symbol,
        "strike_price": strike_price,
        "option_type":  option_type,
        "buy_price":    buy_price,
        "targets":      targets,
        "stop_loss":    stop_loss,
        "horizon":      horizon,
        "expiry":       expiry,
    }


def determine_sentiment(option_type: Optional[str]) -> str:
    if not option_type:
        return "NEUTRAL"
    return "BULLISH" if option_type.upper() == "CE" else "BEARISH"


# ---------------------------------------------------------------------------
# Strategy 1 — ntscraper (FREE, no API key required)
# ---------------------------------------------------------------------------

def fetch_via_ntscraper(max_results: int = 50) -> list[dict]:
    """
    Scrape tweets using ntscraper (Nitter-based, no API key needed).
    Falls back gracefully if ntscraper is unavailable.
    """
    try:
        from ntscraper import Nitter
    except ImportError:
        logger.warning("ntscraper not installed. Run: pip install ntscraper")
        return []

    results = []
    seen_ids = set()

    try:
        scraper = Nitter(log_level=1, skip_instance_check=False)
    except Exception as exc:
        logger.warning("ntscraper init failed: %s", exc)
        return []

    for query in NTSCRAPER_QUERIES[:4]:   # limit queries to avoid rate limits
        logger.info("[ntscraper] Searching: %s", query)
        try:
            tweets_data = scraper.get_tweets(query, mode="term", number=20)
            tweets = tweets_data.get("tweets", []) if isinstance(tweets_data, dict) else []
        except Exception as exc:
            logger.warning("[ntscraper] Query failed [%s]: %s", query, exc)
            continue

        for tw in tweets:
            tweet_id = tw.get("link", "") or str(len(results))
            if tweet_id in seen_ids:
                continue
            seen_ids.add(tweet_id)

            text = tw.get("text", "")
            if not text:
                continue

            parsed = parse_tweet(text)
            if not parsed["symbol"] and not parsed["strike_price"]:
                continue

            # Author info from ntscraper
            user     = tw.get("user", {})
            username = user.get("username", "unknown")
            name     = user.get("name", username)
            # ntscraper doesn't always return follower count; default 0
            followers = int(user.get("followers", 0) or 0)

            # Parse date
            date_str = tw.get("date", "")
            try:
                created_at = datetime.strptime(date_str, "%b %d, %Y · %I:%M %p UTC")
                created_at = created_at.replace(tzinfo=timezone.utc)
            except Exception:
                created_at = datetime.now(timezone.utc)

            stats = tw.get("stats", {})

            rec = {
                "id":         tweet_id,
                "text":       text,
                "tweet_url":  tw.get("link", f"https://x.com/{username}"),
                "created_at": created_at.isoformat(),
                **parsed,
                "sentiment":  determine_sentiment(parsed.get("option_type")),
                "likes":      int(stats.get("likes", 0) or 0),
                "retweets":   int(stats.get("retweets", 0) or 0),
                "replies":    int(stats.get("comments", 0) or 0),
                "author": {
                    "name":              name,
                    "handle":            f"@{username}",
                    "username":          username,
                    "followers":         followers,
                    "following":         int(user.get("following", 0) or 0),
                    "tweet_count":       int(user.get("tweets", 0) or 0),
                    "profile_image_url": user.get("avatar", ""),
                    "description":       user.get("bio", ""),
                    "verified":          bool(user.get("verified", False)),
                },
            }
            results.append(rec)

    logger.info("[ntscraper] Total parsed: %d", len(results))
    return results


# ---------------------------------------------------------------------------
# Strategy 2 — Twitter API v2 (requires Basic tier, $100/mo)
# ---------------------------------------------------------------------------

def get_twitter_client():
    import tweepy
    bearer = os.getenv("TWITTER_BEARER_TOKEN", "")
    if not bearer or bearer == "your_bearer_token_here":
        raise ValueError("TWITTER_BEARER_TOKEN not set.")
    return tweepy.Client(
        bearer_token=bearer,
        consumer_key=os.getenv("TWITTER_API_KEY"),
        consumer_secret=os.getenv("TWITTER_API_SECRET"),
        access_token=os.getenv("TWITTER_ACCESS_TOKEN"),
        access_token_secret=os.getenv("TWITTER_ACCESS_TOKEN_SECRET"),
        wait_on_rate_limit=True,
    )


def fetch_via_twitter_api(max_per_query: int = 30) -> list[dict]:
    """
    Fetch tweets using Twitter API v2.
    Requires Basic tier ($100/month) for search_recent_tweets.
    Returns empty list on 402/403 errors (free tier limitation).
    """
    try:
        import tweepy
        client = get_twitter_client()
    except (ValueError, ImportError) as exc:
        logger.warning("[Twitter API] Skipped: %s", exc)
        return []

    results = []
    seen_ids: set = set()

    for query in TWITTER_API_QUERIES:
        logger.info("[Twitter API] Searching: %s", query)
        try:
            response = client.search_recent_tweets(
                query=query,
                max_results=min(max_per_query, 100),
                tweet_fields=["created_at", "public_metrics", "author_id"],
                user_fields=["name", "username", "public_metrics", "profile_image_url",
                             "description", "verified"],
                expansions=["author_id"],
            )
        except tweepy.errors.Forbidden as exc:
            logger.warning("[Twitter API] 403 Forbidden (need Basic tier): %s", exc)
            break
        except tweepy.errors.TweepyException as exc:
            # 402 Payment Required also comes as TweepyException
            msg = str(exc)
            if "402" in msg or "403" in msg:
                logger.warning("[Twitter API] Payment required (need Basic tier). Skipping API.")
                break
            logger.warning("[Twitter API] Error [%s]: %s", query, exc)
            continue

        if not response or not response.data:
            continue

        user_lookup: dict = {}
        if response.includes and response.includes.get("users"):
            for u in response.includes["users"]:
                m = u.public_metrics or {}
                user_lookup[u.id] = {
                    "name":              u.name,
                    "handle":            f"@{u.username}",
                    "username":          u.username,
                    "followers":         m.get("followers_count", 0),
                    "following":         m.get("following_count", 0),
                    "tweet_count":       m.get("tweet_count", 0),
                    "profile_image_url": getattr(u, "profile_image_url", ""),
                    "description":       getattr(u, "description", ""),
                    "verified":          getattr(u, "verified", False),
                }

        for tweet in response.data:
            if tweet.id in seen_ids:
                continue
            seen_ids.add(tweet.id)

            parsed = parse_tweet(tweet.text)
            if not parsed["symbol"] and not parsed["strike_price"]:
                continue

            author  = user_lookup.get(tweet.author_id, {})
            metrics = tweet.public_metrics or {}
            ca      = tweet.created_at
            if ca and ca.tzinfo is None:
                ca = ca.replace(tzinfo=timezone.utc)

            rec = {
                "id":         str(tweet.id),
                "text":       tweet.text,
                "tweet_url":  f"https://x.com/{author.get('username','i')}/status/{tweet.id}",
                "created_at": (ca or datetime.now(timezone.utc)).isoformat(),
                **parsed,
                "sentiment":  determine_sentiment(parsed.get("option_type")),
                "likes":      metrics.get("like_count", 0),
                "retweets":   metrics.get("retweet_count", 0),
                "replies":    metrics.get("reply_count", 0),
                "author":     author,
            }
            results.append(rec)

    logger.info("[Twitter API] Total parsed: %d", len(results))
    return results


# ---------------------------------------------------------------------------
# Main entry point — tries each strategy in order
# ---------------------------------------------------------------------------

def fetch_recommendations(max_results: int = 50) -> list[dict]:
    """
    Fetch option recommendations using the best available method:
      1. ntscraper (free, no API key)
      2. Twitter API v2 (paid Basic tier)
      3. Returns empty list (caller falls back to mock data)
    """
    # --- Strategy 1: ntscraper (free) ---
    logger.info("Trying ntscraper (free, no API key)…")
    data = fetch_via_ntscraper(max_results)
    if data:
        logger.info("ntscraper returned %d results.", len(data))
        return data

    # --- Strategy 2: Twitter API v2 ---
    bearer = os.getenv("TWITTER_BEARER_TOKEN", "")
    if bearer and bearer != "your_bearer_token_here":
        logger.info("ntscraper empty — trying Twitter API v2…")
        data = fetch_via_twitter_api(max_results)
        if data:
            logger.info("Twitter API returned %d results.", len(data))
            return data

    logger.info("All fetch strategies exhausted — using mock data.")
    return []


# ---------------------------------------------------------------------------
# Demo / mock data (final fallback)
# ---------------------------------------------------------------------------

def get_mock_data() -> list[dict]:
    now_ist = datetime.now(timezone.utc)
    return [
        {
            "id": "mock_001",
            "text": "🔥 BANKNIFTY 51000 CE Buy @ 180-190\n🎯 Target: T1-240, T2-300\n🛑 SL: 140\nFor Today Intraday\n#BankNifty #NSE #optionstrading",
            "tweet_url": "https://x.com/example",
            "created_at": now_ist.isoformat(),
            "symbol": "BANKNIFTY", "strike_price": "51000", "option_type": "CE",
            "buy_price": 185.0, "targets": [240.0, 300.0], "stop_loss": 140.0,
            "horizon": "today", "expiry": "08AUG", "sentiment": "BULLISH",
            "likes": 342, "retweets": 87, "replies": 23,
            "author": {"name": "NSE Options Guru", "handle": "@NSEOptionsGuru", "username": "NSEOptionsGuru",
                       "followers": 125430, "following": 870, "tweet_count": 18540,
                       "profile_image_url": "", "description": "🇮🇳 SEBI Registered | Option Trader | 10+ yrs | NSE/BSE", "verified": True},
        },
        {
            "id": "mock_002",
            "text": "NIFTY 24500 PE Buy near 95-100\nTgt 140 / 175\nSL 70\nIntraday trade for today\n#NIFTY50 #OptionsTrading",
            "tweet_url": "https://x.com/example",
            "created_at": now_ist.isoformat(),
            "symbol": "NIFTY", "strike_price": "24500", "option_type": "PE",
            "buy_price": 97.5, "targets": [140.0, 175.0], "stop_loss": 70.0,
            "horizon": "today", "expiry": "08AUG", "sentiment": "BEARISH",
            "likes": 189, "retweets": 45, "replies": 12,
            "author": {"name": "Rahul Option Trader", "handle": "@RahulOptionTrader", "username": "RahulOptionTrader",
                       "followers": 67200, "following": 1200, "tweet_count": 9800,
                       "profile_image_url": "", "description": "Option buyer | Intraday & Positional | NSE certified", "verified": False},
        },
        {
            "id": "mock_003",
            "text": "📈 RELIANCE 3100 CE buy @55 for tomorrow\nT1: 80 T2: 110\nSL: 38\nPositional call\n#Reliance #NSE",
            "tweet_url": "https://x.com/example",
            "created_at": now_ist.isoformat(),
            "symbol": "RELIANCE", "strike_price": "3100", "option_type": "CE",
            "buy_price": 55.0, "targets": [80.0, 110.0], "stop_loss": 38.0,
            "horizon": "tomorrow", "expiry": "29AUG", "sentiment": "BULLISH",
            "likes": 521, "retweets": 134, "replies": 41,
            "author": {"name": "Stock Market India", "handle": "@StockMarketIndia", "username": "StockMarketIndia",
                       "followers": 312000, "following": 540, "tweet_count": 32100,
                       "profile_image_url": "", "description": "Premium Stock & Option Tips | SEBI Reg RA", "verified": True},
        },
        {
            "id": "mock_004",
            "text": "HDFCBANK 1700 PE @ 28 for tomorrow\nSL 20, Tgt 45/65\nSwing call\n#HDFCBANK #NSE",
            "tweet_url": "https://x.com/example",
            "created_at": now_ist.isoformat(),
            "symbol": "HDFCBANK", "strike_price": "1700", "option_type": "PE",
            "buy_price": 28.0, "targets": [45.0, 65.0], "stop_loss": 20.0,
            "horizon": "tomorrow", "expiry": "29AUG", "sentiment": "BEARISH",
            "likes": 98, "retweets": 22, "replies": 8,
            "author": {"name": "Priya Sharma Trading", "handle": "@PriyaSharmaTrading", "username": "PriyaSharmaTrading",
                       "followers": 44100, "following": 320, "tweet_count": 5600,
                       "profile_image_url": "", "description": "Technical Analyst | Option Strategies | 7+ yrs NSE", "verified": False},
        },
        {
            "id": "mock_005",
            "text": "Monthly swing: TATAMOTORS 900 CE @ 35\nExpiry 28SEP | T1 60 T2 90 T3 130\nSL 22\n#TataMotors #Swing",
            "tweet_url": "https://x.com/example",
            "created_at": now_ist.isoformat(),
            "symbol": "TATAMOTORS", "strike_price": "900", "option_type": "CE",
            "buy_price": 35.0, "targets": [60.0, 90.0], "stop_loss": 22.0,
            "horizon": "monthly", "expiry": "28SEP", "sentiment": "BULLISH",
            "likes": 734, "retweets": 201, "replies": 67,
            "author": {"name": "Vikram Option Expert", "handle": "@VikramOptionExpert", "username": "VikramOptionExpert",
                       "followers": 189500, "following": 680, "tweet_count": 24300,
                       "profile_image_url": "", "description": "SEBI RA | Monthly swing trader | NIFTY BANKNIFTY specialist", "verified": True},
        },
        {
            "id": "mock_006",
            "text": "BANKNIFTY 51500 PE buying at 145\nSL 110, T1 185 T2 230\nFor today EOD trade\n#BankNifty #intraday",
            "tweet_url": "https://x.com/example",
            "created_at": now_ist.isoformat(),
            "symbol": "BANKNIFTY", "strike_price": "51500", "option_type": "PE",
            "buy_price": 145.0, "targets": [185.0, 230.0], "stop_loss": 110.0,
            "horizon": "today", "expiry": "08AUG", "sentiment": "BEARISH",
            "likes": 267, "retweets": 58, "replies": 19,
            "author": {"name": "Amit Kapoor FnO", "handle": "@AmitKapoorFnO", "username": "AmitKapoorFnO",
                       "followers": 88700, "following": 980, "tweet_count": 14200,
                       "profile_image_url": "", "description": "F&O Trader | BankNifty specialist | Daily intraday calls", "verified": False},
        },
        {
            "id": "mock_007",
            "text": "📊 INFY 1850 CE for Monthly swing\nBuy @ 42, Expiry 28AUG\nT1: 68 T2: 95\nSL: 28\n#Infosys #NSE",
            "tweet_url": "https://x.com/example",
            "created_at": now_ist.isoformat(),
            "symbol": "INFY", "strike_price": "1850", "option_type": "CE",
            "buy_price": 42.0, "targets": [68.0, 95.0], "stop_loss": 28.0,
            "horizon": "monthly", "expiry": "28AUG", "sentiment": "BULLISH",
            "likes": 412, "retweets": 93, "replies": 31,
            "author": {"name": "IT Sector Expert", "handle": "@ITSectorExpert", "username": "ITSectorExpert",
                       "followers": 55600, "following": 410, "tweet_count": 7900,
                       "profile_image_url": "", "description": "IT & Tech sector analyst | NSE options", "verified": False},
        },
        {
            "id": "mock_008",
            "text": "Positional for Tomorrow - NIFTY 24800 CE\nEntry: 65-70\nTarget: 100 / 140\nSL: 45\n#NIFTY #NSEOptions",
            "tweet_url": "https://x.com/example",
            "created_at": now_ist.isoformat(),
            "symbol": "NIFTY", "strike_price": "24800", "option_type": "CE",
            "buy_price": 67.5, "targets": [100.0, 140.0], "stop_loss": 45.0,
            "horizon": "tomorrow", "expiry": "08AUG", "sentiment": "BULLISH",
            "likes": 156, "retweets": 38, "replies": 14,
            "author": {"name": "Nifty Positional Calls", "handle": "@NiftyPositional", "username": "NiftyPositional",
                       "followers": 38900, "following": 290, "tweet_count": 6100,
                       "profile_image_url": "", "description": "Positional & swing option calls | Nifty & Bank Nifty", "verified": False},
        },
    ]


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    data = fetch_recommendations()
    if not data:
        print("All live methods failed — showing mock data")
        data = get_mock_data()
    print(json.dumps(data[:2], indent=2, default=str))
    print(f"\nTotal: {len(data)}")
