"""
twitter_fetcher.py
------------------
Fetches stock option recommendation posts for Indian equity markets (NSE/BSE).

Strategy (in order):
  1. twscrape  — scrapes real Twitter using a free Twitter account (most reliable)
  2. StockTwits — free public API, no key needed
  3. Reddit     — public JSON API, no key needed  
  4. Mock data  — always works as final fallback

For twscrape, set these in Railway environment variables:
  TWITTER_USERNAME       = your_twitter_username
  TWITTER_PASSWORD       = your_twitter_password
  TWITTER_EMAIL          = your_twitter_email@gmail.com
  TWITTER_EMAIL_PASSWORD = your_gmail_password (only if email needs login for 2FA)
"""

import os
import re
import json
import time
import asyncio
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
RT_PATTERN = re.compile(r"^RT\s+@", re.IGNORECASE)

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
            seen.add(s); symbols.append(s)
    symbol = symbols[0] if symbols else None
    bm = BUY_PRICE_PATTERN.findall(text)
    buy_price = float(bm[0]) if bm else None
    targets   = [float(t) for t in TARGET_PATTERN.findall(text)[:2]]
    sm = SL_PATTERN.findall(text)
    stop_loss = float(sm[0]) if sm else None
    if TODAY_KEYWORDS.search(text):       horizon = "today"
    elif TOMORROW_KEYWORDS.search(text):  horizon = "tomorrow"
    elif MONTHLY_KEYWORDS.search(text):   horizon = "monthly"
    else: horizon = "monthly" if EXPIRY_PATTERN.search(text_upper) else "today"
    expiry_m = EXPIRY_PATTERN.search(text_upper)
    return {
        "symbol": symbol, "strike_price": strike_price,
        "option_type": option_type, "buy_price": buy_price,
        "targets": targets, "stop_loss": stop_loss,
        "horizon": horizon, "expiry": expiry_m.group(1) if expiry_m else None,
    }


def determine_sentiment(option_type: Optional[str], hint: str = "") -> str:
    if hint.lower() == "bullish": return "BULLISH"
    if hint.lower() == "bearish": return "BEARISH"
    if not option_type: return "NEUTRAL"
    return "BULLISH" if option_type.upper() == "CE" else "BEARISH"


# Extended option keywords for StockTwits/Reddit which use different terminology
_OPTION_KEYWORDS = re.compile(
    r"\b(CE|PE|call\s*option|put\s*option|call|put|options?\s*trade|F&O|FnO|intraday|swing|positional)\b",
    re.IGNORECASE,
)


def _is_option_post(text: str, strict: bool = True) -> bool:
    """
    True if the post looks like an option trade recommendation.
    strict=True  → requires CE/PE + symbol (for Twitter where signal:noise is low)
    strict=False → accepts any option-related post with a symbol (for StockTwits/Reddit)
    """
    has_symbol = bool(SYMBOL_PATTERN.search(text)) or bool(STRIKE_PATTERN.search(text))
    if strict:
        return bool(OPTION_TYPE_PATTERN.search(text)) and has_symbol
    # Relaxed: any option keyword + a known symbol, OR has buy/target/SL
    has_option_kw = bool(_OPTION_KEYWORDS.search(text))
    has_price_kw  = bool(TARGET_PATTERN.search(text)) or bool(SL_PATTERN.search(text))
    return has_symbol and (has_option_kw or has_price_kw)


# ---------------------------------------------------------------------------
# Strategy 1 — twscrape (real Twitter scraping via actual account)
# ---------------------------------------------------------------------------

TWSCRAPE_QUERIES = [
    "BANKNIFTY CE OR PE target SL -filter:retweets",
    "NIFTY CE OR PE target SL intraday -filter:retweets",
    "NSE options CE PE buy target stoploss -filter:retweets",
    "#BankNifty CE PE buy target -filter:retweets",
    "#NIFTY50 CE PE target SL -filter:retweets",
    "RELIANCE OR HDFCBANK CE PE target SL -filter:retweets",
]

# Persistent account DB path (Railway volume or /tmp)
_TWSCRAPE_DB = os.getenv("TWSCRAPE_DB_PATH", "/tmp/twscrape_accounts.db")


async def _twscrape_async(queries: list, max_results: int) -> list[dict]:
    """
    Async core for twscrape.

    Authentication priority:
      1. Cookies: TWITTER_AUTH_TOKEN + TWITTER_CT0  ← bypasses Cloudflare (RECOMMENDED)
      2. Username + Password fallback

    How to get cookies:
      1. Open x.com in Chrome, log in
      2. Press F12 → Application tab → Cookies → https://x.com
      3. Copy value of 'auth_token' and 'ct0'
      4. Set as TWITTER_AUTH_TOKEN and TWITTER_CT0 in Railway Variables
    """
    try:
        from twscrape import API, gather
    except ImportError:
        logger.warning("[twscrape] Not installed. Run: pip install twscrape")
        return []

    username       = os.getenv("TWITTER_USERNAME", "")
    password       = os.getenv("TWITTER_PASSWORD", "")
    email          = os.getenv("TWITTER_EMAIL", "")
    email_password = os.getenv("TWITTER_EMAIL_PASSWORD", password)
    auth_token     = os.getenv("TWITTER_AUTH_TOKEN", "")   # cookie value
    ct0            = os.getenv("TWITTER_CT0", "")           # cookie value

    if not username:
        logger.warning("[twscrape] TWITTER_USERNAME not set.")
        return []

    api = API(_TWSCRAPE_DB)

    try:
        accounts = await api.pool.get_all()
        existing = {a.username.lower(): a for a in accounts}

        if username.lower() not in existing:
            if auth_token and ct0:
                # ── Cookie-based login (bypasses Cloudflare) ──────────────
                logger.info("[twscrape] Adding account via cookies: %s", username)
                cookies_str = f"auth_token={auth_token}; ct0={ct0}"
                await api.pool.add_account(
                    username=username,
                    password=password or "placeholder",
                    email=email or f"{username}@placeholder.com",
                    email_password=email_password or "placeholder",
                    cookies=cookies_str,
                )
                logger.info("[twscrape] Cookie-based account added.")
            elif password and email:
                # ── Username/Password login (may hit Cloudflare on cloud IPs) ──
                logger.info("[twscrape] Adding account via password: %s", username)
                await api.pool.add_account(
                    username=username,
                    password=password,
                    email=email,
                    email_password=email_password,
                )
                await api.pool.login_all()
                logger.info("[twscrape] Password login attempted.")
            else:
                logger.warning(
                    "[twscrape] Need either (TWITTER_AUTH_TOKEN + TWITTER_CT0) "
                    "or (TWITTER_PASSWORD + TWITTER_EMAIL)."
                )
                return []
        else:
            acct = existing[username.lower()]
            # If cookies changed, update them
            if auth_token and ct0 and not getattr(acct, 'active', False):
                logger.info("[twscrape] Re-adding account with fresh cookies: %s", username)
                cookies_str = f"auth_token={auth_token}; ct0={ct0}"
                await api.pool.delete_inactive()
                await api.pool.add_account(
                    username=username,
                    password=password or "placeholder",
                    email=email or f"{username}@placeholder.com",
                    email_password=email_password or "placeholder",
                    cookies=cookies_str,
                )
            else:
                logger.info("[twscrape] Account already active: %s", username)
    except Exception as exc:
        logger.warning("[twscrape] Account setup failed: %s", exc)
        return []

    results  = []
    seen_ids = set()

    for query in queries:
        if len(results) >= max_results:
            break
        logger.info("[twscrape] Searching: %s", query)
        try:
            tweets = await gather(api.search(query, limit=30))
        except Exception as exc:
            logger.warning("[twscrape] Search failed [%s]: %s", query, exc)
            continue

        for tw in tweets:
            tweet_id = str(tw.id)
            if tweet_id in seen_ids:
                continue
            seen_ids.add(tweet_id)

            text = tw.rawContent or tw.content or ""
            if RT_PATTERN.match(text) or not _is_option_post(text):
                continue

            parsed = parse_text(text)
            u      = tw.user

            results.append({
                "id":         tweet_id,
                "text":       text,
                "tweet_url":  f"https://x.com/{u.username}/status/{tweet_id}",
                "created_at": tw.date.replace(tzinfo=timezone.utc).isoformat() if tw.date else datetime.now(timezone.utc).isoformat(),
                **parsed,
                "sentiment":  determine_sentiment(parsed.get("option_type")),
                "likes":      tw.likeCount or 0,
                "retweets":   tw.retweetCount or 0,
                "replies":    tw.replyCount or 0,
                "author": {
                    "name":              u.displayname or u.username,
                    "handle":            f"@{u.username}",
                    "username":          u.username,
                    "followers":         u.followersCount or 0,
                    "following":         u.friendsCount or 0,
                    "tweet_count":       u.statusesCount or 0,
                    "profile_image_url": u.profileImageUrl or "",
                    "description":       u.rawDescription or "",
                    "verified":          bool(u.verified or u.blue),
                },
                "source": "twitter",
            })

        await asyncio.sleep(random.uniform(1.0, 2.0))

    logger.info("[twscrape] Total parsed: %d", len(results))
    return results


def fetch_via_twscrape(max_results: int = 60) -> list[dict]:
    """Sync wrapper around the async twscrape fetch."""
    try:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                raise RuntimeError("loop closed")
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        return loop.run_until_complete(_twscrape_async(TWSCRAPE_QUERIES, max_results))
    except Exception as exc:
        logger.warning("[twscrape] Unexpected error: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Strategy 2 — StockTwits Public API (free, no key)
# ---------------------------------------------------------------------------

STOCKTWITS_SYMBOLS = [
    "NIFTY", "BANKNIFTY", "SENSEX",
    "RELIANCE", "HDFCBANK", "TCS", "INFY",
    "SBIN", "ICICIBANK", "TATAMOTORS",
]


def fetch_via_stocktwits(max_results: int = 60) -> list[dict]:
    results  = []
    seen_ids = set()

    def _proc(msg: dict) -> Optional[dict]:
        mid = str(msg.get("id", ""))
        if not mid or mid in seen_ids:
            return None
        seen_ids.add(mid)
        body = msg.get("body", "")
        if not body or not _is_option_post(body, strict=False):
            return None
        parsed    = parse_text(body)
        user      = msg.get("user", {})
        entities  = msg.get("entities") or {}
        sentiment = (entities.get("sentiment") or {}).get("basic", "")
        username  = user.get("username", "stocktwits_user")
        try:
            created_at = datetime.strptime(
                msg.get("created_at", ""), "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc).isoformat()
        except Exception:
            created_at = datetime.now(timezone.utc).isoformat()
        return {
            "id":         f"st_{mid}",
            "text":       body,
            "tweet_url":  f"https://stocktwits.com/{username}/message/{mid}",
            "created_at": created_at,
            **parsed,
            "sentiment":  determine_sentiment(parsed.get("option_type"), sentiment),
            "likes":      (msg.get("likes") or {}).get("total", 0),
            "retweets":   0,
            "replies":    0,
            "author": {
                "name":              user.get("name", username),
                "handle":            f"@{username}",
                "username":          username,
                "followers":         user.get("followers", 0) or 0,
                "following":         user.get("following", 0) or 0,
                "tweet_count":       user.get("ideas", 0) or 0,
                "profile_image_url": user.get("avatar_url_ssl", "") or "",
                "description":       user.get("classification", "StockTwits trader"),
                "verified":          bool(user.get("official", False)),
            },
            "source": "stocktwits",
        }

    for symbol in STOCKTWITS_SYMBOLS:
        if len(results) >= max_results:
            break
        logger.info("[StockTwits] Symbol stream: %s", symbol)
        try:
            resp = requests.get(
                f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json",
                headers=_BASE_HEADERS, timeout=10,
            )
            if resp.status_code == 429:
                time.sleep(5); continue
            if not resp.ok:
                continue
            for msg in resp.json().get("messages", []):
                rec = _proc(msg)
                if rec:
                    results.append(rec)
        except Exception as exc:
            logger.warning("[StockTwits] Failed [%s]: %s", symbol, exc)
        time.sleep(random.uniform(0.3, 0.7))

    logger.info("[StockTwits] Total: %d", len(results))
    return results


# ---------------------------------------------------------------------------
# Strategy 3 — Reddit Public API (free, needs OAuth app for server env)
# ---------------------------------------------------------------------------

_REDDIT_CLIENT_ID     = os.getenv("REDDIT_CLIENT_ID", "")
_REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "")
_REDDIT_TOKEN: dict   = {}


def _get_reddit_token() -> Optional[str]:
    global _REDDIT_TOKEN
    if not _REDDIT_CLIENT_ID or not _REDDIT_CLIENT_SECRET:
        return None
    now = time.time()
    if _REDDIT_TOKEN.get("token") and now < _REDDIT_TOKEN.get("expires", 0):
        return _REDDIT_TOKEN["token"]
    try:
        resp = requests.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=(_REDDIT_CLIENT_ID, _REDDIT_CLIENT_SECRET),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": "OptionSignalsIndia/1.0"},
            timeout=10,
        )
        if resp.ok:
            d = resp.json()
            _REDDIT_TOKEN = {
                "token":   d.get("access_token"),
                "expires": now + d.get("expires_in", 3600) - 60,
            }
            return _REDDIT_TOKEN["token"]
    except Exception as exc:
        logger.warning("[Reddit] Token error: %s", exc)
    return None


REDDIT_SUBREDDITS  = ["IndianStockMarket", "IndiaInvestments", "NSEbets", "Nifty"]
REDDIT_SEARCHES    = ["NIFTY CE PE target", "BANKNIFTY option call target SL"]


def fetch_via_reddit(max_results: int = 40) -> list[dict]:
    token   = _get_reddit_token()
    headers = {"User-Agent": "OptionSignalsIndia/1.0"}
    base    = "https://oauth.reddit.com" if token else "https://www.reddit.com"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    results  = []
    seen_ids = set()

    def _proc(post: dict) -> Optional[dict]:
        pid = post.get("id", "")
        if not pid or pid in seen_ids:
            return None
        seen_ids.add(pid)
        title = post.get("title", "")
        body  = post.get("selftext", "") or ""
        text  = f"{title}\n{body}".strip()
        if not text or not _is_option_post(text):
            return None
        parsed    = parse_text(text)
        author    = post.get("author", "redditor")
        subreddit = post.get("subreddit", "")
        try:
            created_at = datetime.fromtimestamp(
                post.get("created_utc", 0), tz=timezone.utc
            ).isoformat()
        except Exception:
            created_at = datetime.now(timezone.utc).isoformat()
        permalink = post.get("permalink", "")
        return {
            "id":         f"reddit_{pid}",
            "text":       text[:600],
            "tweet_url":  f"https://reddit.com{permalink}",
            "created_at": created_at,
            **parsed,
            "sentiment":  determine_sentiment(parsed.get("option_type")),
            "likes":      post.get("score", 0) or 0,
            "retweets":   0,
            "replies":    post.get("num_comments", 0) or 0,
            "author": {
                "name":              author,
                "handle":            f"u/{author}",
                "username":          author,
                "followers":         post.get("author_karma", 0) or 0,
                "following":         0, "tweet_count": 0,
                "profile_image_url": "",
                "description":       f"Reddit u/{author} · r/{subreddit}",
                "verified":          False,
            },
            "source": "reddit",
        }

    for sub in REDDIT_SUBREDDITS[:3]:
        for term in REDDIT_SEARCHES[:2]:
            if len(results) >= max_results:
                break
            logger.info("[Reddit] r/%s search: %s", sub, term)
            try:
                resp = requests.get(
                    f"{base}/r/{sub}/search.json",
                    headers=headers,
                    params={"q": term, "sort": "new", "limit": 25, "restrict_sr": "true"},
                    timeout=12,
                )
                if resp.status_code in (401, 403):
                    logger.warning("[Reddit] %d on r/%s — need OAuth credentials", resp.status_code, sub)
                    break
                if not resp.ok:
                    continue
                for post in resp.json().get("data", {}).get("children", []):
                    rec = _proc(post.get("data", {}))
                    if rec:
                        results.append(rec)
            except Exception as exc:
                logger.warning("[Reddit] Failed [r/%s]: %s", sub, exc)
            time.sleep(random.uniform(0.4, 0.8))

    logger.info("[Reddit] Total: %d", len(results))
    return results


# ---------------------------------------------------------------------------
# Strategy 4 — Twitter API v2 (paid — optional fallback)
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
    results, seen_ids = [], set()
    for query in [
        "(NIFTY OR BANKNIFTY) (CE OR PE) (target OR SL) -is:retweet",
        "(NSE OR BSE) (CE OR PE) (target OR buy) -is:retweet",
    ]:
        try:
            response = client.search_recent_tweets(
                query=query, max_results=min(max_per_query, 100),
                tweet_fields=["created_at", "public_metrics", "author_id"],
                user_fields=["name", "username", "public_metrics", "profile_image_url", "description", "verified"],
                expansions=["author_id"],
            )
        except Exception as exc:
            if any(c in str(exc) for c in ["402", "403", "Payment"]):
                break
            continue
        if not response or not response.data:
            continue
        user_lookup = {}
        if response.includes and response.includes.get("users"):
            for u in response.includes["users"]:
                m = u.public_metrics or {}
                user_lookup[u.id] = {
                    "name": u.name, "handle": f"@{u.username}", "username": u.username,
                    "followers": m.get("followers_count", 0), "following": m.get("following_count", 0),
                    "tweet_count": m.get("tweet_count", 0),
                    "profile_image_url": getattr(u, "profile_image_url", ""),
                    "description": getattr(u, "description", ""), "verified": getattr(u, "verified", False),
                }
        for tweet in response.data:
            if tweet.id in seen_ids:
                continue
            seen_ids.add(tweet.id)
            parsed = parse_text(tweet.text)
            if not parsed.get("symbol") and not parsed.get("strike_price"):
                continue
            author = user_lookup.get(tweet.author_id, {})
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
                "author": author, "source": "twitter_api",
            })
    return results


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def fetch_recommendations(max_results: int = 60) -> list[dict]:
    """
    Fetch option recommendations — tries strategies in order:
      1. twscrape  (needs TWITTER_USERNAME/PASSWORD/EMAIL env vars — free account)
      2. StockTwits (free, no key)
      3. Reddit    (free, add REDDIT_CLIENT_ID/SECRET for server env)
      4. Twitter API v2 (paid Basic tier)
    """
    # Strategy 1: twscrape
    username = os.getenv("TWITTER_USERNAME", "")
    if username:
        logger.info("Strategy 1: twscrape (account: %s)…", username)
        data = fetch_via_twscrape(max_results)
        if data:
            logger.info("twscrape succeeded: %d results.", len(data))
            return data
    else:
        logger.info("Strategy 1: twscrape skipped (no TWITTER_USERNAME set).")

    # Strategy 2: StockTwits
    logger.info("Strategy 2: StockTwits Public API…")
    data = fetch_via_stocktwits(max_results)
    if data:
        logger.info("StockTwits succeeded: %d results.", len(data))
        return data

    # Strategy 3: Reddit
    logger.info("Strategy 3: Reddit API…")
    data = fetch_via_reddit(max_results)
    if data:
        logger.info("Reddit succeeded: %d results.", len(data))
        return data

    # Strategy 4: Twitter API v2
    bearer = os.getenv("TWITTER_BEARER_TOKEN", "")
    if bearer and bearer != "your_bearer_token_here":
        logger.info("Strategy 4: Twitter API v2…")
        data = fetch_via_twitter_api(max_results)
        if data:
            logger.info("Twitter API v2 succeeded: %d results.", len(data))
            return data

    logger.warning("All fetch strategies exhausted.")
    return []


# ---------------------------------------------------------------------------
# Mock data (final fallback)
# ---------------------------------------------------------------------------

def get_mock_data() -> list[dict]:
    now = datetime.now(timezone.utc)
    return [
        {"id":"mock_001","source":"demo","text":"🔥 BANKNIFTY 51000 CE Buy @ 180-190\n🎯 Target: T1-240, T2-300\n🛑 SL: 140\nFor Today Intraday\n#BankNifty #NSE",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"BANKNIFTY","strike_price":"51000","option_type":"CE","buy_price":185.0,"targets":[240.0,300.0],"stop_loss":140.0,"horizon":"today","expiry":"08AUG","sentiment":"BULLISH","likes":342,"retweets":87,"replies":23,
         "author":{"name":"NSE Options Guru","handle":"@NSEOptionsGuru","username":"NSEOptionsGuru","followers":125430,"following":870,"tweet_count":18540,"profile_image_url":"","description":"SEBI Registered | Option Trader | NSE/BSE","verified":True}},
        {"id":"mock_002","source":"demo","text":"NIFTY 24500 PE Buy near 95-100\nTgt 140/175 SL 70\nIntraday today\n#NIFTY50",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"NIFTY","strike_price":"24500","option_type":"PE","buy_price":97.5,"targets":[140.0,175.0],"stop_loss":70.0,"horizon":"today","expiry":"08AUG","sentiment":"BEARISH","likes":189,"retweets":45,"replies":12,
         "author":{"name":"Rahul Option Trader","handle":"@RahulOptionTrader","username":"RahulOptionTrader","followers":67200,"following":1200,"tweet_count":9800,"profile_image_url":"","description":"Intraday & Positional NSE trader","verified":False}},
        {"id":"mock_003","source":"demo","text":"📈 RELIANCE 3100 CE buy @55 for tomorrow\nT1:80 T2:110 SL:38\n#Reliance #NSE",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"RELIANCE","strike_price":"3100","option_type":"CE","buy_price":55.0,"targets":[80.0,110.0],"stop_loss":38.0,"horizon":"tomorrow","expiry":"29AUG","sentiment":"BULLISH","likes":521,"retweets":134,"replies":41,
         "author":{"name":"Stock Market India","handle":"@StockMarketIndia","username":"StockMarketIndia","followers":312000,"following":540,"tweet_count":32100,"profile_image_url":"","description":"Premium Option Tips | SEBI Reg RA","verified":True}},
        {"id":"mock_004","source":"demo","text":"HDFCBANK 1700 PE @28 tomorrow SL 20 Tgt 45/65\n#HDFCBANK #NSE",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"HDFCBANK","strike_price":"1700","option_type":"PE","buy_price":28.0,"targets":[45.0,65.0],"stop_loss":20.0,"horizon":"tomorrow","expiry":"29AUG","sentiment":"BEARISH","likes":98,"retweets":22,"replies":8,
         "author":{"name":"Priya Sharma Trading","handle":"@PriyaSharmaTrading","username":"PriyaSharmaTrading","followers":44100,"following":320,"tweet_count":5600,"profile_image_url":"","description":"Technical Analyst | 7+ yrs NSE","verified":False}},
        {"id":"mock_005","source":"demo","text":"Monthly: TATAMOTORS 900 CE @35 Expiry 28SEP T1 60 T2 90 SL 22\n#TataMotors #Swing",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"TATAMOTORS","strike_price":"900","option_type":"CE","buy_price":35.0,"targets":[60.0,90.0],"stop_loss":22.0,"horizon":"monthly","expiry":"28SEP","sentiment":"BULLISH","likes":734,"retweets":201,"replies":67,
         "author":{"name":"Vikram Option Expert","handle":"@VikramOptionExpert","username":"VikramOptionExpert","followers":189500,"following":680,"tweet_count":24300,"profile_image_url":"","description":"SEBI RA | NIFTY BANKNIFTY swing trader","verified":True}},
        {"id":"mock_006","source":"demo","text":"BANKNIFTY 51500 PE at 145 SL 110 T1 185 T2 230 EOD today\n#BankNifty",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"BANKNIFTY","strike_price":"51500","option_type":"PE","buy_price":145.0,"targets":[185.0,230.0],"stop_loss":110.0,"horizon":"today","expiry":"08AUG","sentiment":"BEARISH","likes":267,"retweets":58,"replies":19,
         "author":{"name":"Amit Kapoor FnO","handle":"@AmitKapoorFnO","username":"AmitKapoorFnO","followers":88700,"following":980,"tweet_count":14200,"profile_image_url":"","description":"F&O Trader | BankNifty specialist","verified":False}},
        {"id":"mock_007","source":"demo","text":"INFY 1850 CE Monthly Buy @42 Expiry 28AUG T1:68 T2:95 SL:28\n#Infosys",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"INFY","strike_price":"1850","option_type":"CE","buy_price":42.0,"targets":[68.0,95.0],"stop_loss":28.0,"horizon":"monthly","expiry":"28AUG","sentiment":"BULLISH","likes":412,"retweets":93,"replies":31,
         "author":{"name":"IT Sector Expert","handle":"@ITSectorExpert","username":"ITSectorExpert","followers":55600,"following":410,"tweet_count":7900,"profile_image_url":"","description":"IT sector analyst | NSE options","verified":False}},
        {"id":"mock_008","source":"demo","text":"NIFTY 24800 CE Tomorrow Entry 65-70 Target 100/140 SL 45\n#NIFTY",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"NIFTY","strike_price":"24800","option_type":"CE","buy_price":67.5,"targets":[100.0,140.0],"stop_loss":45.0,"horizon":"tomorrow","expiry":"08AUG","sentiment":"BULLISH","likes":156,"retweets":38,"replies":14,
         "author":{"name":"Nifty Positional Calls","handle":"@NiftyPositional","username":"NiftyPositional","followers":38900,"following":290,"tweet_count":6100,"profile_image_url":"","description":"Positional option calls | Nifty & Bank Nifty","verified":False}},
    ]


if __name__ == "__main__":
    data = fetch_recommendations()
    if not data:
        print("All live methods failed — showing mock data")
        data = get_mock_data()
    print(json.dumps(data[:2], indent=2, default=str))
    print(f"\nTotal: {len(data)}")
