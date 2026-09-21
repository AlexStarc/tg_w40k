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
SOCKS_HEALTH_SRC = "https://raw.githubusercontent.com/xyzs996/free-proxy-health-list/main/socks5.txt"
LINK_RE = re.compile(r"https?://t\.me/proxy\?[^\"'\s<>]+")


async def _alive_socks(count: int = 3, timeout: float = 4.0) -> list[str]:
    """A few SOCKS5 proxies reachable from THIS machine (plain TCP probe).
    Used in --via-socks mode to tunnel MTProto checks when the local network
    itself blocks Telegram-adjacent hosts (RF/TSPU-style filtering)."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(SOCKS_HEALTH_SRC)
            r.raise_for_status()
            cands = [line.strip() for line in r.text.splitlines() if line.strip()][:60]
    except Exception:
        return []
    out: list[str] = []
    for entry in cands:
        host, _, port = entry.partition(":")
        if not port.isdigit():
            continue
        try:
            rr, ww = await asyncio.wait_for(
                asyncio.open_connection(host, int(port)), timeout)
            ww.close()
            out.append(entry)
            if len(out) >= count:
                break
        except Exception:
            continue
    return out


async def _tcp_via_socks(entry_host: str, entry_port: int,
                         socks_url: str, timeout: float) -> bool:
    """TCP-connect to host:port THROUGH a SOCKS5 tunnel (python-socks)."""
    from python_socks.async_.asyncio import Proxy
    try:
        proxy = Proxy.from_url(f"socks5://{socks_url}")
        sock = await asyncio.wait_for(
            proxy.connect(dest_host=entry_host, dest_port=entry_port,
                          timeout=timeout),
            timeout + 2)
        sock.close()
        return True
    except Exception:
        return False

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


async def check(entry: tuple, sem: asyncio.Semaphore, timeout: float,
                via_socks: str | None = None) -> tuple | None:
    host, port = entry[0], entry[1]
    async with sem:
        if via_socks:
            ok = await _tcp_via_socks(host, port, via_socks, timeout)
        else:
            try:
                r, w = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout)
                w.close()
                ok = True
            except Exception:
                ok = False
        return entry if ok else None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=DEFAULT_SRC, help="list of t.me/proxy links (raw URL)")
    ap.add_argument("--limit", type=int, default=60, help="check first N entries")
    ap.add_argument("--count", type=int, default=5, help="print up to N alive links")
    ap.add_argument("--timeout", type=float, default=5.0, help="per-proxy TCP timeout")
    ap.add_argument("--via-socks", action="store_true",
                    help="tunnel checks through a reachable SOCKS5 (for networks "
                         "that block Telegram-adjacent hosts directly, e.g. RF)")
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

    via: str | None = None
    if args.via_socks:
        socks = await _alive_socks(3)
        if not socks:
            print("No reachable SOCKS5 to tunnel through; falling back to direct",
                  file=sys.stderr)
        else:
            via = socks[0]
            print(f"Tunneling checks through SOCKS5 {via}", file=sys.stderr)

    print(f"Parsed {len(entries)} unique MTProto proxies; TCP-checking"
          + (f" via {via}" if via else " directly") + "...", file=sys.stderr)

    sem = asyncio.Semaphore(40)
    alive = []
    for i in range(0, len(entries), 40):
        batch = entries[i:i + 40]
        results = await asyncio.gather(*[check(e, sem, args.timeout, via) for e in batch])
        alive += [x for x in results if x]
        if len(alive) >= args.count:
            break

    print(f"Alive: {len(alive)} of {len(entries)} checked")
    for host, port, secret, link in alive[:args.count]:
        print(link)
    return 0 if alive else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
