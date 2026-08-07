"""
twitter_fetcher.py
------------------
Fetches stock option recommendation tweets for Indian equity markets (NSE/BSE).

Strategy (in order):
  1. Twitter Guest Token API  — FREE, no API key, uses Twitter's own internal API
  2. ntscraper (Nitter)       — FREE fallback, depends on Nitter instance availability
  3. Twitter API v2 (tweepy)  — requires Basic tier ($100/mo)
  4. Mock data                — always works as final fallback

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
import time
import logging
import random
from datetime import datetime, timezone
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Twitter Guest Token — uses the same bearer token Twitter's own website uses
# This allows free, unauthenticated search without any developer account
# ---------------------------------------------------------------------------

# Twitter's public bearer token (same one used by twitter.com frontend)
_TWITTER_PUBLIC_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I7BeRDkYzo%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

_GUEST_TOKEN: Optional[str] = None
_GUEST_TOKEN_TS: float = 0.0
_GUEST_TOKEN_TTL: float = 3600  # refresh every hour

_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://twitter.com/",
    "Origin": "https://twitter.com",
}


def _get_guest_token() -> Optional[str]:
    """Obtain a Twitter guest token (valid ~1 hour)."""
    global _GUEST_TOKEN, _GUEST_TOKEN_TS

    now = time.time()
    if _GUEST_TOKEN and (now - _GUEST_TOKEN_TS) < _GUEST_TOKEN_TTL:
        return _GUEST_TOKEN

    try:
        resp = requests.post(
            "https://api.twitter.com/1.1/guest/activate.json",
            headers={
                **_REQUEST_HEADERS,
                "Authorization": f"Bearer {_TWITTER_PUBLIC_BEARER}",
            },
            timeout=10,
        )
        resp.raise_for_status()
        token = resp.json().get("guest_token")
        if token:
            _GUEST_TOKEN = token
            _GUEST_TOKEN_TS = now
            logger.info("[GuestToken] Obtained new guest token.")
            return token
    except Exception as exc:
        logger.warning("[GuestToken] Failed to get guest token: %s", exc)

    return None


# ---------------------------------------------------------------------------
# Search queries for Indian equity option tweets
# ---------------------------------------------------------------------------

SEARCH_QUERIES = [
    "BANKNIFTY CE OR PE target SL buy -is:retweet",
    "NIFTY CE OR PE target SL intraday -is:retweet",
    "NIFTY OR BANKNIFTY CE PE buy target stoploss -is:retweet",
    "#BankNifty CE PE buy target SL -is:retweet",
    "#optionstrading NIFTY target SL -is:retweet",
    "NSE CE PE buy target SL swing -is:retweet",
]

# Simpler queries for v1.1 search (no operators like -is:retweet)
SEARCH_QUERIES_V1 = [
    "BANKNIFTY CE PE target SL",
    "NIFTY CE PE target SL intraday",
    "BANKNIFTY CE buy target stoploss",
    "NIFTY50 CE PE buy target SL",
    "#BankNifty CE PE target SL",
    "NSE options CE PE target SL",
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
    r"BAJAJFINSV|NESTLEIND|BRITANNIA|EICHERMOT|HEROMOTOCO)\b",
    re.IGNORECASE,
)

OPTION_TYPE_PATTERN = re.compile(r"\b(CE|PE)\b", re.IGNORECASE)
STRIKE_PATTERN      = re.compile(r"\b(\d{4,6})\s*(CE|PE)\b", re.IGNORECASE)
BUY_PRICE_PATTERN   = re.compile(
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

# Detect retweet
RT_PATTERN = re.compile(r"^RT\s+@", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Core parser
# ---------------------------------------------------------------------------

def parse_tweet(text: str) -> dict:
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


def _build_author(user: dict) -> dict:
    """Build a normalised author dict from Twitter v1.1 user object."""
    return {
        "name":              user.get("name", ""),
        "handle":            f"@{user.get('screen_name', '')}",
        "username":          user.get("screen_name", ""),
        "followers":         user.get("followers_count", 0),
        "following":         user.get("friends_count", 0),
        "tweet_count":       user.get("statuses_count", 0),
        "profile_image_url": user.get("profile_image_url_https", "").replace("_normal", "_400x400"),
        "description":       user.get("description", ""),
        "verified":          user.get("verified", False),
    }


def _parse_twitter_date(date_str: str) -> str:
    """Convert Twitter date string to ISO format."""
    try:
        dt = datetime.strptime(date_str, "%a %b %d %H:%M:%S +0000 %Y")
        return dt.replace(tzinfo=timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Strategy 1 — Twitter Guest Token (FREE, no developer account needed)
# ---------------------------------------------------------------------------

def fetch_via_guest_token(max_results: int = 50) -> list[dict]:
    """
    Search Twitter using the guest token API (Twitter v1.1 search/tweets.json).
    Completely free — uses Twitter's own public bearer token that their website uses.
    """
    guest_token = _get_guest_token()
    if not guest_token:
        logger.warning("[GuestToken] Could not obtain guest token.")
        return []

    headers = {
        **_REQUEST_HEADERS,
        "Authorization":  f"Bearer {_TWITTER_PUBLIC_BEARER}",
        "x-guest-token":  guest_token,
        "x-twitter-client-language": "en",
        "x-twitter-active-user": "yes",
    }

    results  = []
    seen_ids = set()

    for query in SEARCH_QUERIES_V1[:4]:
        logger.info("[GuestToken] Searching: %s", query)
        try:
            resp = requests.get(
                "https://api.twitter.com/1.1/search/tweets.json",
                headers=headers,
                params={
                    "q":          query,
                    "count":      min(max_results, 100),
                    "tweet_mode": "extended",
                    "lang":       "en",
                    "result_type": "recent",
                },
                timeout=15,
            )

            if resp.status_code == 429:
                logger.warning("[GuestToken] Rate limited. Sleeping 10s…")
                time.sleep(10)
                continue
            if resp.status_code in (401, 403):
                logger.warning("[GuestToken] Auth error %d — guest token expired, refreshing.", resp.status_code)
                _GUEST_TOKEN = None
                guest_token = _get_guest_token()
                if guest_token:
                    headers["x-guest-token"] = guest_token
                continue
            if not resp.ok:
                logger.warning("[GuestToken] HTTP %d for query [%s]", resp.status_code, query)
                continue

            data = resp.json()
            statuses = data.get("statuses", [])

        except requests.RequestException as exc:
            logger.warning("[GuestToken] Request failed [%s]: %s", query, exc)
            continue

        for tw in statuses:
            tweet_id = str(tw.get("id_str", tw.get("id", "")))
            if not tweet_id or tweet_id in seen_ids:
                continue
            seen_ids.add(tweet_id)

            # Skip retweets
            text = tw.get("full_text") or tw.get("text", "")
            if RT_PATTERN.match(text):
                continue

            parsed = parse_tweet(text)
            # Must have at least a symbol OR a strike price to be useful
            if not parsed["symbol"] and not parsed["strike_price"]:
                continue

            user   = tw.get("user", {})
            author = _build_author(user)

            rec = {
                "id":         tweet_id,
                "text":       text,
                "tweet_url":  f"https://x.com/{author['username']}/status/{tweet_id}",
                "created_at": _parse_twitter_date(tw.get("created_at", "")),
                **parsed,
                "sentiment":  determine_sentiment(parsed.get("option_type")),
                "likes":      tw.get("favorite_count", 0),
                "retweets":   tw.get("retweet_count", 0),
                "replies":    tw.get("reply_count", 0),
                "author":     author,
            }
            results.append(rec)

        # Small delay between queries to be respectful
        time.sleep(random.uniform(0.5, 1.5))

    logger.info("[GuestToken] Total parsed: %d", len(results))
    return results


# ---------------------------------------------------------------------------
# Strategy 2 — ntscraper (Nitter-based, works when Nitter instances are up)
# ---------------------------------------------------------------------------

NTSCRAPER_QUERIES = [
    "BANKNIFTY CE PE target SL",
    "NIFTY CE PE target SL",
    "#BankNifty CE PE buy target",
    "#optionstrading NIFTY target SL",
]


def fetch_via_ntscraper(max_results: int = 50) -> list[dict]:
    try:
        from ntscraper import Nitter
    except (ImportError, Exception) as e:
        logger.warning("[ntscraper] Not available: %s", e)
        return []

    results  = []
    seen_ids = set()

    try:
        scraper = Nitter(log_level=1, skip_instance_check=True)
    except Exception as exc:
        logger.warning("[ntscraper] Init failed: %s", exc)
        return []

    for query in NTSCRAPER_QUERIES[:3]:
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

            user     = tw.get("user", {})
            username = user.get("username", "unknown")
            name     = user.get("name", username)

            try:
                date_str   = tw.get("date", "")
                created_at = datetime.strptime(date_str, "%b %d, %Y · %I:%M %p UTC")
                created_at = created_at.replace(tzinfo=timezone.utc).isoformat()
            except Exception:
                created_at = datetime.now(timezone.utc).isoformat()

            stats = tw.get("stats", {})
            rec = {
                "id":         tweet_id,
                "text":       text,
                "tweet_url":  tw.get("link", f"https://x.com/{username}"),
                "created_at": created_at,
                **parsed,
                "sentiment":  determine_sentiment(parsed.get("option_type")),
                "likes":      int(stats.get("likes", 0) or 0),
                "retweets":   int(stats.get("retweets", 0) or 0),
                "replies":    int(stats.get("comments", 0) or 0),
                "author": {
                    "name":              name,
                    "handle":            f"@{username}",
                    "username":          username,
                    "followers":         int(user.get("followers", 0) or 0),
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
# Strategy 3 — Twitter API v2 (requires paid Basic tier)
# ---------------------------------------------------------------------------

def fetch_via_twitter_api(max_per_query: int = 30) -> list[dict]:
    try:
        import tweepy
    except ImportError:
        return []

    bearer = os.getenv("TWITTER_BEARER_TOKEN", "")
    if not bearer or bearer == "your_bearer_token_here":
        return []

    try:
        client = tweepy.Client(
            bearer_token=bearer,
            consumer_key=os.getenv("TWITTER_API_KEY"),
            consumer_secret=os.getenv("TWITTER_API_SECRET"),
            access_token=os.getenv("TWITTER_ACCESS_TOKEN"),
            access_token_secret=os.getenv("TWITTER_ACCESS_TOKEN_SECRET"),
            wait_on_rate_limit=True,
        )
    except Exception as exc:
        logger.warning("[Twitter API v2] Client init failed: %s", exc)
        return []

    results  = []
    seen_ids = set()
    api_queries = [
        "(NIFTY OR BANKNIFTY) (CE OR PE) (target OR SL) -is:retweet",
        "(NSE OR BSE) (CE OR PE) (target OR buy) -is:retweet",
    ]

    for query in api_queries:
        logger.info("[Twitter API v2] Searching: %s", query)
        try:
            response = client.search_recent_tweets(
                query=query,
                max_results=min(max_per_query, 100),
                tweet_fields=["created_at", "public_metrics", "author_id"],
                user_fields=["name", "username", "public_metrics",
                             "profile_image_url", "description", "verified"],
                expansions=["author_id"],
            )
        except Exception as exc:
            msg = str(exc)
            if "402" in msg or "403" in msg or "Payment" in msg:
                logger.warning("[Twitter API v2] 402/403 — Basic tier required. Skipping.")
                break
            logger.warning("[Twitter API v2] Error [%s]: %s", query, exc)
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
            ca = tweet.created_at
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

    logger.info("[Twitter API v2] Total parsed: %d", len(results))
    return results


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def fetch_recommendations(max_results: int = 50) -> list[dict]:
    """
    Fetch option recommendations using best available method:
      1. Twitter Guest Token API (free, no key needed)
      2. ntscraper via Nitter (free, depends on Nitter uptime)
      3. Twitter API v2 (paid Basic tier only)
    Returns [] if all strategies fail — caller falls back to mock data.
    """
    # Strategy 1: Guest Token (most reliable free method)
    logger.info("Strategy 1: Twitter Guest Token API…")
    data = fetch_via_guest_token(max_results)
    if data:
        logger.info("Guest Token strategy succeeded: %d results.", len(data))
        return data

    # Strategy 2: ntscraper
    logger.info("Strategy 2: ntscraper (Nitter)…")
    data = fetch_via_ntscraper(max_results)
    if data:
        logger.info("ntscraper strategy succeeded: %d results.", len(data))
        return data

    # Strategy 3: Twitter API v2
    logger.info("Strategy 3: Twitter API v2…")
    data = fetch_via_twitter_api(max_results)
    if data:
        logger.info("Twitter API v2 strategy succeeded: %d results.", len(data))
        return data

    logger.warning("All fetch strategies exhausted.")
    return []


# ---------------------------------------------------------------------------
# Mock data (always reliable fallback)
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
