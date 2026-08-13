"""
notifier.py
-----------
Notification module for broadcasting real-time stock & option advisory alerts
to Telegram mobile devices via a dedicated Telegram Alert Bot.
"""

import logging
import urllib.request
import urllib.parse
import urllib.error
import json
import html
from typing import Union, Dict, Any, Tuple

from config import TELEGRAM_ALERT_BOT_TOKEN, TELEGRAM_ALERT_CHAT_ID

logger = logging.getLogger("stock_recommendation.notifier")


def format_alert_message(data: Union[Dict[str, Any], Any]) -> str:
    """Format a recommendation dictionary, SQLAlchemy model, or SignalSchema object into safe HTML for Telegram Bot."""
    if hasattr(data, "to_dict") and callable(data.to_dict):
        rec = data.to_dict()
    elif hasattr(data, "model_dump") and callable(data.model_dump):
        rec = data.model_dump()
    elif isinstance(data, dict):
        rec = data
    elif hasattr(data, "__dict__"):
        rec = data.__dict__
    else:
        rec = dict(data)

    symbol = html.escape(str(rec.get("symbol") or "N/A"))
    category = html.escape(str(rec.get("category") or "OPTION"))
    action = html.escape(str(rec.get("action") or "BUY"))
    option_type = html.escape(str(rec.get("option_type"))) if rec.get("option_type") else None
    strike_price = rec.get("strike_price")
    
    entry_range = rec.get("entry_range") or []
    entry_str = html.escape(" - ".join(map(str, entry_range))) if entry_range else "N/A"
    
    targets = rec.get("targets") or []
    targets_str = html.escape(", ".join(map(str, targets))) if targets else "N/A"
    
    stop_loss = html.escape(str(rec.get("stop_loss") or "N/A"))
    timeframe = html.escape(str(rec.get("timeframe") or "N/A"))
    source_channel = html.escape(str(rec.get("source_channel") or "Telegram Listener"))
    raw_text = html.escape(str(rec.get("raw_text") or "").strip())

    # Category Emojis
    category_emojis = {
        "OPTION": "📈",
        "BTST": "⚡",
        "INVESTMENT": "💎",
        "REPORT": "📊"
    }
    emoji = category_emojis.get(category, "📢")

    # Header title
    title = f"{emoji} <b>NEW ADVISORY ALERT: {action} {symbol}</b>"
    if option_type and strike_price:
        title += f" {strike_price} {option_type}"

    lines = [
        title,
        "━━━━━━━━━━━━━━━━━━━━",
        f"🏷️ <b>Category:</b> {category} | <b>Action:</b> {action}",
        f"🔹 <b>Symbol:</b> {symbol}"
    ]

    if option_type:
        lines.append(f"📌 <b>Option Type:</b> {option_type} @ {strike_price or ''}")

    lines.extend([
        f"💵 <b>Entry Range:</b> {entry_str}",
        f"🎯 <b>Targets:</b> {targets_str}",
        f"🚩 <b>Stop Loss:</b> {stop_loss}",
        f"⏱️ <b>Timeframe:</b> {timeframe}",
        f"📡 <b>Source:</b> <code>{source_channel}</code>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📝 <b>Raw Message:</b>",
        f"<i>{raw_text[:300]}</i>"
    ])

    return "\n".join(lines)


def send_telegram_alert(data: Union[Dict[str, Any], Any], bot_token: str = None, chat_id: str = None) -> bool:
    """
    Send formatted recommendation notification to Telegram Bot.
    
    Returns:
        bool: True if alert sent successfully, False otherwise.
    """
    success, _ = send_telegram_alert_verbose(data, bot_token=bot_token, chat_id=chat_id)
    return success


def send_telegram_alert_verbose(data: Union[Dict[str, Any], Any], bot_token: str = None, chat_id: str = None) -> Tuple[bool, str]:
    """
    Send formatted recommendation notification to Telegram Bot returning detailed status message.
    """
    token = bot_token or TELEGRAM_ALERT_BOT_TOKEN
    target_chat = chat_id or TELEGRAM_ALERT_CHAT_ID

    if not token:
        msg = "TELEGRAM_ALERT_BOT_TOKEN is not configured in environment variables."
        logger.warning(msg)
        return False, msg

    if not target_chat:
        msg = "TELEGRAM_ALERT_CHAT_ID is not configured in environment variables."
        logger.warning(msg)
        return False, msg

    try:
        html_message = format_alert_message(data)
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        
        payload = {
            "chat_id": target_chat,
            "text": html_message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )

        with urllib.request.urlopen(req, timeout=10) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
            if res_data.get("ok"):
                logger.info("Successfully dispatched Telegram push alert to chat_id=%s", target_chat)
                return True, "Alert sent successfully!"
            else:
                err_msg = f"Telegram API returned error: {res_data}"
                logger.error(err_msg)
                return False, err_msg

    except urllib.error.HTTPError as http_err:
        try:
            err_body = http_err.read().decode("utf-8")
        except Exception:
            err_body = str(http_err)
        msg = f"Telegram API HTTP Error {http_err.code}: {err_body}"
        logger.error(msg)
        return False, msg
    except Exception as exc:
        msg = f"Error dispatching Telegram push notification: {exc}"
        logger.error(msg)
        return False, msg
