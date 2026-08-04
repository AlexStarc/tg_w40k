"""One-shot interactive login that creates a Pyrogram .session file.

Run this ONCE before starting the bot:

    python auth_pyrogram.py

Pyrogram itself drives the interactive flow — phone, code from Telegram,
optional 2FA password. The resulting .session file (named <TG_SESSION>.session,
default 'tg_w40k_user.session') lives next to this script and is gitignored.

The bot reads the same session name read-only afterwards. No MTProto proxy is
configured for login — connect directly or via a SOCKS5 host set in the
PYROGRAM_LOGIN_PROXY env (e.g. socks5://127.0.0.1:1080). Once the session
file exists the bot connects through TG_PROXIES / TG_MTPROTO_PROXIES as usual.

Pre-requisites:
  - TG_API_ID and TG_API_HASH in .env (get them at https://my.telegram.org).
"""
import asyncio
import os
import sys
from urllib.parse import urlparse

from dotenv import load_dotenv
from pyrogram import Client

load_dotenv()

API_ID = os.getenv("TG_API_ID")
API_HASH = os.getenv("TG_API_HASH")
SESSION = os.getenv("TG_SESSION", "tg_w40k_user")


def _login_proxy() -> dict | None:
    """Optional proxy for the login flow only (env PYROGRAM_LOGIN_PROXY).
    Useful if Telegram is unreachable directly from this machine."""
    raw = os.getenv("PYROGRAM_LOGIN_PROXY", "").strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw)
    except Exception:
        return None
    scheme = (parsed.scheme or "").lower()
    host = parsed.hostname
    port = parsed.port
    if not host or not port:
        return None
    if scheme in ("socks5", "socks5h"):
        return {"scheme": "socks5", "hostname": host, "port": port}
    if scheme == "socks4":
        return {"scheme": "socks4", "hostname": host, "port": port}
    if scheme in ("http", "https"):
        return {"scheme": "http", "hostname": host, "port": port}
    return None


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

    proxy = _login_proxy()
    if proxy:
        print(f"Login will use proxy: {proxy}")
    else:
        print("Login will try a direct connection (set PYROGRAM_LOGIN_PROXY to override).")

    app = Client(
        SESSION,
        api_id=api_id_int,
        api_hash=API_HASH,
        proxy=proxy,
        no_updates=True,
        workdir=".",
    )
    # Pyrogram will prompt for phone / code / 2FA password automatically if the
    # session file doesn't exist or is unauthorized.
    await app.start()
    me = await app.get_me()
    uname = f"@{me.username}" if me.username else "(no username)"
    print(f"\n✅ Authorized as {me.first_name} ({uname}).")
    print(f"   Session file: {SESSION}.session — keep it private, it is gitignored.")
    await app.stop()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
