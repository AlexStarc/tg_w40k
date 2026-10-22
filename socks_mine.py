"""SOCKS5 miner: harvest public lists, keep only proxies that reach Telegram.

Two stages, both through a real SOCKS5 handshake (python_socks), so a proxy
counts only if it actually tunnels:
  1. alive     — SOCKS5 tunnel to 1.1.1.1:80 (filters dead entries fast)
  2. telegram  — survivors tunneled to TG DC2 + DC5 (what Pyrogram needs)

Sources are hourly-refreshed GitHub lists; raw.githubusercontent is flaky
from RF, so every source has a jsdelivr fallback.

Output: socks_tg.txt (host:port, gitignored) — both-DC passers first.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
from python_socks.async_.asyncio import Proxy

OUT = Path(__file__).parent / "socks_tg.txt"

SOURCES = [
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
    "https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks5.txt",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5_RAW.txt",
    "https://raw.githubusercontent.com/xyzs996/free-proxy-health-list/main/socks5.txt",
]
JSDELIVR = "https://cdn.jsdelivr.net/gh/{repo}@{branch}/{path}"

DC2 = "149.154.167.51"   # Telegram production DC2
DC5 = "91.108.56.130"    # Telegram production DC5


async def _fetch(http: httpx.AsyncClient, url: str) -> list[str]:
    if "raw.githubusercontent.com" in url:
        repo, _, tail = url.split("githubusercontent.com/")[1].partition("/")
        branch, _, path = tail.partition("/")
        jurl = JSDELIVR.format(repo=repo, branch=branch, path=path)
    else:
        jurl = url
    for attempt_url in (url, jurl):
        try:
            r = await http.get(attempt_url, timeout=30)
            r.raise_for_status()
            return [ln.strip() for ln in r.text.splitlines() if ln.strip()]
        except Exception as e:
            print(f"  {attempt_url} -> {type(e).__name__}", file=sys.stderr)
    return []


async def _tunnel(proxy_url: str, host: str, port: int, timeout: float) -> bool:
    try:
        p = Proxy.from_url(proxy_url)
        sock = await asyncio.wait_for(
            p.connect(dest_host=host, dest_port=port, timeout=timeout), timeout + 2)
        sock.close()
        return True
    except Exception:
        return False


async def main() -> None:
    async with httpx.AsyncClient(timeout=30) as http:
        print("источники:", file=sys.stderr)
        batches = await asyncio.gather(*[_fetch(http, u) for u in SOURCES])
    cands = []
    seen = set()
    for b in batches:
        for ln in b:
            ln = ln.split("//")[-1].split("/")[0].strip()
            host, _, port = ln.partition(":")
            if host and port.isdigit() and 0 < int(port) < 65536:
                key = f"{host}:{port}"
                if key not in seen:
                    seen.add(key)
                    cands.append(key)
    print(f"уникальных: {len(cands)}; стадия 1 — живой SOCKS5...", file=sys.stderr)

    sem = asyncio.Semaphore(400)

    async def alive(key: str) -> str | None:
        async with sem:
            return key if await _tunnel(f"socks5://{key}", "1.1.1.1", 80, 3) else None

    live = [x for x in await asyncio.gather(*[alive(k) for k in cands]) if x]
    print(f"живых: {len(live)}; стадия 2 — Telegram DC...", file=sys.stderr)

    sem = asyncio.Semaphore(60)

    async def dc(key: str, host: str) -> bool:
        async with sem:
            return await _tunnel(f"socks5://{key}", host, 443, 7)

    r2 = await asyncio.gather(*[dc(k, DC2) for k in live])
    ok2 = [k for k, ok in zip(live, r2) if ok]
    r5 = await asyncio.gather(*[dc(k, DC5) for k in ok2])
    ok5 = {k for k, ok in zip(ok2, r5) if ok}
    both = [k for k in ok2 if k in ok5]

    print(f"DC2 ok: {len(ok2)}, DC2+DC5 ok: {len(both)}")
    OUT.write_text("\n".join(both + [k for k in ok2 if k not in ok5]) + "\n")
    print(f"сохранено {len(ok2)} → {OUT} (обе DC — первые)")


if __name__ == "__main__":
    asyncio.run(main())
