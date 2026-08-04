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
import logging
import os
import sys

from dotenv import load_dotenv
from pyrogram import Client

load_dotenv()

# PYROGRAM_DEBUG=1 enables pyrogram's DEBUG logs so the real connection
# failure (timeout / refused / bad secret) isn't masked by the NoneType bug.
if os.getenv("PYROGRAM_DEBUG"):
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("pyrogram").setLevel(logging.DEBUG)

API_ID = os.getenv("TG_API_ID")
API_HASH = os.getenv("TG_API_HASH")
SESSION = os.getenv("TG_SESSION", "tg_w40k_user")


def _login_proxy() -> dict | None:
    """Optional proxy for the login flow only (env PYROGRAM_LOGIN_PROXY).
    Useful if Telegram is unreachable directly from this machine.

    Accepted formats:
      socks5://host:port | socks4://host:port | http://host:port
      mtproto://host:port/secret   (Pyrogram-native MTProto proxy)
    If unset but TG_MTPROTO_PROXIES is set, falls back to the first entry
    there so the same MTProto used by the bot can authenticate the session."""
    from urllib.parse import urlparse

    raw = os.getenv("PYROGRAM_LOGIN_PROXY", "").strip()

    if raw:
        # mtproto://host:port/secret — custom scheme, parse by hand
        if raw.lower().startswith("mtproto://"):
            tail = raw[len("mtproto://"):]
            # tail = host:port/secret
            try:
                host_port, secret = tail.rsplit("/", 1)
                host, port = host_port.rsplit(":", 1)
                port_i = int(port)
            except ValueError:
                return None
            if host and port_i and secret:
                return {"hostname": host, "port": port_i, "secret": secret}
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

    # No explicit override — try the first TG_MTPROTO_PROXIES entry, if any.
    mts = os.getenv("TG_MTPROTO_PROXIES", "").strip()
    if not mts:
        return None
    first = mts.split(",", 1)[0].strip()
    parts = first.split(":")
    if len(parts) < 3:
        return None
    secret = parts[-1]
    try:
        port = int(parts[-2])
    except ValueError:
        return None
    host = ":".join(parts[:-2])
    if host and port and secret:
        return {"hostname": host, "port": port, "secret": secret}
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
    try:
        await app.start()
    except AttributeError as e:
        if "NoneType" in str(e):
            print(
                "\n❌ Pyrogram failed to connect via this proxy. Common causes:\n"
                "  - the MTProto proxy/secret is dead (they only live weeks)\n"
                "  - the network blocks the proxy port\n"
                "  - a Pyrogram 2.0.106 bug masking the real error.\n"
                "Run with PYROGRAM_DEBUG=1 to see the underlying connection error.",
                file=sys.stderr,
            )
        raise
    me = await app.get_me()
    uname = f"@{me.username}" if me.username else "(no username)"
    print(f"\n✅ Authorized as {me.first_name} ({uname}).")
    print(f"   Session file: {SESSION}.session — keep it private, it is gitignored.")
    await app.stop()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
