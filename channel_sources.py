"""Telethon wrapper for reading posts from meme source channels.

A user-mode Telegram session (NOT the bot) is used so the bot can read any
public channel the user is subscribed to. Run `auth_telethon.py` once to
create the .session file; the bot uses it read-only afterwards.

The module degrades gracefully when telethon is not installed: is_configured()
returns False and callers fall back to the existing GLM-only seed flow without
crashing the bot on import.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import config

logger = logging.getLogger(__name__)

_client = None
_lock = asyncio.Lock()


@dataclass
class Post:
    channel: str
    tg_id: int
    date: datetime
    text: Optional[str]
    has_media: bool
    image_bytes: Optional[bytes] = None  # populated only when fetched with download_images=True


def _has_telethon() -> bool:
    try:
        importlib.import_module("telethon")
        return True
    except ImportError:
        return False


def is_configured() -> bool:
    """True only if BOTH env vars AND telethon are available. When this returns
    False, callers fall back to the GLM-only seed flow without raising."""
    return bool(config.TG_API_ID and config.TG_API_HASH) and _has_telethon()


def _resolve(channel: str) -> str:
    """Normalize '@username' / 'https://t.me/x[/123]' / raw username to a form
    Telethon understands. Returns '@username' or the raw string if it looks
    like a numeric/invite id."""
    c = channel.strip()
    if not c:
        return c
    if c.startswith("@"):
        return c
    lower = c.lower()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if lower.startswith(prefix):
            tail = c[len(prefix):].split("/", 1)[0]
            return "@" + tail if tail else c
    if "/" not in c and not c.lstrip("-").isdigit():
        return "@" + c
    return c


async def get_client():
    """Lazy singleton. Tries MTProto proxies (if configured) first, then all
    SOCKS/HTTP proxies from TG_PROXIES, then samples the remote SOCKS5 pool,
    then a direct connection — first working wins. Raises RuntimeError if not
    configured, telethon is missing, or every connection attempt fails
    (including an unauthorized session)."""
    global _client
    if _client and _client.is_connected():
        return _client
    if not (config.TG_API_ID and config.TG_API_HASH):
        raise RuntimeError("Telethon not configured: set TG_API_ID and TG_API_HASH")
    try:
        from telethon import TelegramClient
    except ImportError as e:
        raise RuntimeError(
            "telethon is not installed. Run `pip install -r requirements.txt` "
            "(or `pip install telethon[socks]`) to enable channel harvesting."
        ) from e

    async def _try(kind: str, proxy) -> bool:
        """Attempt to connect + authorize. On success sets _client and returns True."""
        nonlocal last_err
        nonlocal _mtproto_unsupported_logged
        try:
            kwargs: dict = {
                "session": config.TG_SESSION,
                "api_id": int(config.TG_API_ID),  # type: ignore[arg-type]
                "api_hash": config.TG_API_HASH,  # type: ignore[arg-type]
                "proxy": proxy,
            }
            # Telethon stable (1.x) doesn't actually route MTProto proxies
            # through (host, port, secret) — PySocks intercepts and fails with
            # 'Unknown proxy protocol type: <host>'. The flag below is kept only
            # so a future Telethon that does support MTProto picks it up.
            if kind.startswith("mtproto"):
                try:
                    from telethon.network.connection import ConnectionTcpAbridged
                    kwargs["connection"] = ConnectionTcpAbridged
                except ImportError:
                    pass
            client = TelegramClient(**kwargs)
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                raise RuntimeError(
                    f"session '{config.TG_SESSION}' is not authorized. "
                    "Run `python auth_telethon.py` once to log in."
                )
            logger.info("Telethon connected via %s: %r", kind, proxy)
            _client = client
            return True
        except Exception as e:  # noqa: BLE001
            last_err = e
            msg = str(e)
            # Telethon stable can't handle MTProto tuple — skip silently after the
            # first such error so the log doesn't get spammed by all 3 entries.
            if kind.startswith("mtproto") and "Unknown proxy protocol type" in msg:
                if not _mtproto_unsupported_logged:
                    logger.warning(
                        "Telethon stable does not support MTProto proxies natively "
                        "(got 'Unknown proxy protocol type'). Skipping all MTProto candidates. "
                        "Use a local mtg bridge (SOCKS5 on localhost) and add it to TG_PROXIES instead."
                    )
                    _mtproto_unsupported_logged = True
                return False
            logger.warning("Telethon %s attempt failed (%s): %s", kind, proxy, e)
            return False

    async with _lock:
        if _client and _client.is_connected():
            return _client

        # One-shot flag so we log the Telethon-stable MTProto limitation only
        # once per get_client() call (the inner _try() may hit it 3 times).
        _mtproto_unsupported_logged = False

        # Build ordered candidate list:
        #   1. MTProto proxies (Telegram-native)
        #   2. TG_TELETHON_PROXY (explicit override) if set
        #   3. All TG_PROXIES (SOCKS/HTTP)
        #   4. Direct
        candidates: list[tuple[str, object]] = []
        for mt in config.TG_MTPROTO_PROXIES:
            candidates.append(("mtproto", mt))
        seen_urls: set[str] = set()
        explicit = [config.TG_TELETHON_PROXY] if config.TG_TELETHON_PROXY else []
        for url in explicit + list(config.TG_PROXIES):
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            tup = config.telethon_proxy_tuple(url)
            if tup:
                candidates.append((f"socks({url})", tup))
        candidates.append(("direct", None))

        last_err: Exception | None = None
        for kind, proxy in candidates:
            if await _try(kind, proxy):
                return _client

        # Last-resort fallback: sample the remote SOCKS5 pool (same source as
        # aiogram uses). Find a proxy that reaches api.telegram.org, then try
        # it through Telethon. Up to POXY_POOL_SAMPLE candidates are tested
        # for HTTP first by proxy_pool.find_working_proxy; only the winner is
        # tried for Telethon here.
        try:
            import proxy_pool
            pool_url = await proxy_pool.find_working_proxy(list(seen_urls))
        except Exception:
            logger.exception("proxy_pool lookup crashed during Telethon fallback")
            pool_url = None
        if pool_url:
            tup = config.telethon_proxy_tuple(pool_url)
            if tup and await _try(f"pool({pool_url})", tup):
                return _client

        raise RuntimeError(
            f"All Telethon connection attempts failed ({len(candidates) + 1} tried). "
            f"Last error: {last_err}"
        )


async def close_client() -> None:
    global _client
    if _client and _client.is_connected():
        try:
            await _client.disconnect()
        except Exception:
            logger.exception("telethon disconnect failed")
    _client = None


async def _iter_posts(target: str, limit: int, min_id: Optional[int]) -> list:
    """Common iterator with FloodWait handling. Returns raw telethon messages."""
    client = await get_client()
    kwargs: dict = {"limit": limit}
    if min_id is not None:
        kwargs["min_id"] = min_id
    try:
        from telethon.errors import FloodWaitError
    except ImportError:
        FloodWaitError = ()  # type: ignore[assignment,misc]
    try:
        out = []
        async for msg in client.iter_messages(target, **kwargs):
            out.append(msg)
        return out
    except FloodWaitError as e:  # type: ignore[misc]
        wait = min(int(e.seconds) + 1, 60)
        logger.warning("FloodWait %ds on %s; backing off %ds", e.seconds, target, wait)
        await asyncio.sleep(wait)
        return []
    except Exception:
        logger.exception("iter_messages failed for %s", target)
        return []


def _to_post(channel: str, msg, *, require_text: bool = True) -> Optional[Post]:
    """Build a Post from a telethon Message. By default skips media-only posts
    (no caption). Pass require_text=False to keep them — caller can then fetch
    their image bytes via download_post_image()."""
    text = getattr(msg, "text", None) or getattr(msg, "message", None)
    has_media = bool(getattr(msg, "media", None))
    if require_text and not text:
        return None
    if not text and not has_media:
        return None
    return Post(
        channel=channel,
        tg_id=msg.id,
        date=getattr(msg, "date", None),
        text=text,
        has_media=has_media,
    )


async def download_post_image(channel: str, tg_id: int) -> Optional[bytes]:
    """Re-fetch a single message by tg_id and download its photo as bytes.
    Returns None if the message has no downloadable media."""
    try:
        from telethon.tl.custom import Message as TgMessage  # noqa: F401
    except ImportError:
        return None
    client = await get_client()
    target = _resolve(channel)
    try:
        msg = await client.get_messages(target, ids=tg_id)
        if not msg:
            return None
        if isinstance(msg, list):
            msg = msg[0] if msg else None
            if not msg:
                return None
        if not getattr(msg, "media", None):
            return None
        data = await client.download_media(msg, file=bytes)
        return data if isinstance(data, (bytes, bytearray)) else None
    except Exception:
        logger.exception("download_post_image failed for %s/%d", channel, tg_id)
        return None


async def fetch_recent(channel: str, limit: int = 50) -> list[Post]:
    """Fetch up to `limit` most recent text-bearing posts from a channel."""
    target = _resolve(channel)
    msgs = await _iter_posts(target, limit, None)
    return [p for p in (_to_post(channel, m) for m in msgs) if p]


async def fetch_since(channel: str, last_tg_id: Optional[int], limit: int = 100,
                      include_media_only: bool = False) -> list[Post]:
    """Fetch text-bearing posts with id > last_tg_id (newer). If last_tg_id is
    None, behaves like fetch_recent (initial backfill).

    Pass include_media=True to also keep posts that have only an image (no
    caption) — caller can then call download_post_image() to get bytes and
    run them through a vision model."""
    target = _resolve(channel)
    msgs = await _iter_posts(target, limit, last_tg_id)
    return [
        p for p in (_to_post(channel, m, require_text=not include_media_only) for m in msgs)
        if p
    ]
