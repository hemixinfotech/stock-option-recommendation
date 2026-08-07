"""
twitter_fetcher.py
------------------
Fetches stock option recommendation posts for Indian equity markets (NSE/BSE).

Strategy (in order — all FREE, no API key required):
  1. StockTwits API       — public REST API, no key needed, built-in sentiment
  2. Reddit JSON API      — r/IndianStockMarket, r/IndiaInvestments (no auth needed)
  3. Twitter API v2       — only if Bearer Token set (requires paid Basic tier)
  4. Mock data            — always works as final fallback

Parses posts to extract:
  - Stock symbol, Option type (CE/PE), Strike price
  - Buy/Entry price, Target(s), Stop-loss
  - Author info (name, handle, followers)
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
# Regex patterns (shared across all sources)
# ---------------------------------------------------------------------------

SYMBOL_PATTERN = re.compile(
    r"\b(NIFTY(?:50|BANK|IT|MIDCAP)?|BANKNIFTY|SENSEX|"
    r"RELIANCE|HDFCBANK|TCS|INFY|WIPRO|ICICIBANK|SBIN|"
    r"BAJFINANCE|AXISBANK|KOTAKBANK|TATAMOTORS|MARUTI|"
    r"HINDUNILVR|ITC|LT|SUNPHARMA|DRREDDY|CIPLA|"
    r"TITAN|ADANIPORTS|ADANIENT|POWERGRID|NTPC|ONGC|"
    r"COALINDIA|TECHM|HCLTECH|ASIANPAINT|ULTRACEMCO|"
    r"BAJAJFINSV|NESTLEIND|EICHERMOT|HEROMOTOCO)\b",
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
TODAY_KEYWORDS    = re.compile(r"\b(today|intraday|day\s*trade|dtrade|eod)\b", re.IGNORECASE)
TOMORROW_KEYWORDS = re.compile(r"\b(tomorrow|tmrw|next\s*day|positional\s*day)\b", re.IGNORECASE)
MONTHLY_KEYWORDS  = re.compile(r"\b(monthly|this\s*month|expiry|weekly|swing)\b", re.IGNORECASE)
EXPIRY_PATTERN    = re.compile(
    r"\b(\d{1,2}(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(?:\d{2})?)\b",
    re.IGNORECASE,
)

_BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}


# ---------------------------------------------------------------------------
# Core parser
# ---------------------------------------------------------------------------

def parse_text(text: str) -> dict:
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

    buy_price = None
    bm = BUY_PRICE_PATTERN.findall(text)
    if bm:
        buy_price = float(bm[0])

    targets = [float(t) for t in TARGET_PATTERN.findall(text)[:2]]

    stop_loss = None
    sm = SL_PATTERN.findall(text)
    if sm:
        stop_loss = float(sm[0])

    if TODAY_KEYWORDS.search(text):
        horizon = "today"
    elif TOMORROW_KEYWORDS.search(text):
        horizon = "tomorrow"
    elif MONTHLY_KEYWORDS.search(text):
        horizon = "monthly"
    else:
        horizon = "monthly" if EXPIRY_PATTERN.search(text_upper) else "today"

    expiry_m = EXPIRY_PATTERN.search(text_upper)
    expiry = expiry_m.group(1) if expiry_m else None

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


def determine_sentiment(option_type: Optional[str], hint: str = "") -> str:
    if hint.lower() == "bullish":
        return "BULLISH"
    if hint.lower() == "bearish":
        return "BEARISH"
    if not option_type:
        return "NEUTRAL"
    return "BULLISH" if option_type.upper() == "CE" else "BEARISH"


def _is_useful(parsed: dict) -> bool:
    """A post must have at least a known symbol OR a strike price to be included."""
    return bool(parsed.get("symbol") or parsed.get("strike_price"))


# ---------------------------------------------------------------------------
# Strategy 1 — StockTwits Public API (completely free, no key)
# ---------------------------------------------------------------------------

# StockTwits symbols that cover Indian equity options
STOCKTWITS_SYMBOLS = [
    "NIFTY", "BANKNIFTY", "SENSEX",
    "RELIANCE", "HDFCBANK", "TCS", "INFY", "SBIN",
    "ICICIBANK", "AXISBANK", "TATAMOTORS", "WIPRO",
]

STOCKTWITS_SEARCH_TERMS = [
    "NIFTY CE PE target SL",
    "BANKNIFTY CE target stoploss",
    "NSE options CE PE buy target",
    "NIFTY50 intraday CE PE",
]


def fetch_via_stocktwits(max_results: int = 60) -> list[dict]:
    """
    Fetch from StockTwits public API.
    Endpoint: https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json
    Also uses /search/messages to find CE/PE option posts.
    No API key or account needed.
    """
    results  = []
    seen_ids = set()

    def _process_message(msg: dict, source_symbol: str = "") -> Optional[dict]:
        msg_id = str(msg.get("id", ""))
        if not msg_id or msg_id in seen_ids:
            return None
        seen_ids.add(msg_id)

        body = msg.get("body", "")
        if not body:
            return None

        # Only process posts that mention CE or PE (option trades)
        if not OPTION_TYPE_PATTERN.search(body):
            return None

        parsed = parse_text(body)
        if not _is_useful(parsed):
            return None

        user = msg.get("user", {})
        # StockTwits sentiment
        entities  = msg.get("entities", {}) or {}
        sentiment = (entities.get("sentiment") or {}).get("basic", "")

        username  = user.get("username", "stocktwits_user")
        followers = user.get("followers", 0) or 0
        verified  = bool(user.get("official", False) or user.get("verified", False))

        created_raw = msg.get("created_at", "")
        try:
            created_at = datetime.strptime(created_raw, "%Y-%m-%dT%H:%M:%SZ")
            created_at = created_at.replace(tzinfo=timezone.utc).isoformat()
        except Exception:
            created_at = datetime.now(timezone.utc).isoformat()

        likes = (msg.get("likes") or {}).get("total", 0) or 0

        return {
            "id":         f"st_{msg_id}",
            "text":       body,
            "tweet_url":  f"https://stocktwits.com/{username}/message/{msg_id}",
            "created_at": created_at,
            **parsed,
            "sentiment":  determine_sentiment(parsed.get("option_type"), sentiment),
            "likes":      likes,
            "retweets":   msg.get("reshares", {}).get("total", 0) if isinstance(msg.get("reshares"), dict) else 0,
            "replies":    msg.get("replies", {}).get("total", 0) if isinstance(msg.get("replies"), dict) else 0,
            "author": {
                "name":              user.get("name", username),
                "handle":            f"@{username}",
                "username":          username,
                "followers":         followers,
                "following":         user.get("following", 0) or 0,
                "tweet_count":       user.get("ideas", 0) or 0,
                "profile_image_url": user.get("avatar_url_ssl", "") or user.get("avatar_url", ""),
                "description":       user.get("classification", "StockTwits trader"),
                "verified":          verified,
            },
            "source": "stocktwits",
        }

    # --- Symbol stream ---
    for symbol in STOCKTWITS_SYMBOLS[:8]:
        if len(results) >= max_results:
            break
        url = f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
        logger.info("[StockTwits] Fetching symbol stream: %s", symbol)
        try:
            resp = requests.get(url, headers=_BASE_HEADERS, timeout=10)
            if resp.status_code == 429:
                logger.warning("[StockTwits] Rate limited. Sleeping 5s…")
                time.sleep(5)
                continue
            if not resp.ok:
                logger.warning("[StockTwits] HTTP %d for symbol %s", resp.status_code, symbol)
                continue
            messages = resp.json().get("messages", [])
        except Exception as exc:
            logger.warning("[StockTwits] Symbol stream failed [%s]: %s", symbol, exc)
            continue

        for msg in messages:
            rec = _process_message(msg, symbol)
            if rec:
                results.append(rec)

        time.sleep(random.uniform(0.3, 0.8))

    # --- Search endpoint ---
    for term in STOCKTWITS_SEARCH_TERMS[:3]:
        if len(results) >= max_results:
            break
        logger.info("[StockTwits] Search: %s", term)
        try:
            resp = requests.get(
                "https://api.stocktwits.com/api/2/search/messages.json",
                headers=_BASE_HEADERS,
                params={"q": term, "limit": 30},
                timeout=10,
            )
            if not resp.ok:
                continue
            messages = resp.json().get("results", [])
        except Exception as exc:
            logger.warning("[StockTwits] Search failed [%s]: %s", term, exc)
            continue

        for msg in messages:
            rec = _process_message(msg)
            if rec:
                results.append(rec)

        time.sleep(random.uniform(0.3, 0.8))

    logger.info("[StockTwits] Total useful posts: %d", len(results))
    return results


# ---------------------------------------------------------------------------
# Strategy 2 — Reddit Public JSON API (no key, no auth needed)
# ---------------------------------------------------------------------------

REDDIT_SUBREDDITS = [
    "IndianStockMarket",
    "IndiaInvestments",
    "Nifty",
    "NSEbets",
]

REDDIT_SEARCH_TERMS = [
    "CE PE target SL",
    "NIFTY CE buy target",
    "BANKNIFTY option call",
    "options intraday target stoploss",
]

_REDDIT_HEADERS = {
    "User-Agent": "OptionSignalsIndia/1.0 (stock option dashboard)",
    "Accept": "application/json",
}


def fetch_via_reddit(max_results: int = 40) -> list[dict]:
    """
    Fetch from Reddit public JSON API.
    Works without any authentication or API key.
    """
    results  = []
    seen_ids = set()

    def _process_post(post_data: dict) -> Optional[dict]:
        post_id = post_data.get("id", "")
        if not post_id or post_id in seen_ids:
            return None
        seen_ids.add(post_id)

        title = post_data.get("title", "")
        body  = post_data.get("selftext", "") or ""
        text  = f"{title}\n{body}".strip()

        if not text or len(text) < 20:
            return None

        # Must mention CE or PE for option trades
        if not OPTION_TYPE_PATTERN.search(text):
            return None

        parsed = parse_text(text)
        if not _is_useful(parsed):
            return None

        author    = post_data.get("author", "redditor")
        score     = post_data.get("score", 0) or 0
        comments  = post_data.get("num_comments", 0) or 0
        subreddit = post_data.get("subreddit", "")

        created_utc = post_data.get("created_utc", 0)
        try:
            created_at = datetime.fromtimestamp(created_utc, tz=timezone.utc).isoformat()
        except Exception:
            created_at = datetime.now(timezone.utc).isoformat()

        permalink = post_data.get("permalink", "")
        post_url  = f"https://reddit.com{permalink}" if permalink else f"https://reddit.com/r/{subreddit}"

        return {
            "id":         f"reddit_{post_id}",
            "text":       text[:500],
            "tweet_url":  post_url,
            "created_at": created_at,
            **parsed,
            "sentiment":  determine_sentiment(parsed.get("option_type")),
            "likes":      score,
            "retweets":   0,
            "replies":    comments,
            "author": {
                "name":              author,
                "handle":            f"u/{author}",
                "username":          author,
                "followers":         post_data.get("author_karma", 0) or 0,
                "following":         0,
                "tweet_count":       0,
                "profile_image_url": "",
                "description":       f"Reddit u/{author} on r/{subreddit}",
                "verified":          False,
            },
            "source": "reddit",
        }

    # Search across subreddits
    for subreddit in REDDIT_SUBREDDITS[:3]:
        for term in REDDIT_SEARCH_TERMS[:2]:
            if len(results) >= max_results:
                break
            url = f"https://www.reddit.com/r/{subreddit}/search.json"
            logger.info("[Reddit] Searching r/%s for: %s", subreddit, term)
            try:
                resp = requests.get(
                    url,
                    headers=_REDDIT_HEADERS,
                    params={"q": term, "sort": "new", "limit": 25, "restrict_sr": "true"},
                    timeout=12,
                )
                if resp.status_code == 429:
                    logger.warning("[Reddit] Rate limited. Sleeping 5s…")
                    time.sleep(5)
                    continue
                if not resp.ok:
                    logger.warning("[Reddit] HTTP %d for r/%s", resp.status_code, subreddit)
                    continue
                posts = resp.json().get("data", {}).get("children", [])
            except Exception as exc:
                logger.warning("[Reddit] Failed [r/%s]: %s", subreddit, exc)
                continue

            for post in posts:
                rec = _process_post(post.get("data", {}))
                if rec:
                    results.append(rec)

            time.sleep(random.uniform(0.5, 1.0))

    # Also fetch hot posts from key subreddits
    for subreddit in ["IndianStockMarket", "NSEbets"]:
        if len(results) >= max_results:
            break
        logger.info("[Reddit] Hot posts from r/%s", subreddit)
        try:
            resp = requests.get(
                f"https://www.reddit.com/r/{subreddit}/new.json",
                headers=_REDDIT_HEADERS,
                params={"limit": 25},
                timeout=12,
            )
            if not resp.ok:
                continue
            posts = resp.json().get("data", {}).get("children", [])
        except Exception as exc:
            logger.warning("[Reddit] Failed [r/%s new]: %s", subreddit, exc)
            continue

        for post in posts:
            rec = _process_post(post.get("data", {}))
            if rec:
                results.append(rec)

        time.sleep(random.uniform(0.5, 1.0))

    logger.info("[Reddit] Total useful posts: %d", len(results))
    return results


# ---------------------------------------------------------------------------
# Strategy 3 — Twitter API v2 (requires paid Basic tier — kept as optional)
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
        client = tweepy.Client(bearer_token=bearer, wait_on_rate_limit=True)
    except Exception:
        return []

    results  = []
    seen_ids = set()
    queries  = [
        "(NIFTY OR BANKNIFTY) (CE OR PE) (target OR SL) -is:retweet",
        "(NSE OR BSE) (CE OR PE) (target OR buy) -is:retweet",
    ]

    for query in queries:
        logger.info("[Twitter API v2] Searching: %s", query)
        try:
            response = client.search_recent_tweets(
                query=query,
                max_results=min(max_per_query, 100),
                tweet_fields=["created_at", "public_metrics", "author_id"],
                user_fields=["name", "username", "public_metrics", "profile_image_url", "description", "verified"],
                expansions=["author_id"],
            )
        except Exception as exc:
            msg = str(exc)
            if any(c in msg for c in ["402", "403", "Payment"]):
                logger.warning("[Twitter API v2] Payment required — skipping.")
                break
            logger.warning("[Twitter API v2] Error: %s", exc)
            continue

        if not response or not response.data:
            continue

        user_lookup: dict = {}
        if response.includes and response.includes.get("users"):
            for u in response.includes["users"]:
                m = u.public_metrics or {}
                user_lookup[u.id] = {
                    "name": u.name, "handle": f"@{u.username}", "username": u.username,
                    "followers": m.get("followers_count", 0), "following": m.get("following_count", 0),
                    "tweet_count": m.get("tweet_count", 0),
                    "profile_image_url": getattr(u, "profile_image_url", ""),
                    "description": getattr(u, "description", ""),
                    "verified": getattr(u, "verified", False),
                }

        for tweet in response.data:
            if tweet.id in seen_ids:
                continue
            seen_ids.add(tweet.id)
            parsed = parse_text(tweet.text)
            if not _is_useful(parsed):
                continue
            author  = user_lookup.get(tweet.author_id, {})
            metrics = tweet.public_metrics or {}
            ca = tweet.created_at
            if ca and ca.tzinfo is None:
                ca = ca.replace(tzinfo=timezone.utc)
            results.append({
                "id": str(tweet.id), "text": tweet.text,
                "tweet_url": f"https://x.com/{author.get('username','i')}/status/{tweet.id}",
                "created_at": (ca or datetime.now(timezone.utc)).isoformat(),
                **parsed,
                "sentiment": determine_sentiment(parsed.get("option_type")),
                "likes": metrics.get("like_count", 0),
                "retweets": metrics.get("retweet_count", 0),
                "replies": metrics.get("reply_count", 0),
                "author": author,
            })

    logger.info("[Twitter API v2] Total parsed: %d", len(results))
    return results


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def fetch_recommendations(max_results: int = 60) -> list[dict]:
    """
    Fetch option recommendations — tries each strategy in order:
      1. StockTwits API  (free, no key)
      2. Reddit JSON API (free, no key)
      3. Twitter API v2  (paid — optional)
    Returns [] if all fail; caller uses mock data.
    """
    # Strategy 1: StockTwits
    logger.info("Strategy 1: StockTwits Public API…")
    data = fetch_via_stocktwits(max_results)
    if data:
        logger.info("StockTwits succeeded: %d results.", len(data))
        return data

    # Strategy 2: Reddit
    logger.info("Strategy 2: Reddit Public JSON API…")
    data = fetch_via_reddit(max_results)
    if data:
        logger.info("Reddit succeeded: %d results.", len(data))
        return data

    # Strategy 3: Twitter API v2 (paid)
    bearer = os.getenv("TWITTER_BEARER_TOKEN", "")
    if bearer and bearer != "your_bearer_token_here":
        logger.info("Strategy 3: Twitter API v2…")
        data = fetch_via_twitter_api(max_results)
        if data:
            logger.info("Twitter API v2 succeeded: %d results.", len(data))
            return data

    logger.warning("All fetch strategies exhausted.")
    return []


# ---------------------------------------------------------------------------
# Mock data (always reliable fallback)
# ---------------------------------------------------------------------------

def get_mock_data() -> list[dict]:
    now = datetime.now(timezone.utc)
    return [
        {
            "id": "mock_001", "source": "demo",
            "text": "🔥 BANKNIFTY 51000 CE Buy @ 180-190\n🎯 Target: T1-240, T2-300\n🛑 SL: 140\nFor Today Intraday\n#BankNifty #NSE",
            "tweet_url": "https://x.com/example", "created_at": now.isoformat(),
            "symbol": "BANKNIFTY", "strike_price": "51000", "option_type": "CE",
            "buy_price": 185.0, "targets": [240.0, 300.0], "stop_loss": 140.0,
            "horizon": "today", "expiry": "08AUG", "sentiment": "BULLISH",
            "likes": 342, "retweets": 87, "replies": 23,
            "author": {"name": "NSE Options Guru", "handle": "@NSEOptionsGuru", "username": "NSEOptionsGuru",
                       "followers": 125430, "following": 870, "tweet_count": 18540,
                       "profile_image_url": "", "description": "SEBI Registered | Option Trader | NSE/BSE", "verified": True},
        },
        {
            "id": "mock_002", "source": "demo",
            "text": "NIFTY 24500 PE Buy near 95-100\nTgt 140 / 175\nSL 70\nIntraday for today\n#NIFTY50 #OptionsTrading",
            "tweet_url": "https://x.com/example", "created_at": now.isoformat(),
            "symbol": "NIFTY", "strike_price": "24500", "option_type": "PE",
            "buy_price": 97.5, "targets": [140.0, 175.0], "stop_loss": 70.0,
            "horizon": "today", "expiry": "08AUG", "sentiment": "BEARISH",
            "likes": 189, "retweets": 45, "replies": 12,
            "author": {"name": "Rahul Option Trader", "handle": "@RahulOptionTrader", "username": "RahulOptionTrader",
                       "followers": 67200, "following": 1200, "tweet_count": 9800,
                       "profile_image_url": "", "description": "Intraday & Positional NSE trader", "verified": False},
        },
        {
            "id": "mock_003", "source": "demo",
            "text": "📈 RELIANCE 3100 CE buy @55 for tomorrow\nT1: 80 T2: 110\nSL: 38\n#Reliance #NSE",
            "tweet_url": "https://x.com/example", "created_at": now.isoformat(),
            "symbol": "RELIANCE", "strike_price": "3100", "option_type": "CE",
            "buy_price": 55.0, "targets": [80.0, 110.0], "stop_loss": 38.0,
            "horizon": "tomorrow", "expiry": "29AUG", "sentiment": "BULLISH",
            "likes": 521, "retweets": 134, "replies": 41,
            "author": {"name": "Stock Market India", "handle": "@StockMarketIndia", "username": "StockMarketIndia",
                       "followers": 312000, "following": 540, "tweet_count": 32100,
                       "profile_image_url": "", "description": "Premium Option Tips | SEBI Reg RA", "verified": True},
        },
        {
            "id": "mock_004", "source": "demo",
            "text": "HDFCBANK 1700 PE @ 28 for tomorrow\nSL 20, Tgt 45/65\n#HDFCBANK #NSE",
            "tweet_url": "https://x.com/example", "created_at": now.isoformat(),
            "symbol": "HDFCBANK", "strike_price": "1700", "option_type": "PE",
            "buy_price": 28.0, "targets": [45.0, 65.0], "stop_loss": 20.0,
            "horizon": "tomorrow", "expiry": "29AUG", "sentiment": "BEARISH",
            "likes": 98, "retweets": 22, "replies": 8,
            "author": {"name": "Priya Sharma Trading", "handle": "@PriyaSharmaTrading", "username": "PriyaSharmaTrading",
                       "followers": 44100, "following": 320, "tweet_count": 5600,
                       "profile_image_url": "", "description": "Technical Analyst | 7+ yrs NSE", "verified": False},
        },
        {
            "id": "mock_005", "source": "demo",
            "text": "Monthly swing: TATAMOTORS 900 CE @ 35\nExpiry 28SEP | T1 60 T2 90\nSL 22\n#TataMotors #Swing",
            "tweet_url": "https://x.com/example", "created_at": now.isoformat(),
            "symbol": "TATAMOTORS", "strike_price": "900", "option_type": "CE",
            "buy_price": 35.0, "targets": [60.0, 90.0], "stop_loss": 22.0,
            "horizon": "monthly", "expiry": "28SEP", "sentiment": "BULLISH",
            "likes": 734, "retweets": 201, "replies": 67,
            "author": {"name": "Vikram Option Expert", "handle": "@VikramOptionExpert", "username": "VikramOptionExpert",
                       "followers": 189500, "following": 680, "tweet_count": 24300,
                       "profile_image_url": "", "description": "SEBI RA | Monthly swing | NIFTY BANKNIFTY", "verified": True},
        },
        {
            "id": "mock_006", "source": "demo",
            "text": "BANKNIFTY 51500 PE buying at 145\nSL 110, T1 185 T2 230\nToday EOD trade\n#BankNifty #intraday",
            "tweet_url": "https://x.com/example", "created_at": now.isoformat(),
            "symbol": "BANKNIFTY", "strike_price": "51500", "option_type": "PE",
            "buy_price": 145.0, "targets": [185.0, 230.0], "stop_loss": 110.0,
            "horizon": "today", "expiry": "08AUG", "sentiment": "BEARISH",
            "likes": 267, "retweets": 58, "replies": 19,
            "author": {"name": "Amit Kapoor FnO", "handle": "@AmitKapoorFnO", "username": "AmitKapoorFnO",
                       "followers": 88700, "following": 980, "tweet_count": 14200,
                       "profile_image_url": "", "description": "F&O Trader | BankNifty specialist", "verified": False},
        },
        {
            "id": "mock_007", "source": "demo",
            "text": "📊 INFY 1850 CE Monthly swing\nBuy @ 42, Expiry 28AUG\nT1: 68 T2: 95 SL: 28\n#Infosys #NSE",
            "tweet_url": "https://x.com/example", "created_at": now.isoformat(),
            "symbol": "INFY", "strike_price": "1850", "option_type": "CE",
            "buy_price": 42.0, "targets": [68.0, 95.0], "stop_loss": 28.0,
            "horizon": "monthly", "expiry": "28AUG", "sentiment": "BULLISH",
            "likes": 412, "retweets": 93, "replies": 31,
            "author": {"name": "IT Sector Expert", "handle": "@ITSectorExpert", "username": "ITSectorExpert",
                       "followers": 55600, "following": 410, "tweet_count": 7900,
                       "profile_image_url": "", "description": "IT sector analyst | NSE options", "verified": False},
        },
        {
            "id": "mock_008", "source": "demo",
            "text": "Positional Tomorrow - NIFTY 24800 CE\nEntry: 65-70 Target: 100/140 SL: 45\n#NIFTY #NSEOptions",
            "tweet_url": "https://x.com/example", "created_at": now.isoformat(),
            "symbol": "NIFTY", "strike_price": "24800", "option_type": "CE",
            "buy_price": 67.5, "targets": [100.0, 140.0], "stop_loss": 45.0,
            "horizon": "tomorrow", "expiry": "08AUG", "sentiment": "BULLISH",
            "likes": 156, "retweets": 38, "replies": 14,
            "author": {"name": "Nifty Positional Calls", "handle": "@NiftyPositional", "username": "NiftyPositional",
                       "followers": 38900, "following": 290, "tweet_count": 6100,
                       "profile_image_url": "", "description": "Positional option calls | Nifty & Bank Nifty", "verified": False},
        },
    ]


if __name__ == "__main__":
    data = fetch_recommendations()
    if not data:
        print("All live methods failed — showing mock data")
        data = get_mock_data()
    print(json.dumps(data[:2], indent=2, default=str))
    print(f"\nTotal: {len(data)}")
