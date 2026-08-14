"""
telegram_listener.py
--------------------
Asynchronous Telegram Listener using Telethon.
Monitors public and private Telegram channels/groups in real-time.
Parses advisory messages (text, image captions, PDF descriptions) using SignalParser
and persists validated recommendations to the database with deduplication and FloodWait resilience.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Union

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.types import Channel, Chat, User

from config import (
    TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE,
    TELEGRAM_SESSION_NAME, TELEGRAM_SESSION_STRING, TELEGRAM_CHANNELS
)
from storage import init_db, save_recommendation
from parser import SignalParser

logger = logging.getLogger("stock_recommendation.telegram_listener")


class TelegramListenerService:
    """Async service to monitor Telegram channels and process stock recommendations."""

    def __init__(self, channels: List[str] = None):
        self.api_id = TELEGRAM_API_ID
        self.api_hash = TELEGRAM_API_HASH
        self.phone = TELEGRAM_PHONE
        self.session_name = TELEGRAM_SESSION_NAME
        self.session_string = TELEGRAM_SESSION_STRING
        self.channels = channels or TELEGRAM_CHANNELS
        
        self.parser = SignalParser(use_gemini_fallback=True)
        self.client: TelegramClient = None

    async def initialize(self):
        """Initialize Telethon client and establish session connection."""
        if not self.api_id or not self.api_hash or self.api_id == 0 or "your_" in str(self.api_hash).lower():
            raise ValueError(
                "Missing or invalid TELEGRAM_API_ID / TELEGRAM_API_HASH! "
                "Please configure real credentials from https://my.telegram.org in environment variables."
            )

        if self.session_string:
            from telethon.sessions import StringSession
            logger.info("Initializing Telethon client with StringSession...")
            self.client = TelegramClient(StringSession(self.session_string), self.api_id, self.api_hash)
            await self.client.connect()
            if await self.client.is_user_authorized():
                logger.info("Successfully authenticated with Telegram API via StringSession!")
                return
            else:
                logger.error("TELEGRAM_SESSION_STRING is invalid or expired.")
                raise ValueError("Provided TELEGRAM_SESSION_STRING is not authorized or has expired. Please regenerate your session string.")

        # Local fallback using session file or interactive login
        import sys
        is_interactive = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()

        if not is_interactive:
            logger.error(
                "Telegram session is not authorized and server is running in non-interactive mode. "
                "Set TELEGRAM_SESSION_STRING in environment variables."
            )
            raise ValueError("TELEGRAM_SESSION_STRING environment variable is required for cloud / headless deployment on Railway.")

        logger.info("Initializing Telethon client with session file '%s'...", self.session_name)
        self.client = TelegramClient(self.session_name, self.api_id, self.api_hash)

        phone_val = self.phone.strip() if (self.phone and isinstance(self.phone, str) and self.phone.strip()) else None
        phone_param = phone_val or (lambda: input("Enter Telegram phone number (e.g. +919876543210): "))

        await self.client.start(phone=phone_param)
        logger.info("Successfully authenticated with Telegram API!")

    async def resolve_channels(self) -> List[Union[Channel, Chat]]:
        """Resolve channel handles/IDs into Telethon entity objects."""
        resolved_entities = []
        for ch in self.channels:
            try:
                # Handle numeric channel IDs
                target = int(ch) if (ch.startswith("-") or ch.isdigit()) else ch
                entity = await self.client.get_entity(target)
                resolved_entities.append(entity)
                title = getattr(entity, "title", getattr(entity, "username", ch))
                logger.info("Monitoring resolved channel: '%s' (ID: %s)", title, getattr(entity, "id", "N/A"))
            except Exception as exc:
                logger.error("Could not resolve channel '%s': %s", ch, exc)
        return resolved_entities

    def register_handlers(self, chats=None):
        """Register asynchronous event listeners for new incoming messages and media."""
        monitored_chats = chats if chats is not None else self.channels
        
        @self.client.on(events.NewMessage(chats=monitored_chats if monitored_chats else None))
        async def on_new_message(event: events.NewMessage.Event):
            try:
                message = event.message
                text = message.text or message.message or ""

                # Extract channel/group name
                sender = await event.get_chat()
                source_channel = getattr(sender, "title", None) or getattr(sender, "username", None) or "Telegram Channel"

                # Check for media attachments (Images / PDFs / Documents)
                media_info = ""
                if message.media:
                    if message.photo:
                        media_info = "[Image Attached]"
                    elif message.document:
                        file_name = getattr(message.file, "name", "document")
                        media_info = f"[File Attached: {file_name}]"

                if not text.strip():
                    if media_info:
                        text = f"{media_info} (No text caption provided)"
                    else:
                        return

                logger.info("Received new message from [%s] (ID: %d): %s...", 
                            source_channel, message.id, text[:60].replace("\n", " "))

                # Timestamp
                msg_time = message.date.astimezone(timezone.utc).isoformat() if message.date else datetime.now(timezone.utc).isoformat()

                # Parse signal using Hybrid Parser
                recommendation = self.parser.parse_message(
                    text=text,
                    source_channel=source_channel,
                    timestamp=msg_time
                )

                # Persist to database with deduplication
                saved_record = save_recommendation(
                    schema=recommendation,
                    telegram_message_id=message.id
                )
                if saved_record:
                    logger.info("Successfully ingested: %s | %s | %s", 
                                saved_record.symbol, saved_record.category, saved_record.action)
                    try:
                        if saved_record.category != "REPORT":
                            from notifier import send_telegram_alert
                            send_telegram_alert(saved_record)
                        else:
                            logger.info("Skipping Telegram alert for REPORT category.")
                    except Exception as notify_err:
                        logger.error("Failed to send push alert: %s", notify_err)

            except FloodWaitError as flood_err:
                logger.warning("Telegram FloodWait rate limit hit! Sleeping for %d seconds...", flood_err.seconds)
                await asyncio.sleep(flood_err.seconds)
            except RPCError as rpc_err:
                logger.error("Telegram RPC Error: %s", rpc_err)
            except Exception as exc:
                logger.error("Unexpected error processing message: %s", exc, exc_info=True)

    async def start_listening(self):
        """Main async loop to start the Telegram listener with auto-reconnect."""
        init_db()
        await self.initialize()
        resolved_entities = await self.resolve_channels()
        
        if resolved_entities:
            logger.info("Registered %d resolved channel entities for event listening.", len(resolved_entities))
            self.register_handlers(chats=resolved_entities)
        else:
            logger.warning("No valid channel entities resolved. Monitoring all account messages...")
            self.register_handlers(chats=None)

        logger.info("=====================================================")
        logger.info("Telegram Advisory Listener is live and monitoring!")
        logger.info("Channels: %s", ", ".join(self.channels))
        logger.info("=====================================================")

        # Launch automated daily cleanup task (End of Day - 12:00 AM IST)
        async def daily_cleanup_loop():
            from datetime import timedelta
            ist_offset = timezone(timedelta(hours=5, minutes=30))
            from storage import clear_all_recommendations
            while True:
                now_ist = datetime.now(ist_offset)
                # Target midnight (00:00 IST) for end of day
                target = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
                if now_ist >= target:
                    target += timedelta(days=1)
                
                sleep_secs = (target - now_ist).total_seconds()
                logger.info("Next end-of-day auto-cleanup scheduled in %.1f hours.", sleep_secs / 3600)
                await asyncio.sleep(sleep_secs)
                
                logger.info("Executing scheduled end-of-day database cleanup...")
                clear_all_recommendations()

        asyncio.create_task(daily_cleanup_loop())

        # Run Telethon client until disconnected
        await self.client.run_until_disconnected()


def run_telegram_listener():
    """Synchronous entrypoint helper to run the async listener loop."""
    service = TelegramListenerService()
    try:
        asyncio.run(service.start_listening())
    except KeyboardInterrupt:
        logger.info("Telegram Listener service stopped by user.")
    except Exception as exc:
        logger.critical("Fatal error in Telegram Listener service: %s", exc, exc_info=True)
        raise exc


if __name__ == "__main__":
    run_telegram_listener()
