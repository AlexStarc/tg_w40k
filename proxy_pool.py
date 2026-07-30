"""Fallback SOCKS5 proxy pool.

When all configured TG_PROXIES fail at startup, this module samples a public
SOCKS5 list, tests a random subset concurrently for connectivity to
api.telegram.org, and returns the first one that works. The result is cached
in the DB (settings.last_pool_proxy) so the next startup fast-paths through it.

Strategy:
  1. Try cached last_pool_proxy from DB (fast).
  2. Load socks5 list from disk cache if fresh (<PROXY_POOL_CACHE_TTL>),
     otherwise re-download from PROXY_REMOTE_SOURCES.
  3. Sample N candidates, test concurrently, return first reachable.

This is a LAST resort: public SOCKS5 proxies are unreliable and Telegram often
bans them. The bot logs every step so failure modes are observable.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from pathlib import Path

import httpx

import config
from database import get_setting, set_setting

logger = logging.getLogger(__name__)

HERE = Path(__file__).parent
POOL_CACHE = HERE / "proxies_pool.txt"
TEST_URL = "https://api.telegram.org"


def _cache_fresh() -> bool:
    if not POOL_CACHE.exists():
        return False
    ttl = getattr(config, "PROXY_POOL_CACHE_TTL", 86400)
    age = time.time() - POOL_CACHE.stat().st_mtime
    return age < ttl


async def _download_pool() -> list[str]:
    """Download socks5 lists from configured sources, dedup host:port lines."""
    sources = getattr(config, "PROXY_REMOTE_SOURCES", []) or []
    out: set[str] = set()
    for url in sources:
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get(url)
                r.raise_for_status()
                for line in r.text.splitlines():
                    line = line.strip()
                    if line and ":" in line and not line.startswith("#"):
                        out.add(line)
        except Exception:
            logger.exception("Failed to download proxy list from %s", url)
    if out:
        try:
            POOL_CACHE.write_text("\n".join(sorted(out)), encoding="utf-8")
        except Exception:
            logger.exception("Failed to write proxy pool cache")
        logger.info("Proxy pool refreshed: %d entries from %d sources", len(out), len(sources))
    return list(out)


async def _load_pool() -> list[str]:
    """Return pool entries — disk cache if fresh, else re-download."""
    if _cache_fresh():
        try:
            return [line for line in POOL_CACHE.read_text(encoding="utf-8").splitlines() if line.strip()]
        except Exception:
            logger.exception("Proxy pool cache read failed; re-downloading")
    return await _download_pool()


async def _test_socks5(host: str, port: int, timeout: float) -> bool:
    """Quick connectivity test through a SOCKS5 proxy to TEST_URL."""
    import aiohttp
    from aiohttp_socks import ProxyConnector
    try:
        connector = ProxyConnector.from_url(f"socks5://{host}:{port}")
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as session:
            async with session.get(TEST_URL) as r:
                return r.status < 500
    except Exception:
        return False


async def _find_working(entries: list[str], sample_size: int,
                        timeout: float, concurrency: int) -> str | None:
    """Sample entries, test concurrently, return first 'socks5://host:port'."""
    sample = random.sample(entries, min(sample_size, len(entries)))
    sem = asyncio.Semaphore(concurrency)
    working: asyncio.Future = asyncio.get_event_loop().create_future()

    async def check(entry: str) -> None:
        parts = entry.split(":")
        if len(parts) != 2:
            return
        host = parts[0].strip()
        try:
            port = int(parts[1])
        except ValueError:
            return
        async with sem:
            if working.done():
                return
            ok = await _test_socks5(host, port, timeout)
            if ok and not working.done():
                working.set_result(f"socks5://{host}:{port}")

    tasks = [asyncio.create_task(check(e)) for e in sample]
    try:
        await asyncio.wait_for(asyncio.shield(working), timeout=timeout * (len(sample) // concurrency + 1) + 5)
    except asyncio.TimeoutError:
        pass
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
    return working.result() if working.done() and not working.cancelled() else None


async def find_working_proxy(configured: list[str] | None = None) -> str | None:
    """Return a working socks5:// URL, or None if none found.

    Order: cached last_pool_proxy from DB (fast) → fresh sample from the pool.
    `configured` is used only to skip re-testing entries already in TG_PROXIES.
    """
    configured = configured or []
    sample_size = getattr(config, "PROXY_POOL_SAMPLE", 100)
    timeout = getattr(config, "PROXY_POOL_TIMEOUT", 4.0)
    concurrency = getattr(config, "PROXY_POOL_CONCURRENCY", 20)

    # 1. Cached last working proxy
    cached = None
    try:
        cached = await get_setting("last_pool_proxy")
    except Exception:
        logger.exception("Failed to read cached pool proxy")
    if cached and cached not in configured:
        host_port = cached.replace("socks5://", "")
        host, _, port = host_port.partition(":")
        try:
            port_i = int(port)
        except ValueError:
            port_i = 0
        if host and port_i:
            if await _test_socks5(host, port_i, timeout):
                logger.info("Cached pool proxy still works: %s", cached)
                return cached
            logger.info("Cached pool proxy no longer works: %s", cached)

    # 2. Remote pool
    entries = await _load_pool()
    if not entries:
        logger.warning("Proxy pool is empty; no remote source succeeded")
        return None

    logger.info("Testing %d random proxies from pool (of %d)...", sample_size, len(entries))
    working = await _find_working(entries, sample_size, timeout, concurrency)
    if working:
        try:
            await set_setting("last_pool_proxy", working)
        except Exception:
            logger.exception("Failed to cache working pool proxy")
        logger.info("Found working pool proxy: %s", working)
    else:
        logger.warning("No working proxy found in pool sample of %d", sample_size)
    return working
