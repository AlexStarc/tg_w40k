"""Local meme mining from Telegram channels, without the server VM.

The bot's harvest (main.harvest_channels) rides on Pyrogram, which cannot
use MTProto proxies — and MTProto is the ONLY Telegram route from RF Macs.
This driver uses Telethon (which speaks MTProto proxies natively) for the
fetch step and reuses the bot's own GLM extraction (main.
_extract_pairs_from_posts → meme.append_entry) for the mine step, so what
lands in bank.json is identical to a server-side harvest.

Subcommands:
  login   [--code NNNNN] [--password P]   one-time auth (code goes to your TG)
  fetch   --channel @name [--limit 100]   pull post captions to memes_mine/
  extract FILES... [--per-channel 3]      GLM-extract pairs into bank.json

Proxies (FakeTLS 'ee' hex or base64url accepted, first working wins):
  --proxy host:port:secret[,host:port:secret...]  or env TG_MTPROXY
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

MINE_DIR = Path(__file__).parent / "memes_mine"

# config.py exits without these; the local flow needs none of them for real.
for _k, _v in (("BOT_TOKEN", "dummy"), ("ADMIN_ID", "1"), ("CHAT_ID", "-100")):
    os.environ.setdefault(_k, _v)
load_dotenv()

TG_SESSION = "tg_w40k_mine"  # keep separate from the bot's pyrogram session


def _norm_secret(s: str) -> str:
    """t.me secrets come as hex ('ee...') or base64url; Telethon wants hex."""
    s = s.strip().strip("/")
    if len(s) >= 16 and len(s) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in s):
        return s.lower()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).hex()


def _proxies(arg: str | None) -> list[tuple[str, int, str]]:
    raw = arg or os.getenv("TG_MTPROXY") or ""
    out = []
    for item in [x for x in raw.split(",") if x.strip()]:
        host, _, rest = item.strip().partition(":")
        port, _, secret = rest.partition(":")
        out.append((host, int(port), _norm_secret(secret)))
    if not out:
        sys.exit("no proxies: pass --proxy host:port:secret or set TG_MTPROXY")
    return out


def _make_client(proxy: tuple[str, int, str]):
    from telethon import TelegramClient, connection
    api_id = os.getenv("TG_API_ID")
    api_hash = os.getenv("TG_API_HASH")
    if not (api_id and api_hash):
        sys.exit("TG_API_ID / TG_API_HASH are not set (put them in .env)")
    return TelegramClient(
        TG_SESSION, int(api_id), api_hash,
        connection=connection.ConnectionTcpMTProxyRandomizedIntermediate,
        proxy=proxy,
    )


async def _connect_any(proxies):
    last = None
    for p in proxies:
        print(f"пробую {p[0]}:{p[1]} ...", file=sys.stderr)
        client = _make_client(p)
        try:
            await asyncio.wait_for(client.connect(), 25)
            if await client.is_user_authorized():
                print(f"ok: {p[0]}:{p[1]}", file=sys.stderr)
            else:
                print("connected; not authorized", file=sys.stderr)
            return client  # authorized or not, login flow continues here
        except Exception as e:
            last = e
            print(f"  fail: {type(e).__name__}: {e}", file=sys.stderr)
            try:
                await client.disconnect()
            except Exception:
                pass
    sys.exit(f"all proxies failed; last: {last}")


async def cmd_login(args) -> None:
    from telethon.errors import SessionPasswordNeededError
    client = await _connect_any(_proxies(args.proxy))
    me = await client.get_me()
    if me:
        print(f"already authorized as {me.first_name} (@{me.username})")
        return
    phone = os.getenv("TG_PHONE") or sys.exit("TG_PHONE is not set (e.g. +7...)")
    if not args.code:
        await client.sign_in(phone=phone)
        print(f"код отправлен в Telegram на {phone}; повтори: "
              f"mine_local.py login --code NNNNN [--password P]")
        return
    try:
        await client.sign_in(phone=phone, code=args.code)
    except SessionPasswordNeededError:
        if not args.password:
            sys.exit("2FA включена: добавь --password")
        await client.sign_in(password=args.password)
    me = await client.get_me()
    print(f"logged in as {me.first_name} (@{me.username}); "
          f"session '{TG_SESSION}' сохранена")


def _chan_file(channel: str) -> Path:
    MINE_DIR.mkdir(parents=True, exist_ok=True)
    return MINE_DIR / f"{channel.lstrip('@').replace('/', '_')}.json"


async def cmd_fetch(args) -> None:
    client = await _connect_any(_proxies(args.proxy))
    out = _chan_file(args.channel)
    seen = {p["id"]: p for p in (json.loads(out.read_text()) if out.exists() else [])}
    n = 0
    async for m in client.iter_messages(args.channel, limit=args.limit):
        if m.action or not (m.text or m.media):
            continue
        seen[m.id] = {"id": m.id, "text": m.text or "", "date": str(m.date or ""),
                      "has_media": bool(m.media)}
        n += 1
    posts = sorted(seen.values(), key=lambda p: p["id"])
    out.write_text(json.dumps(posts, ensure_ascii=False, indent=1))
    with_text = sum(1 for p in posts if p["text"])
    print(f"{args.channel}: +{n} новых, всего {len(posts)} "
          f"(с текстом {with_text}, image-only без OCR пропущены) → {out}")
    await client.disconnect()


async def cmd_extract(args) -> None:
    import meme
    from main import _extract_pairs_from_posts
    total = 0
    for f in args.files:
        channel = "@" + Path(f).stem
        posts = [p for p in json.loads(Path(f).read_text()) if p["text"]]
        if not posts:
            print(f"{channel}: нет текстовых постов, скип")
            continue
        ns = [type("P", (), {"tg_id": p["id"], "text": p["text"]})() for p in posts]
        added = await _extract_pairs_from_posts(channel, ns, args.per_channel)
        total += added
        print(f"{channel}: извлечено {added} пар из {len(ns)} постов")
    meme.cap_bank()
    bank = meme.load_bank()
    print(f"\nbank.json: {len(bank)} записей (+{total} новых)")
    for e in (bank[-total:] if total else []):
        print(f"  [{e.tone}] «{e.quote[:44]}» → {e.punchline[:44]}  ({e.source})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("login", help="one-time Telegram auth")
    p.add_argument("--code", help="login code from Telegram")
    p.add_argument("--password", help="2FA password if enabled")
    p.add_argument("--proxy")
    p.set_defaults(fn=cmd_login)

    p = sub.add_parser("fetch", help="pull channel posts")
    p.add_argument("--channel", required=True)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--proxy")
    p.set_defaults(fn=cmd_fetch)

    p = sub.add_parser("extract", help="GLM-extract pairs into bank.json")
    p.add_argument("files", nargs="+")
    p.add_argument("--per-channel", type=int, default=3)
    p.set_defaults(fn=cmd_extract)

    args = ap.parse_args()
    asyncio.run(args.fn(args))


if __name__ == "__main__":
    main()
