"""
twitter_fetcher.py
------------------
Fetches stock option recommendation posts for Indian equity markets (NSE/BSE).

Strategy (in order):
  1. twscrape      — scrapes real Twitter using a free Twitter account (most reliable)
  2. Twitter API v2 — paid Basic tier (optional fallback)
  3. Mock data      — always works as final fallback

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

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns (shared across all sources)
# ---------------------------------------------------------------------------

INDEX_SYMBOLS = {
    "NIFTY", "NIFTY50", "BANKNIFTY", "NIFTYBANK", "SENSEX",
    "NIFTYIT", "NIFTYMIDCAP", "FINNIFTY", "MIDCPNIFTY",
    "NIFTYPSE", "NIFTYAUTO", "NIFTYFMCG", "NIFTYPHARMA",
    "NIFTYREALTY", "NIFTYMETAL", "NIFTYMEDIA",
}

SYMBOL_PATTERN = re.compile(
    r"\b(NIFTY(?:50|BANK|IT|MIDCAP|PSE|AUTO|FMCG|PHARMA|REALTY|METAL|MEDIA)?|"
    r"BANKNIFTY|FINNIFTY|MIDCPNIFTY|SENSEX|"
    # Large cap Nifty50 stocks
    r"RELIANCE|HDFCBANK|TCS|INFY|WIPRO|ICICIBANK|SBIN|HDFC|"
    r"BAJFINANCE|AXISBANK|KOTAKBANK|TATAMOTORS|MARUTI|"
    r"HINDUNILVR|ITC|LT|SUNPHARMA|DRREDDY|CIPLA|"
    r"TITAN|ADANIPORTS|ADANIENT|POWERGRID|NTPC|ONGC|"
    r"COALINDIA|TECHM|HCLTECH|ASIANPAINT|ULTRACEMCO|"
    r"BAJAJFINSV|NESTLEIND|EICHERMOT|HEROMOTOCO|"
    # Mid-cap / popular F&O stocks
    r"INDUSINDBK|DIVISLAB|GRASIM|BPCL|IOC|HINDPETRO|"
    r"TATACONSUM|BRITANNIA|HAVELLS|PIDILITIND|BERGEPAINT|"
    r"MPHASIS|LTIM|PERSISTENT|COFORGE|TATAELXSI|"
    r"IRCTC|DMART|NAUKRI|ZOMATO|PAYTM|POLICYBAZAAR|"
    r"STAR|PEL|MANAPPURAM|MUTHOOTFIN|CHOLAFIN|BAJAJ\-AUTO|"
    r"APOLLOHOSP|MAXHEALTH|FORTIS|LALPATHLAB|METROPOLIS|"
    r"OBEROIRLTY|GODREJPROP|PRESTIGE|DLF|PHOENIXLTD|"
    r"TATAPOWER|ADANIGREEN|CESC|TORNTPOWER|NHPC|"
    r"MOTHERSON|BHARATFORG|APOLLOTYRE|MRF|BALKRISIND|"
    r"FEDERALBNK|IDFCFIRSTB|RBLBANK|BANDHANBNK|AUBANK|"
    r"GNFC|AARTIIND|DEEPAKNITRITE|SRF|PIIND|"
    r"CAMS|CDSL|BSE|MCX|ANGELONE|ICICIPRULI|SBILIFE|HDFCLIFE)\b",
    re.IGNORECASE,
)
OPTION_TYPE_PATTERN = re.compile(r"\b(CE|PE)\b", re.IGNORECASE)
STRIKE_PATTERN      = re.compile(r"\b(\d{4,6})\s*(CE|PE)\b", re.IGNORECASE)
BUY_PRICE_PATTERN   = re.compile(
    r"(?:buy(?:ing)?|entry|cmp|ltp|@|above|near|around)\s*[:\-]?\s*(?:rs\.?|₹|inr)?\s*(\d+(?:\.\d+)?)"
    r"|(?:rs\.?|₹)\s*(\d+(?:\.\d+)?)\s*(?:buy|entry|ce|pe)",
    re.IGNORECASE,
)
TARGET_PATTERN = re.compile(
    r"(?:tgt|target|t1|t2|tp|tg)\s*[:\-]?\s*(?:rs\.?|₹)?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
SL_PATTERN = re.compile(
    r"(?:sl|stoploss|stop[\s\-]?loss|slw?|trail)\s*[:\-]?\s*(?:rs\.?|₹)?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
TODAY_KEYWORDS    = re.compile(r"\b(today|intraday|day\s*trade|dtrade|eod)\b", re.IGNORECASE)
TOMORROW_KEYWORDS = re.compile(r"\b(tomorrow|tmrw|next\s*day|positional\s*day)\b", re.IGNORECASE)
MONTHLY_KEYWORDS  = re.compile(r"\b(monthly|this\s*month|monthly\s*expiry|swing)\b", re.IGNORECASE)
WEEKLY_KEYWORDS   = re.compile(r"\b(weekly|week\s*expiry|this\s*week|weekly\s*expiry)\b", re.IGNORECASE)
BTST_KEYWORDS     = re.compile(r"\b(BTST|buy\s*today\s*sell\s*tomorrow|positional\s*call|overnight\s*call|overnight\s*trade)\b", re.IGNORECASE)
STOCK_REPORT_KEYWORDS = re.compile(r"\b(stock\s*report|analysis|fundamental|results?\s*preview|quarterly\s*result|earnings?\s*report|technical\s*report|research\s*report|sector\s*report)\b", re.IGNORECASE)
EXPIRY_PATTERN    = re.compile(
    r"\b(\d{1,2}(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(?:\d{2})?)\b",
    re.IGNORECASE,
)
RT_PATTERN = re.compile(r"^RT\s+@", re.IGNORECASE)

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
    # BUY_PRICE_PATTERN has two groups; pick the non-empty one
    buy_price = None
    for match_groups in bm:
        if isinstance(match_groups, tuple):
            val = next((g for g in match_groups if g), None)
        else:
            val = match_groups
        if val:
            try:
                buy_price = float(val)
                break
            except ValueError:
                pass
    targets   = [float(t) for t in TARGET_PATTERN.findall(text)[:2]]
    sm = SL_PATTERN.findall(text)
    stop_loss = float(sm[0]) if sm else None
    if TODAY_KEYWORDS.search(text):       horizon = "today"
    elif TOMORROW_KEYWORDS.search(text):  horizon = "tomorrow"
    elif MONTHLY_KEYWORDS.search(text):   horizon = "monthly"
    elif WEEKLY_KEYWORDS.search(text):    horizon = "today"
    else: horizon = "monthly" if EXPIRY_PATTERN.search(text_upper) else "today"
    expiry_m = EXPIRY_PATTERN.search(text_upper)

    # Determine instrument type: index, stock, btst, or stock_report
    symbol_upper = (symbol or "").upper()

    # Check for BTST first
    if BTST_KEYWORDS.search(text):
        instrument_type = "btst"
        expiry_type = None
    # Check for stock report
    elif STOCK_REPORT_KEYWORDS.search(text) and not OPTION_TYPE_PATTERN.search(text):
        instrument_type = "stock_report"
        expiry_type = None
    elif symbol_upper in INDEX_SYMBOLS:
        instrument_type = "index"
        # Determine expiry type for index options
        if WEEKLY_KEYWORDS.search(text) or (horizon in ("today", "tomorrow")):
            expiry_type = "weekly"
        elif MONTHLY_KEYWORDS.search(text) or horizon == "monthly":
            expiry_type = "monthly"
        else:
            expiry_type = "weekly"  # default for index: weekly
    else:
        instrument_type = "stock"
        expiry_type = None

    return {
        "symbol": symbol, "strike_price": strike_price,
        "option_type": option_type, "buy_price": buy_price,
        "targets": targets, "stop_loss": stop_loss,
        "horizon": horizon, "expiry": expiry_m.group(1) if expiry_m else None,
        "instrument_type": instrument_type, "expiry_type": expiry_type,
    }


def determine_sentiment(option_type: Optional[str], hint: str = "") -> str:
    if hint.lower() == "bullish": return "BULLISH"
    if hint.lower() == "bearish": return "BEARISH"
    if not option_type: return "NEUTRAL"
    return "BULLISH" if option_type.upper() == "CE" else "BEARISH"


# Extended option keywords for broader post matching
_OPTION_KEYWORDS = re.compile(
    r"\b(CE|PE|call\s*option|put\s*option|call|put|options?\s*trade|F&O|FnO|intraday|swing|positional)\b",
    re.IGNORECASE,
)


def _is_option_post(text: str, strict: bool = True) -> bool:
    """
    True if the post looks like an option trade recommendation.
    strict=True  → requires CE/PE + symbol (for Twitter where signal:noise is low)
    strict=False → accepts any option-related post with a known symbol
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
    "BANKNIFTY OR NIFTY CE OR PE target SL -filter:retweets lang:en",
    "NSE options CE PE buy target stoploss -filter:retweets lang:en",
    "RELIANCE OR HDFCBANK OR TCS CE PE target SL -filter:retweets lang:en",
    "SBIN OR ICICIBANK OR TATAMOTORS CE PE target SL -filter:retweets lang:en",
    "FINNIFTY OR MIDCPNIFTY CE PE target SL -filter:retweets lang:en",
]

# Persistent account DB path (Railway volume or /tmp)
_TWSCRAPE_DB = os.getenv("TWSCRAPE_DB_PATH", "/tmp/twscrape_accounts.db")


async def _twscrape_async(queries: list, max_results: int) -> list[dict]:
    """
    Async core for twscrape.

    Authentication priority (per account):
      1. Cookies: TWITTER_AUTH_TOKEN + TWITTER_CT0  ← bypasses Cloudflare (RECOMMENDED)
      2. Username + Password fallback

    Account 1 env vars:
      TWITTER_USERNAME, TWITTER_PASSWORD, TWITTER_EMAIL,
      TWITTER_EMAIL_PASSWORD, TWITTER_AUTH_TOKEN, TWITTER_CT0

    Account 2 env vars (optional, used when account 1 is rate-limited):
      TWITTER_USERNAME_2, TWITTER_PASSWORD_2, TWITTER_EMAIL_2,
      TWITTER_EMAIL_PASSWORD_2, TWITTER_AUTH_TOKEN_2, TWITTER_CT0_2

    How to get cookies:
      1. Open x.com in Chrome, log in
      2. Press F12 → Application tab → Cookies → https://x.com
      3. Copy value of 'auth_token' and 'ct0'
      4. Set as TWITTER_AUTH_TOKEN / TWITTER_CT0 (and _2 for second account)
    """
    try:
        from twscrape import API, gather
    except ImportError:
        logger.warning("[twscrape] Not installed. Run: pip install twscrape")
        return []

    api = API(_TWSCRAPE_DB)

    # ── Account registration helper ──────────────────────────────────────────
    async def _register_account(suffix: str) -> bool:
        """Register one account from env vars. suffix is '' or '_2'."""
        uname  = os.getenv(f"TWITTER_USERNAME{suffix}", "")
        passwd = os.getenv(f"TWITTER_PASSWORD{suffix}", "")
        email  = os.getenv(f"TWITTER_EMAIL{suffix}", "")
        epwd   = os.getenv(f"TWITTER_EMAIL_PASSWORD{suffix}", passwd)
        token  = os.getenv(f"TWITTER_AUTH_TOKEN{suffix}", "")
        ct0val = os.getenv(f"TWITTER_CT0{suffix}", "")

        if not uname:
            return False

        try:
            existing = {a.username.lower(): a for a in await api.pool.get_all()}

            if uname.lower() not in existing:
                if token and ct0val:
                    logger.info("[twscrape] Adding account%s via cookies: %s", suffix or "1", uname)
                    await api.pool.add_account(
                        username=uname,
                        password=passwd or "placeholder",
                        email=email or f"{uname}@placeholder.com",
                        email_password=epwd or "placeholder",
                        cookies=f"auth_token={token}; ct0={ct0val}",
                    )
                    logger.info("[twscrape] Cookie-based account%s added: %s", suffix or "1", uname)
                elif passwd and email:
                    logger.info("[twscrape] Adding account%s via password: %s", suffix or "1", uname)
                    await api.pool.add_account(
                        username=uname, password=passwd,
                        email=email, email_password=epwd,
                    )
                    await api.pool.login_all()
                    logger.info("[twscrape] Password login attempted for account%s: %s", suffix or "1", uname)
                else:
                    logger.warning(
                        "[twscrape] Account%s (%s): need (AUTH_TOKEN+CT0) or (PASSWORD+EMAIL).",
                        suffix or "1", uname,
                    )
                    return False
            else:
                acct = existing[uname.lower()]
                # Refresh cookies if account is marked inactive and new cookies supplied
                if token and ct0val and not getattr(acct, "active", True):
                    logger.info("[twscrape] Refreshing cookies for account%s: %s", suffix or "1", uname)
                    await api.pool.delete_inactive()
                    await api.pool.add_account(
                        username=uname,
                        password=passwd or "placeholder",
                        email=email or f"{uname}@placeholder.com",
                        email_password=epwd or "placeholder",
                        cookies=f"auth_token={token}; ct0={ct0val}",
                    )
                else:
                    logger.info("[twscrape] Account%s already in pool: %s", suffix or "1", uname)
        except Exception as exc:
            logger.warning("[twscrape] Account%s setup failed (%s): %s", suffix or "1", uname, exc)
            return False

        return True

    # ── Register accounts ────────────────────────────────────────────────────
    acc1_ok = await _register_account("")          # primary (required)
    if not acc1_ok:
        logger.warning("[twscrape] Primary account (TWITTER_USERNAME) not configured — aborting.")
        return []
    await _register_account("_2")                  # secondary (optional, silently skipped if not set)

    # ── Pool health check ─────────────────────────────────────────────────
    results  = []
    seen_ids = set()

    try:
        all_accounts = await api.pool.get_all()
        if not all_accounts:
            logger.warning("[twscrape] No accounts in pool — skipping search.")
            return []

        now_utc = datetime.now(timezone.utc)
        unlocked_count = 0
        for acct in all_accounts:
            locks      = getattr(acct, "locks", {}) or {}
            lock_until = locks.get("SearchTimeline")
            logger.info(
                "[twscrape] Account %-20s — SearchTimeline lock until: %s",
                getattr(acct, "username", "?"),
                lock_until or "none (available)",
            )
            # Count accounts whose lock has expired (or never set)
            if lock_until is None or (hasattr(lock_until, "replace") and lock_until < now_utc):
                unlocked_count += 1
            elif isinstance(lock_until, str):
                # some versions return a string
                try:
                    from datetime import datetime as _dt
                    lt = _dt.fromisoformat(lock_until)
                    if lt.tzinfo is None:
                        lt = lt.replace(tzinfo=timezone.utc)
                    if lt < now_utc:
                        unlocked_count += 1
                except Exception:
                    pass

        if unlocked_count == 0:
            logger.warning(
                "[twscrape] All %d account(s) are rate-limited — skipping search to avoid timeouts.",
                len(all_accounts),
            )
            return []

        logger.info("[twscrape] %d/%d account(s) available for search.", unlocked_count, len(all_accounts))

    except Exception as exc:
        logger.warning("[twscrape] Could not inspect account pool: %s", exc)
        return []

    # ── Search queries ────────────────────────────────────────────────────
    for query in queries:
        if len(results) >= max_results:
            break
        logger.info("[twscrape] Searching: %s", query)
        try:
            # 60s timeout per query — shorter to fail-fast when rate-limited
            tweets = await asyncio.wait_for(
                gather(api.search(query, limit=50)),
                timeout=60,
            )
        except asyncio.TimeoutError:
            logger.warning("[twscrape] Query timed out (60s), skipping: %s", query)
            break  # pool is probably fully rate-limited — stop wasting time
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

            u = tw.user
            # Filter: only profiles with >= 20K followers
            if (u.followersCount or 0) < 20000:
                continue

            parsed = parse_text(text)

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

        await asyncio.sleep(random.uniform(3.0, 6.0))

    logger.info("[twscrape] Total parsed: %d", len(results))
    return results


def fetch_via_twscrape(max_results: int = 60) -> list[dict]:
    """Sync wrapper around the async twscrape fetch with a hard 3-minute timeout."""
    try:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                raise RuntimeError("loop closed")
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        coro = _twscrape_async(TWSCRAPE_QUERIES, max_results)
        return loop.run_until_complete(asyncio.wait_for(coro, timeout=300))
    except asyncio.TimeoutError:
        logger.warning("[twscrape] Overall fetch timed out after 3 minutes — returning empty.")
        return []
    except Exception as exc:
        logger.warning("[twscrape] Unexpected error: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Strategy 2 — Twitter API v2 (paid — optional fallback)
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

def fetch_recommendations(max_results: int = 150) -> list[dict]:
    """
    Fetch option recommendations from Twitter only — tries in order:
      1. twscrape      (set TWITTER_USERNAME + credentials in env vars)
      2. Twitter API v2 (set TWITTER_BEARER_TOKEN — paid Basic tier)
    Falls back to mock data (handled by the caller) if both fail.
    """
    combined: list[dict] = []
    seen_ids: set = set()

    def _merge(items: list[dict]) -> None:
        for item in items:
            uid = item.get("id", "")
            if uid and uid not in seen_ids:
                seen_ids.add(uid)
                combined.append(item)

    # Strategy 1: twscrape
    username = os.getenv("TWITTER_USERNAME", "")
    if username:
        logger.info("Strategy 1: twscrape (account: %s)...", username)
        data = fetch_via_twscrape(max_results)
        if data:
            logger.info("twscrape: %d results.", len(data))
            _merge(data)
    else:
        logger.info("Strategy 1: twscrape skipped (no TWITTER_USERNAME set).")

    # Strategy 2: Twitter API v2
    bearer = os.getenv("TWITTER_BEARER_TOKEN", "")
    if bearer and bearer != "your_bearer_token_here":
        logger.info("Strategy 2: Twitter API v2...")
        data = fetch_via_twitter_api(max_results)
        if data:
            logger.info("Twitter API v2: %d results.", len(data))
            _merge(data)

    if combined:
        logger.info("Total recommendations fetched: %d", len(combined))
        return combined

    logger.warning("All Twitter fetch strategies exhausted.")
    return []


# ---------------------------------------------------------------------------
# Mock data (final fallback)
# ---------------------------------------------------------------------------

def get_mock_data() -> list[dict]:
    now = datetime.now(timezone.utc)
    _a = lambda name, handle, followers, desc="NSE F&O Trader", verified=False: {
        "name": name, "handle": f"@{handle}", "username": handle,
        "followers": followers, "following": 500, "tweet_count": 10000,
        "profile_image_url": "", "description": desc, "verified": verified,
    }
    return [
        # ── BANKNIFTY ──────────────────────────────────────────────
        {"id":"mock_001","source":"demo","text":"🔥 BANKNIFTY 51000 CE Buy @ 180-190\n🎯 Target: T1-240, T2-300\n🛑 SL: 140\nFor Today Intraday\n#BankNifty #NSE",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"BANKNIFTY","strike_price":"51000","option_type":"CE","buy_price":185.0,"targets":[240.0,300.0],"stop_loss":140.0,"horizon":"today","expiry":"08AUG","sentiment":"BULLISH","likes":342,"retweets":87,"replies":23,"instrument_type":"index","expiry_type":"weekly",
         "author":_a("NSE Options Guru","NSEOptionsGuru",125430,"SEBI Registered | Option Trader | NSE/BSE",True)},

        {"id":"mock_002","source":"demo","text":"BANKNIFTY 51500 PE at 145 SL 110 T1 185 T2 230 EOD today\n#BankNifty",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"BANKNIFTY","strike_price":"51500","option_type":"PE","buy_price":145.0,"targets":[185.0,230.0],"stop_loss":110.0,"horizon":"today","expiry":"08AUG","sentiment":"BEARISH","likes":267,"retweets":58,"replies":19,"instrument_type":"index","expiry_type":"weekly",
         "author":_a("Amit Kapoor FnO","AmitKapoorFnO",88700,"F&O Trader | BankNifty specialist")},

        {"id":"mock_003","source":"demo","text":"BANKNIFTY 52000 CE Monthly swing buy @ 220\nTarget T1:290 T2:360 SL:168\n#BankNifty #Swing",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"BANKNIFTY","strike_price":"52000","option_type":"CE","buy_price":220.0,"targets":[290.0,360.0],"stop_loss":168.0,"horizon":"monthly","expiry":"28AUG","sentiment":"BULLISH","likes":510,"retweets":121,"replies":43,"instrument_type":"index","expiry_type":"monthly",
         "author":_a("Vikram Option Expert","VikramOptionExpert",189500,"SEBI RA | NIFTY BANKNIFTY swing trader",True)},

        # ── NIFTY ──────────────────────────────────────────────────
        {"id":"mock_004","source":"demo","text":"NIFTY 24500 PE Buy near 95-100\nTgt 140/175 SL 70\nIntraday today\n#NIFTY50",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"NIFTY","strike_price":"24500","option_type":"PE","buy_price":97.5,"targets":[140.0,175.0],"stop_loss":70.0,"horizon":"today","expiry":"08AUG","sentiment":"BEARISH","likes":189,"retweets":45,"replies":12,"instrument_type":"index","expiry_type":"weekly",
         "author":_a("Rahul Option Trader","RahulOptionTrader",67200,"Intraday & Positional NSE trader")},

        {"id":"mock_005","source":"demo","text":"NIFTY 24800 CE Monthly Entry 65-70 Target 100/140 SL 45 monthly expiry\n#NIFTY",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"NIFTY","strike_price":"24800","option_type":"CE","buy_price":67.5,"targets":[100.0,140.0],"stop_loss":45.0,"horizon":"monthly","expiry":"29AUG","sentiment":"BULLISH","likes":156,"retweets":38,"replies":14,"instrument_type":"index","expiry_type":"monthly",
         "author":_a("Nifty Positional Calls","NiftyPositional",38900,"Positional option calls | Nifty & Bank Nifty")},

        {"id":"mock_006","source":"demo","text":"NIFTY 25000 CE Intraday Buy above 75\nT1:110 T2:145 SL:55\n#NIFTY50 #Intraday",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"NIFTY","strike_price":"25000","option_type":"CE","buy_price":75.0,"targets":[110.0,145.0],"stop_loss":55.0,"horizon":"today","expiry":"08AUG","sentiment":"BULLISH","likes":225,"retweets":54,"replies":18,"instrument_type":"index","expiry_type":"weekly",
         "author":_a("NSE Options Guru","NSEOptionsGuru",125430,"SEBI Registered | Option Trader",True)},

        {"id":"mock_007","source":"demo","text":"NIFTY 24200 PE tomorrow trade\nEntry: 82-88 T1:125 T2:162 SL:60\n#NIFTY",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"NIFTY","strike_price":"24200","option_type":"PE","buy_price":85.0,"targets":[125.0,162.0],"stop_loss":60.0,"horizon":"tomorrow","expiry":"15AUG","sentiment":"BEARISH","likes":178,"retweets":41,"replies":11,"instrument_type":"index","expiry_type":"weekly",
         "author":_a("Rahul Option Trader","RahulOptionTrader",67200,"Intraday & Positional NSE trader")},

        # ── FINNIFTY / MIDCPNIFTY ──────────────────────────────────
        {"id":"mock_008","source":"demo","text":"FINNIFTY 22000 CE Buy 85-90 weekly expiry SL 62 TGT 130 T2 165\n#FINNifty #Weekly",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"FINNIFTY","strike_price":"22000","option_type":"CE","buy_price":87.0,"targets":[130.0,165.0],"stop_loss":62.0,"horizon":"today","expiry":"08AUG","sentiment":"BULLISH","likes":98,"retweets":21,"replies":7,"instrument_type":"index","expiry_type":"weekly",
         "author":_a("FinNifty Trader","FinNiftyTrader",29800,"FINNIFTY weekly options specialist")},

        {"id":"mock_009","source":"demo","text":"MIDCPNIFTY 13500 PE Intraday SL 55 TGT 85/110\n#MidcapNifty #NSE",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"MIDCPNIFTY","strike_price":"13500","option_type":"PE","buy_price":68.0,"targets":[85.0,110.0],"stop_loss":55.0,"horizon":"today","expiry":"08AUG","sentiment":"BEARISH","likes":74,"retweets":15,"replies":5,"instrument_type":"index","expiry_type":"weekly",
         "author":_a("MidcapNifty Expert","MidcapNiftyExpert",22500,"MidCap & SmallCap NSE options")},

        # ── RELIANCE ───────────────────────────────────────────────
        {"id":"mock_010","source":"demo","text":"📈 RELIANCE 3100 CE buy @55 for tomorrow\nT1:80 T2:110 SL:38\n#Reliance #NSE",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"RELIANCE","strike_price":"3100","option_type":"CE","buy_price":55.0,"targets":[80.0,110.0],"stop_loss":38.0,"horizon":"tomorrow","expiry":"29AUG","sentiment":"BULLISH","likes":521,"retweets":134,"replies":41,"instrument_type":"stock","expiry_type":None,
         "author":_a("Stock Market India","StockMarketIndia",312000,"Premium Option Tips | SEBI Reg RA",True)},

        {"id":"mock_011","source":"demo","text":"RELIANCE 3050 PE Monthly buy @42 SL 30 TGT 65/88\nSwing trade positional\n#Reliance",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"RELIANCE","strike_price":"3050","option_type":"PE","buy_price":42.0,"targets":[65.0,88.0],"stop_loss":30.0,"horizon":"monthly","expiry":"28SEP","sentiment":"BEARISH","likes":310,"retweets":72,"replies":24,"instrument_type":"stock","expiry_type":None,
         "author":_a("Vikram Option Expert","VikramOptionExpert",189500,"SEBI RA | swing trader",True)},

        # ── HDFCBANK ───────────────────────────────────────────────
        {"id":"mock_012","source":"demo","text":"HDFCBANK 1700 PE @28 tomorrow SL 20 Tgt 45/65\n#HDFCBANK #NSE",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"HDFCBANK","strike_price":"1700","option_type":"PE","buy_price":28.0,"targets":[45.0,65.0],"stop_loss":20.0,"horizon":"tomorrow","expiry":"29AUG","sentiment":"BEARISH","likes":98,"retweets":22,"replies":8,"instrument_type":"stock","expiry_type":None,
         "author":_a("Priya Sharma Trading","PriyaSharmaTrading",44100,"Technical Analyst | 7+ yrs NSE")},

        {"id":"mock_013","source":"demo","text":"HDFCBANK 1750 CE Intraday Buy @32 T1:48 T2:62 SL:22\n#HDFCBANK",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"HDFCBANK","strike_price":"1750","option_type":"CE","buy_price":32.0,"targets":[48.0,62.0],"stop_loss":22.0,"horizon":"today","expiry":"08AUG","sentiment":"BULLISH","likes":143,"retweets":33,"replies":9,"instrument_type":"stock","expiry_type":None,
         "author":_a("Amit Kapoor FnO","AmitKapoorFnO",88700,"F&O Trader | Bank stocks specialist")},

        # ── TCS / INFY / WIPRO ────────────────────────────────────
        {"id":"mock_014","source":"demo","text":"TCS 4200 CE Monthly buy @68\nT1:100 T2:135 SL:48\n#TCS #IT",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"TCS","strike_price":"4200","option_type":"CE","buy_price":68.0,"targets":[100.0,135.0],"stop_loss":48.0,"horizon":"monthly","expiry":"28AUG","sentiment":"BULLISH","likes":267,"retweets":61,"replies":22,"instrument_type":"stock","expiry_type":None,
         "author":_a("IT Sector Expert","ITSectorExpert",55600,"IT sector analyst | NSE options")},

        {"id":"mock_015","source":"demo","text":"INFY 1850 CE Monthly Buy @42 Expiry 28AUG T1:68 T2:95 SL:28\n#Infosys",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"INFY","strike_price":"1850","option_type":"CE","buy_price":42.0,"targets":[68.0,95.0],"stop_loss":28.0,"horizon":"monthly","expiry":"28AUG","sentiment":"BULLISH","likes":412,"retweets":93,"replies":31,"instrument_type":"stock","expiry_type":None,
         "author":_a("IT Sector Expert","ITSectorExpert",55600,"IT sector analyst | NSE options")},

        {"id":"mock_016","source":"demo","text":"WIPRO 550 CE Intraday Buy above 18\nTGT 28/38 SL 12\n#Wipro #NSE",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"WIPRO","strike_price":"550","option_type":"CE","buy_price":18.0,"targets":[28.0,38.0],"stop_loss":12.0,"horizon":"today","expiry":"08AUG","sentiment":"BULLISH","likes":88,"retweets":18,"replies":6,"instrument_type":"stock","expiry_type":None,
         "author":_a("Priya Sharma Trading","PriyaSharmaTrading",44100,"Technical Analyst | 7+ yrs NSE")},

        {"id":"mock_017","source":"demo","text":"HCLTECH 1900 PE tomorrow SL 22 TGT 38/55\n#HCLTECH",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"HCLTECH","strike_price":"1900","option_type":"PE","buy_price":27.0,"targets":[38.0,55.0],"stop_loss":22.0,"horizon":"tomorrow","expiry":"29AUG","sentiment":"BEARISH","likes":132,"retweets":29,"replies":8,"instrument_type":"stock","expiry_type":None,
         "author":_a("IT Sector Expert","ITSectorExpert",55600,"IT sector analyst")},

        # ── BANKING STOCKS ────────────────────────────────────────
        {"id":"mock_018","source":"demo","text":"ICICIBANK 1300 CE Buy @38 Intraday\nT1:58 T2:75 SL:26\n#ICICIBANK",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"ICICIBANK","strike_price":"1300","option_type":"CE","buy_price":38.0,"targets":[58.0,75.0],"stop_loss":26.0,"horizon":"today","expiry":"08AUG","sentiment":"BULLISH","likes":201,"retweets":48,"replies":15,"instrument_type":"stock","expiry_type":None,
         "author":_a("NSE Options Guru","NSEOptionsGuru",125430,"SEBI Registered | Option Trader",True)},

        {"id":"mock_019","source":"demo","text":"SBIN 900 CE Monthly buy @25 SL 18 TGT 40/58\n#SBIN #PSUBank",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"SBIN","strike_price":"900","option_type":"CE","buy_price":25.0,"targets":[40.0,58.0],"stop_loss":18.0,"horizon":"monthly","expiry":"28AUG","sentiment":"BULLISH","likes":176,"retweets":40,"replies":13,"instrument_type":"stock","expiry_type":None,
         "author":_a("Stock Market India","StockMarketIndia",312000,"Premium Option Tips | SEBI Reg RA",True)},

        {"id":"mock_020","source":"demo","text":"AXISBANK 1200 PE tomorrow SL 32 TGT 52/72\n#AxisBank",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"AXISBANK","strike_price":"1200","option_type":"PE","buy_price":40.0,"targets":[52.0,72.0],"stop_loss":32.0,"horizon":"tomorrow","expiry":"29AUG","sentiment":"BEARISH","likes":115,"retweets":25,"replies":7,"instrument_type":"stock","expiry_type":None,
         "author":_a("Priya Sharma Trading","PriyaSharmaTrading",44100,"Technical Analyst | 7+ yrs NSE")},

        {"id":"mock_021","source":"demo","text":"KOTAKBANK 2000 CE Intraday Buy @45 SL 32 T1:65 T2:88\n#KotakBank",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"KOTAKBANK","strike_price":"2000","option_type":"CE","buy_price":45.0,"targets":[65.0,88.0],"stop_loss":32.0,"horizon":"today","expiry":"08AUG","sentiment":"BULLISH","likes":156,"retweets":36,"replies":12,"instrument_type":"stock","expiry_type":None,
         "author":_a("Amit Kapoor FnO","AmitKapoorFnO",88700,"F&O Trader | Bank stocks")},

        {"id":"mock_022","source":"demo","text":"BAJFINANCE 8000 PE Monthly buy @115 SL 84 TGT 165/215 swing\n#BajajFinance",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"BAJFINANCE","strike_price":"8000","option_type":"PE","buy_price":115.0,"targets":[165.0,215.0],"stop_loss":84.0,"horizon":"monthly","expiry":"28SEP","sentiment":"BEARISH","likes":289,"retweets":67,"replies":23,"instrument_type":"stock","expiry_type":None,
         "author":_a("Vikram Option Expert","VikramOptionExpert",189500,"SEBI RA | swing trader",True)},

        # ── AUTO / ENERGY ─────────────────────────────────────────
        {"id":"mock_023","source":"demo","text":"Monthly: TATAMOTORS 900 CE @35 Expiry 28SEP T1 60 T2 90 SL 22\n#TataMotors #Swing",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"TATAMOTORS","strike_price":"900","option_type":"CE","buy_price":35.0,"targets":[60.0,90.0],"stop_loss":22.0,"horizon":"monthly","expiry":"28SEP","sentiment":"BULLISH","likes":734,"retweets":201,"replies":67,"instrument_type":"stock","expiry_type":None,
         "author":_a("Vikram Option Expert","VikramOptionExpert",189500,"SEBI RA | NIFTY BANKNIFTY swing trader",True)},

        {"id":"mock_024","source":"demo","text":"MARUTI 13000 CE Monthly @180 SL 128 TGT 260/340\n#Maruti #AutoSector",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"MARUTI","strike_price":"13000","option_type":"CE","buy_price":180.0,"targets":[260.0,340.0],"stop_loss":128.0,"horizon":"monthly","expiry":"28SEP","sentiment":"BULLISH","likes":213,"retweets":52,"replies":17,"instrument_type":"stock","expiry_type":None,
         "author":_a("Stock Market India","StockMarketIndia",312000,"Premium Option Tips",True)},

        {"id":"mock_025","source":"demo","text":"NTPC 370 CE intraday buy @8 SL 5 TGT 13/18\n#NTPC #PowerSector",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"NTPC","strike_price":"370","option_type":"CE","buy_price":8.0,"targets":[13.0,18.0],"stop_loss":5.0,"horizon":"today","expiry":"08AUG","sentiment":"BULLISH","likes":95,"retweets":21,"replies":6,"instrument_type":"stock","expiry_type":None,
         "author":_a("Nifty Positional Calls","NiftyPositional",38900,"Positional option calls")},

        {"id":"mock_026","source":"demo","text":"ONGC 310 PE tomorrow buy @9 SL 6.5 TGT 15/21\n#ONGC #OilGas",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"ONGC","strike_price":"310","option_type":"PE","buy_price":9.0,"targets":[15.0,21.0],"stop_loss":6.5,"horizon":"tomorrow","expiry":"29AUG","sentiment":"BEARISH","likes":71,"retweets":16,"replies":5,"instrument_type":"stock","expiry_type":None,
         "author":_a("Amit Kapoor FnO","AmitKapoorFnO",88700,"F&O Trader")},

        # ── PHARMA ────────────────────────────────────────────────
        {"id":"mock_027","source":"demo","text":"SUNPHARMA 1800 CE Monthly buy @55 SL 38 T1:82 T2:112\n#SunPharma #Pharma",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"SUNPHARMA","strike_price":"1800","option_type":"CE","buy_price":55.0,"targets":[82.0,112.0],"stop_loss":38.0,"horizon":"monthly","expiry":"28AUG","sentiment":"BULLISH","likes":167,"retweets":38,"replies":13,"instrument_type":"stock","expiry_type":None,
         "author":_a("IT Sector Expert","ITSectorExpert",55600,"Sector analyst | NSE options")},

        {"id":"mock_028","source":"demo","text":"DRREDDY 6500 PE Intraday buy @88 SL 64 TGT 130/175\n#DrReddy",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"DRREDDY","strike_price":"6500","option_type":"PE","buy_price":88.0,"targets":[130.0,175.0],"stop_loss":64.0,"horizon":"today","expiry":"08AUG","sentiment":"BEARISH","likes":122,"retweets":27,"replies":8,"instrument_type":"stock","expiry_type":None,
         "author":_a("Priya Sharma Trading","PriyaSharmaTrading",44100,"Technical Analyst")},

        # ── CONSUMER / FMCG ───────────────────────────────────────
        {"id":"mock_029","source":"demo","text":"ITC 480 CE Intraday buy @10 SL 7 TGT 16/22\n#ITC #FMCG",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"ITC","strike_price":"480","option_type":"CE","buy_price":10.0,"targets":[16.0,22.0],"stop_loss":7.0,"horizon":"today","expiry":"08AUG","sentiment":"BULLISH","likes":189,"retweets":43,"replies":14,"instrument_type":"stock","expiry_type":None,
         "author":_a("Rahul Option Trader","RahulOptionTrader",67200,"Intraday & Positional NSE trader")},

        {"id":"mock_030","source":"demo","text":"HINDUNILVR 2700 PE Monthly swing @68 SL 50 TGT 102/138\n#HUL",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"HINDUNILVR","strike_price":"2700","option_type":"PE","buy_price":68.0,"targets":[102.0,138.0],"stop_loss":50.0,"horizon":"monthly","expiry":"28SEP","sentiment":"BEARISH","likes":143,"retweets":31,"replies":10,"instrument_type":"stock","expiry_type":None,
         "author":_a("Stock Market India","StockMarketIndia",312000,"Premium Option Tips",True)},

        # ── ADANI GROUP ───────────────────────────────────────────
        {"id":"mock_031","source":"demo","text":"ADANIPORTS 1500 CE Monthly buy @38 SL 26 T1:58 T2:78\n#AdaniPorts",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"ADANIPORTS","strike_price":"1500","option_type":"CE","buy_price":38.0,"targets":[58.0,78.0],"stop_loss":26.0,"horizon":"monthly","expiry":"28AUG","sentiment":"BULLISH","likes":198,"retweets":47,"replies":16,"instrument_type":"stock","expiry_type":None,
         "author":_a("Vikram Option Expert","VikramOptionExpert",189500,"SEBI RA | swing trader",True)},

        {"id":"mock_032","source":"demo","text":"ADANIENT 3000 PE tomorrow buy @72 SL 52 TGT 108/145\n#AdaniEnterprises",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"ADANIENT","strike_price":"3000","option_type":"PE","buy_price":72.0,"targets":[108.0,145.0],"stop_loss":52.0,"horizon":"tomorrow","expiry":"29AUG","sentiment":"BEARISH","likes":167,"retweets":38,"replies":12,"instrument_type":"stock","expiry_type":None,
         "author":_a("Amit Kapoor FnO","AmitKapoorFnO",88700,"F&O Trader")},

        # ── INFRA / CAPITAL GOODS ─────────────────────────────────
        {"id":"mock_033","source":"demo","text":"LT 3800 CE Monthly buy @95 SL 68 T1:140 T2:188\n#LarsenToubro #Infra",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"LT","strike_price":"3800","option_type":"CE","buy_price":95.0,"targets":[140.0,188.0],"stop_loss":68.0,"horizon":"monthly","expiry":"28SEP","sentiment":"BULLISH","likes":231,"retweets":55,"replies":18,"instrument_type":"stock","expiry_type":None,
         "author":_a("Stock Market India","StockMarketIndia",312000,"Premium Option Tips",True)},

        # ── NEW-AGE / TECH ────────────────────────────────────────
        {"id":"mock_034","source":"demo","text":"ZOMATO 290 CE Intraday Buy above 12 SL 8 TGT 18/25\n#Zomato #NewAge",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"ZOMATO","strike_price":"290","option_type":"CE","buy_price":12.0,"targets":[18.0,25.0],"stop_loss":8.0,"horizon":"today","expiry":"08AUG","sentiment":"BULLISH","likes":312,"retweets":76,"replies":28,"instrument_type":"stock","expiry_type":None,
         "author":_a("Rahul Option Trader","RahulOptionTrader",67200,"Intraday & Positional NSE trader")},

        {"id":"mock_035","source":"demo","text":"IRCTC 950 CE Monthly swing buy @28 SL 20 T1:42 T2:58\n#IRCTC",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"IRCTC","strike_price":"950","option_type":"CE","buy_price":28.0,"targets":[42.0,58.0],"stop_loss":20.0,"horizon":"monthly","expiry":"28AUG","sentiment":"BULLISH","likes":189,"retweets":44,"replies":14,"instrument_type":"stock","expiry_type":None,
         "author":_a("Nifty Positional Calls","NiftyPositional",38900,"Positional option calls")},

        # ── INSURANCE / FINANCIAL SERVICES ───────────────────────
        {"id":"mock_036","source":"demo","text":"SBILIFE 1800 CE buy @45 SL 32 TGT 68/90 Monthly\n#SBILife #Insurance",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"SBILIFE","strike_price":"1800","option_type":"CE","buy_price":45.0,"targets":[68.0,90.0],"stop_loss":32.0,"horizon":"monthly","expiry":"28AUG","sentiment":"BULLISH","likes":134,"retweets":30,"replies":10,"instrument_type":"stock","expiry_type":None,
         "author":_a("IT Sector Expert","ITSectorExpert",55600,"Sector analyst | NSE options")},

        {"id":"mock_037","source":"demo","text":"BAJAJFINSV 1900 PE tomorrow buy @52 SL 38 TGT 80/108\n#BajajFinserv",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"BAJAJFINSV","strike_price":"1900","option_type":"PE","buy_price":52.0,"targets":[80.0,108.0],"stop_loss":38.0,"horizon":"tomorrow","expiry":"29AUG","sentiment":"BEARISH","likes":178,"retweets":41,"replies":13,"instrument_type":"stock","expiry_type":None,
         "author":_a("Priya Sharma Trading","PriyaSharmaTrading",44100,"Technical Analyst | 7+ yrs NSE")},

        # ── CONSUMER DURABLES ─────────────────────────────────────
        {"id":"mock_038","source":"demo","text":"TITAN 3800 CE Monthly buy @88 SL 62 TGT 130/172\n#Titan #ConsumerDurables",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"TITAN","strike_price":"3800","option_type":"CE","buy_price":88.0,"targets":[130.0,172.0],"stop_loss":62.0,"horizon":"monthly","expiry":"28AUG","sentiment":"BULLISH","likes":212,"retweets":50,"replies":17,"instrument_type":"stock","expiry_type":None,
         "author":_a("Stock Market India","StockMarketIndia",312000,"Premium Option Tips",True)},

        # ── CEMENT / PAINTS ───────────────────────────────────────
        {"id":"mock_039","source":"demo","text":"ULTRACEMCO 11500 CE Monthly buy @145 SL 102 TGT 210/275\n#UltraCemco",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"ULTRACEMCO","strike_price":"11500","option_type":"CE","buy_price":145.0,"targets":[210.0,275.0],"stop_loss":102.0,"horizon":"monthly","expiry":"28SEP","sentiment":"BULLISH","likes":156,"retweets":36,"replies":11,"instrument_type":"stock","expiry_type":None,
         "author":_a("Vikram Option Expert","VikramOptionExpert",189500,"SEBI RA | swing trader",True)},

        {"id":"mock_040","source":"demo","text":"ASIANPAINT 3200 PE Intraday buy @72 SL 52 TGT 108/145\n#AsianPaints",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"ASIANPAINT","strike_price":"3200","option_type":"PE","buy_price":72.0,"targets":[108.0,145.0],"stop_loss":52.0,"horizon":"today","expiry":"08AUG","sentiment":"BEARISH","likes":134,"retweets":30,"replies":9,"instrument_type":"stock","expiry_type":None,
         "author":_a("Rahul Option Trader","RahulOptionTrader",67200,"Intraday & Positional NSE trader")},

        # ── REAL ESTATE ───────────────────────────────────────────
        {"id":"mock_041","source":"demo","text":"DLF 900 CE Monthly buy @28 SL 20 TGT 42/58 swing\n#DLF #RealEstate",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"DLF","strike_price":"900","option_type":"CE","buy_price":28.0,"targets":[42.0,58.0],"stop_loss":20.0,"horizon":"monthly","expiry":"28AUG","sentiment":"BULLISH","likes":178,"retweets":42,"replies":14,"instrument_type":"stock","expiry_type":None,
         "author":_a("NSE Options Guru","NSEOptionsGuru",125430,"SEBI Registered | Option Trader",True)},

        # ── POWER / UTILITIES ─────────────────────────────────────
        {"id":"mock_042","source":"demo","text":"POWERGRID 320 CE intraday buy @7 SL 5 TGT 11/15\n#PowerGrid #PowerSector",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"POWERGRID","strike_price":"320","option_type":"CE","buy_price":7.0,"targets":[11.0,15.0],"stop_loss":5.0,"horizon":"today","expiry":"08AUG","sentiment":"BULLISH","likes":82,"retweets":18,"replies":5,"instrument_type":"stock","expiry_type":None,
         "author":_a("Amit Kapoor FnO","AmitKapoorFnO",88700,"F&O Trader")},

        # ── MIDCAP BANKING ────────────────────────────────────────
        {"id":"mock_043","source":"demo","text":"FEDERALBNK 210 CE Monthly buy @8 SL 5.5 TGT 13/18\n#FederalBank",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"FEDERALBNK","strike_price":"210","option_type":"CE","buy_price":8.0,"targets":[13.0,18.0],"stop_loss":5.5,"horizon":"monthly","expiry":"28AUG","sentiment":"BULLISH","likes":98,"retweets":22,"replies":7,"instrument_type":"stock","expiry_type":None,
         "author":_a("Nifty Positional Calls","NiftyPositional",38900,"Positional option calls")},

        {"id":"mock_044","source":"demo","text":"IDFCFIRSTB 90 CE Intraday buy @3.5 SL 2.2 TGT 5.5/7.5\n#IDFCFirstBank",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"IDFCFIRSTB","strike_price":"90","option_type":"CE","buy_price":3.5,"targets":[5.5,7.5],"stop_loss":2.2,"horizon":"today","expiry":"08AUG","sentiment":"BULLISH","likes":67,"retweets":14,"replies":4,"instrument_type":"stock","expiry_type":None,
         "author":_a("Rahul Option Trader","RahulOptionTrader",67200,"Intraday & Positional NSE trader")},

        # ── HEROMOTOCO / EICHERMOT ────────────────────────────────
        {"id":"mock_045","source":"demo","text":"HEROMOTOCO 5500 CE Monthly buy @108 SL 78 TGT 158/208\n#HeroMoto #Auto",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"HEROMOTOCO","strike_price":"5500","option_type":"CE","buy_price":108.0,"targets":[158.0,208.0],"stop_loss":78.0,"horizon":"monthly","expiry":"28SEP","sentiment":"BULLISH","likes":145,"retweets":33,"replies":11,"instrument_type":"stock","expiry_type":None,
         "author":_a("Stock Market India","StockMarketIndia",312000,"Premium Option Tips",True)},

        # ── BTST CALLS ─────────────────────────────────────────
        {"id":"mock_046","source":"demo","text":"🌙 BTST Call: TATAMOTORS 920 CE Buy @42\n🎯 TGT T1:65 T2:90 SL:28\n⏳ Hold overnight for tomorrow\n#BTST #TataMotors",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"TATAMOTORS","strike_price":"920","option_type":"CE","buy_price":42.0,"targets":[65.0,90.0],"stop_loss":28.0,"horizon":"tomorrow","expiry":"15AUG","sentiment":"BULLISH","likes":678,"retweets":188,"replies":55,"instrument_type":"btst","expiry_type":None,
         "author":_a("Stock Market India","StockMarketIndia",312000,"Premium Option Tips | SEBI Reg RA",True)},

        {"id":"mock_047","source":"demo","text":"🌙 BTST: RELIANCE 3120 CE @ 58 for overnight position\n🎯 TGT 85/115 🛑 SL 38\nPositional Call overnight trade\n#BTST #Reliance",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"RELIANCE","strike_price":"3120","option_type":"CE","buy_price":58.0,"targets":[85.0,115.0],"stop_loss":38.0,"horizon":"tomorrow","expiry":"29AUG","sentiment":"BULLISH","likes":512,"retweets":141,"replies":48,"instrument_type":"btst","expiry_type":None,
         "author":_a("Vikram Option Expert","VikramOptionExpert",189500,"SEBI RA | NIFTY BANKNIFTY swing trader",True)},

        {"id":"mock_048","source":"demo","text":"🌙 Overnight Call: HDFCBANK 1720 PE @32 BTST\nSL 22 TGT 52/72\nBuy today sell tomorrow\n#BTST #HDFCBank",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"HDFCBANK","strike_price":"1720","option_type":"PE","buy_price":32.0,"targets":[52.0,72.0],"stop_loss":22.0,"horizon":"tomorrow","expiry":"29AUG","sentiment":"BEARISH","likes":389,"retweets":102,"replies":34,"instrument_type":"btst","expiry_type":None,
         "author":_a("NSE Options Guru","NSEOptionsGuru",125430,"SEBI Registered | Option Trader | NSE/BSE",True)},

        {"id":"mock_049","source":"demo","text":"🌙 BTST Positional Call: ICICIBANK 1310 CE @40\nSL 28 T1:62 T2:82\nOvernight trade buy today sell tomorrow\n#BTST #ICICIBank",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"ICICIBANK","strike_price":"1310","option_type":"CE","buy_price":40.0,"targets":[62.0,82.0],"stop_loss":28.0,"horizon":"tomorrow","expiry":"15AUG","sentiment":"BULLISH","likes":445,"retweets":118,"replies":39,"instrument_type":"btst","expiry_type":None,
         "author":_a("Rahul Option Trader","RahulOptionTrader",67200,"Intraday & Positional NSE trader")},

        {"id":"mock_050","source":"demo","text":"🌙 BTST: ZOMATO 295 CE @14 overnight\nSL 9 TGT 22/32\nBTST positional call\n#BTST #Zomato #NewAge",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"ZOMATO","strike_price":"295","option_type":"CE","buy_price":14.0,"targets":[22.0,32.0],"stop_loss":9.0,"horizon":"tomorrow","expiry":"15AUG","sentiment":"BULLISH","likes":334,"retweets":88,"replies":29,"instrument_type":"btst","expiry_type":None,
         "author":_a("Amit Kapoor FnO","AmitKapoorFnO",88700,"F&O Trader | BankNifty specialist")},

        {"id":"mock_051","source":"demo","text":"🌙 BTST Alert: BANKNIFTY 51200 PE @165\n Overnight positional call SL 125 TGT 215/270\n#BTST #BankNifty",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"BANKNIFTY","strike_price":"51200","option_type":"PE","buy_price":165.0,"targets":[215.0,270.0],"stop_loss":125.0,"horizon":"tomorrow","expiry":"15AUG","sentiment":"BEARISH","likes":589,"retweets":155,"replies":52,"instrument_type":"btst","expiry_type":None,
         "author":_a("Stock Market India","StockMarketIndia",312000,"Premium Option Tips | SEBI Reg RA",True)},

        # ── STOCK REPORTS ───────────────────────────────────
        {"id":"mock_052","source":"demo","text":"📋 Technical Report: RELIANCE\n⬆️ Strong breakout above 3080. Targets: 3150/3220. Support at 3020.\nFundamental analysis: EV push + Jio growth driving upside.\n#Reliance #StockReport #TechnicalAnalysis",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"RELIANCE","strike_price":None,"option_type":None,"buy_price":3080.0,"targets":[3150.0,3220.0],"stop_loss":3020.0,"horizon":"monthly","expiry":None,"sentiment":"BULLISH","likes":1205,"retweets":387,"replies":142,"instrument_type":"stock_report","expiry_type":None,
         "author":_a("Stock Market India","StockMarketIndia",312000,"Premium Option Tips | SEBI Reg RA",True)},

        {"id":"mock_053","source":"demo","text":"📋 Quarterly Results Preview: TCS Q1FY26\n📊 Revenue estimate: $7.2B | EPS est: ₹64\nTechnical: Cup & Handle breakout above 4180. Target 4350/4500.\n#TCS #EarningsReport #IT",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"TCS","strike_price":None,"option_type":None,"buy_price":4180.0,"targets":[4350.0,4500.0],"stop_loss":4050.0,"horizon":"monthly","expiry":None,"sentiment":"BULLISH","likes":932,"retweets":276,"replies":98,"instrument_type":"stock_report","expiry_type":None,
         "author":_a("IT Sector Expert","ITSectorExpert",55600,"IT sector analyst | NSE options")},

        {"id":"mock_054","source":"demo","text":"📋 Research Report: HDFCBANK Analysis\n🔵 NIM pressure, but credit growth solid at 16% YoY.\nTechnical: Consolidating near 1680 support. Breakout above 1720 targets 1800+.\n#HDFCBANK #BankingReport #Fundamental",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"HDFCBANK","strike_price":None,"option_type":None,"buy_price":1680.0,"targets":[1720.0,1800.0],"stop_loss":1640.0,"horizon":"monthly","expiry":None,"sentiment":"BULLISH","likes":789,"retweets":212,"replies":76,"instrument_type":"stock_report","expiry_type":None,
         "author":_a("Vikram Option Expert","VikramOptionExpert",189500,"SEBI RA | Research & Analysis",True)},

        {"id":"mock_055","source":"demo","text":"📋 Sector Report: Auto Sector Outlook\n🚗 TATAMOTORS, MARUTI, HEROMOTOCO leading the pack.\nEV adoption up 35% QoQ. TATAMOTORS: Target 980 | MARUTI: Target 13500.\n#AutoSector #SectorReport #NSE",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"TATAMOTORS","strike_price":None,"option_type":None,"buy_price":920.0,"targets":[980.0,1050.0],"stop_loss":870.0,"horizon":"monthly","expiry":None,"sentiment":"BULLISH","likes":1456,"retweets":423,"replies":167,"instrument_type":"stock_report","expiry_type":None,
         "author":_a("NSE Options Guru","NSEOptionsGuru",125430,"SEBI Registered | Research & Option Analyst",True)},

        {"id":"mock_056","source":"demo","text":"📋 Stock Analysis: INFY Q1 Results Preview\nRevenue expected ₹38,200Cr (+11% YoY). Attrition falling - margin expansion likely.\nTechnical Report: Strong support at 1820. Target 1950/2050.\n#Infosys #ResearchReport",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"INFY","strike_price":None,"option_type":None,"buy_price":1820.0,"targets":[1950.0,2050.0],"stop_loss":1780.0,"horizon":"monthly","expiry":None,"sentiment":"BULLISH","likes":678,"retweets":189,"replies":64,"instrument_type":"stock_report","expiry_type":None,
         "author":_a("IT Sector Expert","ITSectorExpert",55600,"IT sector analyst | NSE options")},

        {"id":"mock_057","source":"demo","text":"📋 Bearish Report: BAJFINANCE Analysis\n🔴 Valuations stretched at 35x FY26E. Credit cost rising.\nFundamental concern + Technical breakdown below 7800 targets 7400/7100.\n#BajajFinance #StockReport",
         "tweet_url":"https://x.com/example","created_at":now.isoformat(),
         "symbol":"BAJFINANCE","strike_price":None,"option_type":None,"buy_price":7800.0,"targets":[7400.0,7100.0],"stop_loss":8100.0,"horizon":"monthly","expiry":None,"sentiment":"BEARISH","likes":934,"retweets":261,"replies":89,"instrument_type":"stock_report","expiry_type":None,
         "author":_a("Stock Market India","StockMarketIndia",312000,"Premium Analysis | SEBI Reg RA",True)},
    ]


if __name__ == "__main__":
    data = fetch_recommendations()
    if not data:
        print("All live methods failed — showing mock data")
        data = get_mock_data()
    print(json.dumps(data[:2], indent=2, default=str))
    print(f"\nTotal: {len(data)}")
