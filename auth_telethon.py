"""One-shot interactive login that creates a Telethon .session file.

Run this ONCE before starting the bot:

    python auth_telethon.py

The bot reads the same session name (configured via TG_SESSION, default
'tg_w40k_user') read-only afterwards. The .session file is created next to
this script (it is gitignored).

You'll be asked for:
  - phone (international format, e.g. +79991234567)
  - login code (sent by Telegram to that phone)
  - 2FA password (only if your account has cloud-password enabled)

Pre-requisites:
  - TG_API_ID and TG_API_HASH in .env
    (get them at https://my.telegram.org → API development tools)
"""
import asyncio
import os
import sys

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

load_dotenv()

API_ID = os.getenv("TG_API_ID")
API_HASH = os.getenv("TG_API_HASH")
SESSION = os.getenv("TG_SESSION", "tg_w40k_user")


async def main() -> int:
    if not API_ID or not API_HASH:
        print("ERROR: set TG_API_ID and TG_API_HASH in .env first.", file=sys.stderr)
        print("Get them at https://my.telegram.org → API development tools.", file=sys.stderr)
        return 1
    try:
        api_id_int = int(API_ID)
    except ValueError:
        print(f"ERROR: TG_API_ID must be an integer, got {API_ID!r}.", file=sys.stderr)
        return 1

    client = TelegramClient(SESSION, api_id_int, API_HASH)
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            uname = f"@{me.username}" if me.username else "(no username)"
            print(f"✅ Session '{SESSION}' already authorized as {me.first_name} ({uname}).")
            return 0

        phone = input("Phone (international, e.g. +79991234567): ").strip()
        if not phone:
            print("Phone is required.", file=sys.stderr)
            return 1
        await client.send_code_request(phone)
        code = input("Login code from Telegram: ").strip()
        try:
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            pwd = input("2FA password: ").strip()
            await client.sign_in(password=pwd)

        me = await client.get_me()
        uname = f"@{me.username}" if me.username else "(no username)"
        print(f"✅ Authorized as {me.first_name} ({uname}).")
        print(f"   Session file: {SESSION}.session — keep it private, it is gitignored.")
        return 0
    finally:
        await client.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
