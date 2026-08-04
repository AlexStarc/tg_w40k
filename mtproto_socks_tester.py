"""Quick test: which SOCKS5 proxies from a public list can actually reach
Telegram's MTProto DC at 149.154.167.51:443?

The bot's proxy_pool only tests HTTP reachability to api.telegram.org, which
is enough for aiogram but NOT for Pyrogram/Telethon — they need MTProto DC
access, which Telegram aggressively filters by source IP.

Run:
    python mtproto_socks_tester.py
    python mtproto_socks_tester.py --limit 200          # only first 200
    python mtproto_socks_tester.py --src https://...    # custom list

Output: prints every working proxy live, then a summary at the end.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import sys
from concurrent.futures import ThreadPoolExecutor

import httpx
import socks

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("mtproto_tester")

# Telegram DC2 (Amsterdam) — the main DC that client libraries hit first.
DC_HOST = "149.154.167.51"
DC_PORT = 443
DEFAULT_SRC = "https://raw.githubusercontent.com/ProxyScraper/ProxyScraper/main/socks5.txt"
WORKERS = 50
TIMEOUT = 5


async def fetch_list(url: str) -> list[str]:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url)
        r.raise_for_status()
        return [line.strip() for line in r.text.splitlines()
                if line.strip() and ":" in line and not line.startswith("#")]


def _test_one(host: str, port: int, timeout: int) -> bool:
    """Try to open a TCP connection to the MTProto DC through a SOCKS5 proxy.
    We don't need to speak MTProto — TCP connect success means the proxy IP is
    not blocked by Telegram's edge filters."""
    s = socks.socksocket(socket.AF_INET)
    s.set_proxy(socks.SOCKS5, host, port)
    s.settimeout(timeout)
    try:
        s.connect((DC_HOST, DC_PORT))
        s.close()
        return True
    except Exception:
        return False


async def test_all(entries: list[str], timeout: int) -> list[str]:
    sem = asyncio.Semaphore(WORKERS)
    loop = asyncio.get_event_loop()
    pool = ThreadPoolExecutor(max_workers=WORKERS)
    working: list[str] = []
    tested = 0
    lock = asyncio.Lock()

    async def check(entry: str) -> None:
        nonlocal tested
        parts = entry.split(":")
        if len(parts) != 2:
            return
        host = parts[0].strip()
        try:
            port = int(parts[1])
        except ValueError:
            return
        async with sem:
            ok = await loop.run_in_executor(pool, _test_one, host, port, timeout)
        async with lock:
            tested += 1
            if tested % 50 == 0:
                log.info("tested %d/%d, working so far: %d", tested, len(entries), len(working))
            if ok:
                url = f"socks5://{host}:{port}"
                working.append(url)
                print(f"WORKING: {url}", flush=True)

    await asyncio.gather(*[check(e) for e in entries])
    return working


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", default=DEFAULT_SRC, help="URL of socks5 list")
    parser.add_argument("--limit", type=int, default=0,
                        help="Test only first N entries (0 = all)")
    parser.add_argument("--timeout", type=int, default=TIMEOUT,
                        help=f"Per-proxy timeout in seconds (default {TIMEOUT})")
    args = parser.parse_args()

    log.info("Downloading %s ...", args.src)
    try:
        entries = await fetch_list(args.src)
    except Exception as e:
        print(f"Failed to download list: {e}", file=sys.stderr)
        return 1
    if args.limit:
        entries = entries[:args.limit]
    log.info("Got %d proxies. Testing each against %s:%d (workers=%d, timeout=%ds)",
             len(entries), DC_HOST, DC_PORT, WORKERS, args.timeout)

    working = await test_all(entries, args.timeout)
    print("\n=== RESULT ===", flush=True)
    print(f"Tested: {len(entries)}", flush=True)
    print(f"Working: {len(working)}", flush=True)
    for url in working:
        print(f"  {url}", flush=True)
    return 0 if working else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
