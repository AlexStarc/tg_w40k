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
import json
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
    winners = await _find_many(entries, sample_size, 1, timeout, concurrency, "http")
    return winners[0] if winners else None


async def _find_many(entries: list[str], sample_size: int, count: int,
                     timeout: float, concurrency: int, kind: str) -> list[str]:
    """Test up to `sample_size` entries, collect up to `count` working
    'socks5://host:port' URLs. kind: 'http' (Bot-API reachability) or
    'mtproto' (MTProto-DC TCP reachability). Extra tasks are cancelled once
    enough winners are found."""
    sample = random.sample(entries, min(sample_size, len(entries)))
    sem = asyncio.Semaphore(concurrency)
    loop = asyncio.get_event_loop()
    winners: list[str] = []
    lock = asyncio.Lock()
    done_ev = asyncio.Event()

    if kind == "mtproto":
        pool = _get_thread_pool()

        async def check(entry: str) -> None:
            host, port = _split(entry)
            if not host:
                return
            async with sem:
                if done_ev.is_set():
                    return
                ok = await loop.run_in_executor(
                    pool, _test_socks5_to_mtproto_dc, host, port, timeout
                )
                await _record(ok, host, port)
    else:
        async def check(entry: str) -> None:
            host, port = _split(entry)
            if not host:
                return
            async with sem:
                if done_ev.is_set():
                    return
                ok = await _test_socks5(host, port, timeout)
                await _record(ok, host, port)

    async def _record(ok: bool, host: str, port: int) -> None:
        nonlocal winners
        if not ok:
            return
        async with lock:
            if len(winners) >= count:
                return
            winners.append(f"socks5://{host}:{port}")
            if len(winners) >= count:
                done_ev.set()

    tasks = [asyncio.create_task(check(e)) for e in sample]
    try:
        await asyncio.wait_for(
            done_ev.wait(),
            timeout=timeout * (len(sample) // concurrency + 1) + 5,
        )
    except asyncio.TimeoutError:
        pass
    finally:
        done_ev.set()
        for t in tasks:
            if not t.done():
                t.cancel()
    return winners


def _split(entry: str) -> tuple[str, int]:
    parts = entry.split(":")
    if len(parts) != 2:
        return "", 0
    try:
        return parts[0].strip(), int(parts[1])
    except ValueError:
        return "", 0


async def find_working_proxy(configured: list[str] | None = None) -> str | None:
    """Compatibility wrapper — a single working socks5:// URL (Bot-API HTTP)."""
    winners = await find_working_proxies(1, configured)
    return winners[0] if winners else None


async def find_working_proxies(count: int = 5,
                               configured: list[str] | None = None) -> list[str]:
    """Find up to `count` SOCKS5 proxies that reach api.telegram.org (HTTP,
    what aiogram needs). Winners are cached as a JSON list in
    settings.auto_proxies so startup/health-check reuse them without a new
    scan. `configured` entries are excluded from results.

    Order: cached still-alive winners → tier-1 health-checked list (full) →
    tier-2 bulk pools (sample)."""
    configured = configured or []
    timeout = getattr(config, "PROXY_POOL_TIMEOUT", 4.0)

    # 0. Cached winners that still pass a quick liveness check
    cached = await _load_cached_list("auto_proxies")
    alive = [p for p in cached if p not in configured and await _quick_alive(p, timeout)]
    if alive:
        logger.info("Cached auto-proxies still alive: %d of %d", len(alive), len(cached))
        if len(alive) >= count:
            return alive[:count]

    # 1. Priority tier (health-checked) → 2. Bulk tier
    for label, loader, full in (
        ("priority", _load_priority_pool, True),
        ("bulk", _load_pool, False),
    ):
        entries = await loader()
        if not entries:
            continue
        sample_size = len(entries) if full else getattr(config, "PROXY_POOL_SAMPLE", 100)
        concurrency = max(getattr(config, "PROXY_POOL_CONCURRENCY", 20), 50) if full \
            else getattr(config, "PROXY_POOL_CONCURRENCY", 20)
        need = count - len(alive)
        logger.info("Scanning %s pool (%d entries, need %d more)...", label, len(entries), need)
        found = await _find_many(entries, sample_size, need, timeout, concurrency, "http")
        alive.extend(found)
        if len(alive) >= count:
            break

    if alive:
        await _save_cached_list("auto_proxies", alive)
        logger.info("Auto-proxies (Bot-API): %s", alive)
    else:
        logger.warning("No working proxies found in any pool")
    return alive


async def _quick_alive(proxy_url: str, timeout: float) -> bool:
    host, port = _split(proxy_url.replace("socks5://", ""))
    if not host:
        return False
    return await _test_socks5(host, port, timeout)


async def _load_cached_list(key: str) -> list[str]:
    try:
        raw = await get_setting(key)
        val = json.loads(raw) if raw else []
        return [p for p in val if isinstance(p, str) and p.startswith("socks5://")]
    except Exception:
        logger.exception("Failed to read cached list %s", key)
        return []


async def _save_cached_list(key: str, proxies: list[str]) -> None:
    try:
        await set_setting(key, json.dumps(proxies))
    except Exception:
        logger.exception("Failed to cache list %s", key)


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
    """Compatibility wrapper — first MTProto-DC-reachable proxy."""
    winners = await _find_many(entries, sample_size, 1, timeout, concurrency, "mtproto")
    return winners[0] if winners else None


_thread_pool = None


def _get_thread_pool():
    """Lazy singleton ThreadPoolExecutor for blocking socks operations."""
    global _thread_pool
    if _thread_pool is None:
        from concurrent.futures import ThreadPoolExecutor
        _thread_pool = ThreadPoolExecutor(max_workers=64, thread_name_prefix="mtproto-test")
    return _thread_pool


async def find_working_mtproto_proxy(configured: list[str] | None = None) -> str | None:
    """Compatibility wrapper — a single MTProto-DC-reachable proxy."""
    winners = await find_working_mtproto_proxies(1, configured)
    return winners[0] if winners else None


async def find_working_mtproto_proxies(count: int = 5,
                                       configured: list[str] | None = None) -> list[str]:
    """Find up to `count` SOCKS5 proxies that can carry MTProto traffic
    (TCP-reach the DC) — what Pyrogram needs. Cached as a JSON list in
    settings.auto_mtproto_proxies. Same tiering as find_working_proxies."""
    configured = configured or []
    timeout = getattr(config, "PROXY_POOL_TIMEOUT", 4.0)
    loop = asyncio.get_event_loop()

    # 0. Cached winners that still pass a quick MTProto liveness check
    cached = await _load_cached_list("auto_mtproto_proxies")
    alive: list[str] = []
    for p in cached:
        if p in configured:
            continue
        host, port = _split(p.replace("socks5://", ""))
        if host and await loop.run_in_executor(
            _get_thread_pool(), _test_socks5_to_mtproto_dc, host, port, timeout
        ):
            alive.append(p)
    if alive:
        logger.info("Cached MTProto auto-proxies still alive: %d of %d", len(alive), len(cached))
        if len(alive) >= count:
            return alive[:count]

    # 1. Priority tier → 2. Bulk tier
    for label, loader, full in (
        ("priority", _load_priority_pool, True),
        ("bulk", _load_pool, False),
    ):
        entries = await loader()
        if not entries:
            continue
        sample_size = len(entries) if full else getattr(config, "PROXY_POOL_SAMPLE", 100)
        concurrency = max(getattr(config, "PROXY_POOL_CONCURRENCY", 20), 50) if full \
            else getattr(config, "PROXY_POOL_CONCURRENCY", 20)
        need = count - len(alive)
        logger.info("Scanning %s pool for MTProto DC (%d entries, need %d more)...",
                    label, len(entries), need)
        found = await _find_many(entries, sample_size, need, timeout, concurrency, "mtproto")
        alive.extend(found)
        if len(alive) >= count:
            break

    if alive:
        await _save_cached_list("auto_mtproto_proxies", alive)
        logger.info("Auto-proxies (MTProto): %s", alive)
    else:
        logger.warning("No MTProto-working proxies found in any pool")
    return alive
