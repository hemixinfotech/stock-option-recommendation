"""
notifier.py
-----------
Notification module for broadcasting real-time stock & option advisory alerts
to Telegram mobile devices via a dedicated Telegram Alert Bot.
"""

import logging
import urllib.request
import urllib.parse
import json
from typing import Union, Dict, Any

from config import TELEGRAM_ALERT_BOT_TOKEN, TELEGRAM_ALERT_CHAT_ID

logger = logging.getLogger("stock_recommendation.notifier")


def format_alert_message(data: Union[Dict[str, Any], Any]) -> str:
    """Format a recommendation dictionary or SignalSchema object into HTML for Telegram Bot."""
    if hasattr(data, "model_dump"):
        rec = data.model_dump()
    elif isinstance(data, dict):
        rec = data
    else:
        rec = dict(data)

    symbol = rec.get("symbol", "N/A")
    category = rec.get("category", "OPTION")
    action = rec.get("action", "BUY")
    option_type = rec.get("option_type")
    strike_price = rec.get("strike_price")
    
    entry_range = rec.get("entry_range") or []
    entry_str = " - ".join(map(str, entry_range)) if entry_range else "N/A"
    
    targets = rec.get("targets") or []
    targets_str = ", ".join(map(str, targets)) if targets else "N/A"
    
    stop_loss = rec.get("stop_loss") or "N/A"
    timeframe = rec.get("timeframe") or "N/A"
    source_channel = rec.get("source_channel", "Telegram Listener")
    raw_text = rec.get("raw_text", "").strip()

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
    
    Args:
        data: Recommendation dict or SignalSchema object
        bot_token: Optional override for TELEGRAM_ALERT_BOT_TOKEN
        chat_id: Optional override for TELEGRAM_ALERT_CHAT_ID
        
    Returns:
        bool: True if alert sent successfully, False otherwise.
    """
    token = bot_token or TELEGRAM_ALERT_BOT_TOKEN
    target_chat = chat_id or TELEGRAM_ALERT_CHAT_ID

    if not token or not target_chat:
        logger.debug("Telegram alert bot credentials not set. Skipping push notification.")
        return False

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

        with urllib.request.urlopen(req, timeout=8) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
            if res_data.get("ok"):
                logger.info("Successfully dispatched Telegram push alert to chat_id=%s", target_chat)
                return True
            else:
                logger.error("Failed to send Telegram alert: %s", res_data)
                return False

    except Exception as exc:
        logger.error("Error dispatching Telegram push notification: %s", exc)
        return False
