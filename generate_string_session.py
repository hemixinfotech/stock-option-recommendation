"""
generate_string_session.py
---------------------------
Helper utility to generate a Telethon StringSession string.
Copy and paste the generated string into your Railway environment variable (TELEGRAM_SESSION_STRING)
to run the Telegram Listener headlessly in the cloud without session file permission issues.
"""

import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession
from config import TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE

async def generate():
    print("=" * 60)
    print("🔑 Telethon StringSession Generator for Cloud / Railway")
    print("=" * 60)

    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH or TELEGRAM_API_ID == 0:
        print("❌ Error: Please configure TELEGRAM_API_ID & TELEGRAM_API_HASH in .env first.")
        return

    phone_param = TELEGRAM_PHONE.strip() if (TELEGRAM_PHONE and TELEGRAM_PHONE.strip()) else None
    client = TelegramClient(StringSession(), TELEGRAM_API_ID, TELEGRAM_API_HASH)
    
    await client.start(phone=phone_param)
    session_str = client.session.save()
    
    print("\n✅ Successfully Authenticated!")
    print("Here is your TELEGRAM_SESSION_STRING:\n")
    print("-" * 60)
    print(session_str)
    print("-" * 60)
    print("\n👉 Copy the above string and add it to your Railway Variables as:")
    print("   TELEGRAM_SESSION_STRING = <paste string here>")

    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(generate())
