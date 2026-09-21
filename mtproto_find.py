"""Find ALIVE MTProto proxies for the Telegram mobile client.

Unlike mtproto_socks_tester.py (which finds SOCKS5 that reach Telegram DCs
for the bot's Pyrogram), this one validates real MTProto proxies
(host+port+secret) that a phone/desktop Telegram client can use directly.

Source: auto-updated lists of t.me/proxy?... links (default:
SoliSpirit/mtproto, refreshed every 12h by upstream). Liveness check is a
plain TCP connect to the proxy host:port — MTProto servers that accept TCP
are overwhelmingly usable; the client confirms the secret on connect.

Run:
    python mtproto_find.py                 # top-5 alive links
    python mtproto_find.py --limit 40      # check first 40 entries
    python mtproto_find.py --count 10      # print up to 10 alive
    python mtproto_find.py --src URL       # custom list of t.me/proxy links
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from urllib.parse import parse_qs, urlparse

import httpx

DEFAULT_SRC = "https://raw.githubusercontent.com/SoliSpirit/mtproto/master/all_proxies.txt"
LINK_RE = re.compile(r"https?://t\.me/proxy\?[^\"'\s<>]+")

# Telegram DC endpoints are unreachable from some networks; checking the
# PROXY host directly avoids that problem entirely.


def parse_links(text: str) -> list[tuple[str, int, str, str]]:
    """Extract (host, port, secret, full_link) from t.me/proxy links."""
    out = []
    seen: set[str] = set()
    for link in LINK_RE.findall(text):
        q = parse_qs(urlparse(link).query)
        host = (q.get("server") or [""])[0].rstrip(".")
        port = (q.get("port") or [""])[0]
        secret = (q.get("secret") or [""])[0]
        if not host or not port.isdigit() or not secret:
            continue
        if host in seen:
            continue
        seen.add(host)
        out.append((host, int(port), secret, link))
    return out


async def check(entry: tuple, sem: asyncio.Semaphore, timeout: float) -> tuple | None:
    host, port = entry[0], entry[1]
    async with sem:
        try:
            r, w = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout)
            w.close()
            return entry
        except Exception:
            return None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=DEFAULT_SRC, help="list of t.me/proxy links (raw URL)")
    ap.add_argument("--limit", type=int, default=60, help="check first N entries")
    ap.add_argument("--count", type=int, default=5, help="print up to N alive links")
    ap.add_argument("--timeout", type=float, default=5.0, help="per-proxy TCP timeout")
    args = ap.parse_args()

    try:
        r = await httpx.AsyncClient(timeout=20, follow_redirects=True).get(args.src)
        r.raise_for_status()
        raw = r.text
    except Exception as e:
        print(f"Failed to download list: {e}", file=sys.stderr)
        return 1

    entries = parse_links(raw)
    if args.limit:
        entries = entries[:args.limit]
    print(f"Parsed {len(entries)} unique MTProto proxies; TCP-checking...", file=sys.stderr)

    sem = asyncio.Semaphore(40)
    alive = []
    for i in range(0, len(entries), 40):
        batch = entries[i:i + 40]
        results = await asyncio.gather(*[check(e, sem, args.timeout) for e in batch])
        alive += [x for x in results if x]
        if len(alive) >= args.count:
            break

    print(f"Alive: {len(alive)} of {len(entries)} checked")
    for host, port, secret, link in alive[:args.count]:
        print(link)
    return 0 if alive else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
