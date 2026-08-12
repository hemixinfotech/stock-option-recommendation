"""
verify_channels.py
------------------
CLI verification utility to test whether all Telegram channels/groups
specified in .env (TELEGRAM_CHANNELS) are valid and accessible by your account.
"""

import asyncio
import sys
import logging
from telethon import TelegramClient
from telethon.errors import RPCError

from config import (
    TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE,
    TELEGRAM_SESSION_NAME, TELEGRAM_CHANNELS
)

logging.basicConfig(level=logging.WARNING)


async def check_channels():
    print("=" * 60)
    print("🔍 Telegram Channel & Group Verification Utility")
    print("=" * 60)

    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH or TELEGRAM_API_ID == 0:
        print("❌ ERROR: Missing or invalid TELEGRAM_API_ID / TELEGRAM_API_HASH in .env!")
        print("Please configure your credentials from https://my.telegram.org in .env first.")
        return

    print(f"Connecting using session: '{TELEGRAM_SESSION_NAME}'...")
    client = TelegramClient(TELEGRAM_SESSION_NAME, TELEGRAM_API_ID, TELEGRAM_API_HASH)

    phone_param = TELEGRAM_PHONE.strip() if (TELEGRAM_PHONE and TELEGRAM_PHONE.strip()) else lambda: input("Enter phone number: ")
    await client.start(phone=phone_param)

    print(f"\nChecking {len(TELEGRAM_CHANNELS)} channels/groups listed in TELEGRAM_CHANNELS:\n")

    valid_count = 0
    invalid_count = 0

    for ch in TELEGRAM_CHANNELS:
        try:
            target = int(ch) if (ch.startswith("-") or ch.isdigit()) else ch
            entity = await client.get_entity(target)

            title = getattr(entity, "title", getattr(entity, "username", ch))
            ch_id = getattr(entity, "id", "N/A")
            participants = getattr(entity, "participants_count", "N/A")
            ch_type = type(entity).__name__

            print(f"  ✅ VALID: {ch:<25} ➔  Name: '{title}' (ID: {ch_id}, Type: {ch_type})")
            valid_count += 1
        except RPCError as rpc_err:
            print(f"  ❌ INVALID / UNREACHABLE: {ch:<20} ➔ Error: {rpc_err.message}")
            invalid_count += 1
        except Exception as exc:
            print(f"  ❌ INVALID / UNREACHABLE: {ch:<20} ➔ Error: {exc}")
            invalid_count += 1

    print("\n" + "=" * 60)
    print(f"📊 Summary: {valid_count} Valid | {invalid_count} Invalid/Unreachable")
    print("=" * 60)

    await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(check_channels())
    except KeyboardInterrupt:
        print("\nVerification cancelled by user.")
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
