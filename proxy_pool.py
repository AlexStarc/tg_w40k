"""Fallback SOCKS5 proxy pool — two tiers.

When all configured TG_PROXIES fail, this module hunts for a working public
SOCKS5 proxy:

  Tier 1 (priority): small HEALTH-CHECKED lists (PROXY_PRIORITY_SOURCES,
  e.g. xyzs996/free-proxy-health-list — CI-verified upstream). Tested in
  full with high concurrency. A few hundred verified entries have a far
  better hit rate than a random slice of the raw pools.
  Tier 2 (bulk): big raw scraped lists (PROXY_REMOTE_SOURCES, ~100k+).
  Random sample of PROXY_POOL_SAMPLE entries.

Winners are cached in the DB (settings.last_pool_proxy for Bot-API HTTP,
settings.last_mtproto_proxy for Pyrogram MTProto-DC reachability) so the
next run fast-paths through them.

This is a LAST resort: public SOCKS5 proxies are unreliable and Telegram
often bans them. The bot logs every step so failure modes are observable.
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
PRIORITY_CACHE = HERE / "proxies_priority.txt"
TEST_URL = "https://api.telegram.org"


def _cache_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    ttl = getattr(config, "PROXY_POOL_CACHE_TTL", 86400)
    age = time.time() - path.stat().st_mtime
    return age < ttl


async def _download(urls: list[str]) -> list[str]:
    """Download socks5 lists from the given sources, dedup host:port lines."""
    out: set[str] = set()
    for url in urls:
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
    return list(out)


async def _load_list(urls: list[str], cache_path: Path, label: str) -> list[str]:
    """Return list entries — fresh disk cache if available, else download and
    refresh the cache."""
    if _cache_fresh(cache_path):
        try:
            return [line for line in cache_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except Exception:
            logger.exception("%s cache read failed; re-downloading", label)
    entries = await _download(urls)
    if entries:
        try:
            cache_path.write_text("\n".join(sorted(entries)), encoding="utf-8")
        except Exception:
            logger.exception("Failed to write %s cache", label)
        logger.info("%s refreshed: %d entries from %d sources", label, len(entries), len(urls))
    return entries


async def _load_pool() -> list[str]:
    """Bulk tier: big raw public lists (sampled)."""
    return await _load_list(
        getattr(config, "PROXY_REMOTE_SOURCES", []) or [], POOL_CACHE, "Proxy pool"
    )


async def _load_priority_pool() -> list[str]:
    """Priority tier: small health-checked lists (tested in full first)."""
    urls = getattr(config, "PROXY_PRIORITY_SOURCES", []) or []
    if not urls:
        return []
    return await _load_list(urls, PRIORITY_CACHE, "Priority proxy pool")


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

    # 2. Priority tier: health-checked lists — small, so test in FULL with a
    #    higher concurrency before touching the big raw pools.
    priority = await _load_priority_pool()
    if priority:
        logger.info("Testing %d priority (health-checked) proxies in full...", len(priority))
        working = await _find_working(priority, len(priority), timeout, max(concurrency, 50))
        if working:
            try:
                await set_setting("last_pool_proxy", working)
            except Exception:
                logger.exception("Failed to cache working pool proxy")
            logger.info("Found working proxy in priority pool: %s", working)
            return working
        logger.info("Priority pool exhausted (%d tested); falling back to bulk pool", len(priority))

    # 3. Bulk tier: random sample from the big raw lists.
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


# --------------------------------------------------------------- mtproto pool
# Pyrogram/Telethon need to reach a Telegram MTProto DC (e.g. 149.154.167.51),
# not api.telegram.org. Telegram filters public IPs much more aggressively
# on the MTProto edge, so a SOCKS5 that works for aiogram may not work for
# Pyrogram. This tester checks TCP-connect to a DC through SOCKS5.

MTPROTO_DC = ("149.154.167.51", 443)


def _test_socks5_to_mtproto_dc(host: str, port: int, timeout: float) -> bool:
    """Open a TCP connection to MTPROTO_DC through a SOCKS5 proxy. We don't
    need to speak MTProto — TCP-connect success is enough: it means the proxy
    IP is not blocked by Telegram's MTProto edge filters."""
    import socks as socks_mod
    s = socks_mod.socksocket()
    s.set_proxy(socks_mod.SOCKS5, host, port)
    s.settimeout(timeout)
    try:
        s.connect(MTPROTO_DC)
        s.close()
        return True
    except Exception:
        return False


async def _find_mtproto_working(entries: list[str], sample_size: int,
                                timeout: float, concurrency: int) -> str | None:
    """Like _find_working but uses _test_socks5_to_mtproto_dc."""
    sample = random.sample(entries, min(sample_size, len(entries)))
    sem = asyncio.Semaphore(concurrency)
    loop = asyncio.get_event_loop()
    working: asyncio.Future = asyncio.get_event_loop().create_future()
    pool = _get_thread_pool()

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
            ok = await loop.run_in_executor(
                pool, _test_socks5_to_mtproto_dc, host, port, timeout
            )
            if ok and not working.done():
                working.set_result(f"socks5://{host}:{port}")

    tasks = [asyncio.create_task(check(e)) for e in sample]
    try:
        await asyncio.wait_for(
            asyncio.shield(working),
            timeout=timeout * (len(sample) // concurrency + 1) + 5,
        )
    except asyncio.TimeoutError:
        pass
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
    return working.result() if working.done() and not working.cancelled() else None


_thread_pool = None


def _get_thread_pool():
    """Lazy singleton ThreadPoolExecutor for blocking socks operations."""
    global _thread_pool
    if _thread_pool is None:
        from concurrent.futures import ThreadPoolExecutor
        _thread_pool = ThreadPoolExecutor(max_workers=64, thread_name_prefix="mtproto-test")
    return _thread_pool


async def find_working_mtproto_proxy(configured: list[str] | None = None) -> str | None:
    """Find a SOCKS5 proxy that can carry MTProto traffic (TCP-reach the DC),
    not just HTTP. Used by channel_sources as a last-resort fallback when all
    TG_PROXIES fail for Pyrogram.

    Cached separately from HTTP-pool in settings.last_mtproto_proxy.
    Returns 'socks5://host:port' URL, or None if nothing works."""
    configured = configured or []
    sample_size = getattr(config, "PROXY_POOL_SAMPLE", 100)
    timeout = getattr(config, "PROXY_POOL_TIMEOUT", 4.0)
    concurrency = getattr(config, "PROXY_POOL_CONCURRENCY", 20)

    # 1. Cached MTProto-working proxy
    cached = None
    try:
        cached = await get_setting("last_mtproto_proxy")
    except Exception:
        logger.exception("Failed to read cached mtproto proxy")
    if cached and cached not in configured:
        host_port = cached.replace("socks5://", "")
        host, _, port = host_port.partition(":")
        try:
            port_i = int(port)
        except ValueError:
            port_i = 0
        if host and port_i:
            loop = asyncio.get_event_loop()
            ok = await loop.run_in_executor(
                _get_thread_pool(), _test_socks5_to_mtproto_dc, host, port_i, timeout
            )
            if ok:
                logger.info("Cached MTProto proxy still works: %s", cached)
                return cached
            logger.info("Cached MTProto proxy no longer works: %s", cached)

    # 2. Priority tier: health-checked lists — test in full with high
    #    concurrency before sampling the big raw pools.
    priority = await _load_priority_pool()
    if priority:
        logger.info("Testing %d priority (health-checked) proxies for MTProto DC...", len(priority))
        working = await _find_mtproto_working(priority, len(priority), timeout, max(concurrency, 50))
        if working:
            try:
                await set_setting("last_mtproto_proxy", working)
            except Exception:
                logger.exception("Failed to cache working MTProto proxy")
            logger.info("Found MTProto-working proxy in priority pool: %s", working)
            return working
        logger.info("Priority pool exhausted for MTProto (%d tested); falling back to bulk pool", len(priority))

    # 3. Fresh sample from the remote bulk pool
    entries = await _load_pool()
    if not entries:
        logger.warning("Proxy pool empty; MTProto fallback has no candidates")
        return None

    logger.info("Testing %d random proxies for MTProto DC reachability (of %d)...",
                sample_size, len(entries))
    working = await _find_mtproto_working(entries, sample_size, timeout, concurrency)
    if working:
        try:
            await set_setting("last_mtproto_proxy", working)
        except Exception:
            logger.exception("Failed to cache working MTProto proxy")
        logger.info("Found MTProto-working pool proxy: %s", working)
    else:
        logger.warning("No MTProto-working proxy found in sample of %d", sample_size)
    return working
