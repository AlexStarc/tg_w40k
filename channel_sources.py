"""Pyrogram wrapper for reading posts from meme source channels.

A user-mode Telegram session (NOT the bot) is used so the bot can read any
public channel the user is subscribed to. Run `python auth_pyrogram.py` once
to create the .session file; the bot uses it read-only afterwards.

Pyrogram supports MTProto proxies natively (no extra bridge required), which
is the main reason we use it instead of Telethon.

The module degrades gracefully when Pyrogram is not installed: is_configured()
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
from urllib.parse import urlparse

import config

logger = logging.getLogger(__name__)

_client = None  # type: Optional[object]
_lock = asyncio.Lock()


@dataclass
class Post:
    channel: str
    tg_id: int
    date: datetime
    text: Optional[str]
    has_media: bool
    image_bytes: Optional[bytes] = None  # populated only when fetched with download_images=True


def _has_pyrogram() -> bool:
    try:
        importlib.import_module("pyrogram")
        return True
    except ImportError:
        return False


def is_configured() -> bool:
    """True only if BOTH env vars AND Pyrogram are available. When this returns
    False, callers fall back to the GLM-only seed flow without raising."""
    return bool(config.TG_API_ID and config.TG_API_HASH) and _has_pyrogram()


def _resolve(channel: str) -> str:
    """Normalize '@username' / 'https://t.me/x[/123]' / raw username to a form
    Pyrogram understands. Returns '@username' or the raw string if it looks
    like a numeric id."""
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


def _proxy_candidates() -> list[tuple[str, dict]]:
    """Build the ordered proxy candidate list (kind, pyrogram-proxy-dict).

    Pyrogram / pyrofork support only SOCKS4/SOCKS5/HTTP via PySocks — MTProto
    is NOT supported through the `proxy=` parameter (PySocks has no MTPROTO
    type). To use an MTProto upstream, run a local mtg bridge and add its
    SOCKS5 endpoint (socks5://127.0.0.1:<port>) to TG_PROXIES.

    Order: all TG_PROXIES (SOCKS/HTTP) → explicit TG_TELETHON_PROXY."""
    out: list[tuple[str, dict]] = []
    seen_urls: set[str] = set()
    explicit = [config.TG_TELETHON_PROXY] if config.TG_TELETHON_PROXY else []
    for url in explicit + list(config.TG_PROXIES):
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        try:
            parsed = urlparse(url)
        except Exception:
            continue
        scheme = (parsed.scheme or "").lower()
        host = parsed.hostname
        port = parsed.port
        if not host or not port:
            continue
        if scheme in ("socks5", "socks5h"):
            out.append((f"socks5({url})", {"scheme": "socks5", "hostname": host, "port": port}))
        elif scheme == "socks4":
            out.append((f"socks4({url})", {"scheme": "socks4", "hostname": host, "port": port}))
        elif scheme in ("http", "https"):
            out.append((f"http({url})", {"scheme": "http", "hostname": host, "port": port}))
    return out


async def get_client():
    """Lazy singleton. Tries each proxy candidate in order, then a direct
    connection — first working wins. Raises RuntimeError if not configured,
    Pyrogram is missing, or every connection attempt fails (including an
    unauthorized session)."""
    global _client
    if _client is not None:
        return _client
    if not (config.TG_API_ID and config.TG_API_HASH):
        raise RuntimeError("Pyrogram not configured: set TG_API_ID and TG_API_HASH")
    try:
        from pyrogram import Client
    except ImportError as e:
        raise RuntimeError(
            "pyrogram is not installed. Run `pip install -r requirements.txt` "
            "(or `pip install pyrogram tgcrypto`) to enable channel harvesting."
        ) from e

    async with _lock:
        if _client is not None:
            return _client

        async def _try(kind: str, proxy: dict | None) -> bool:
            nonlocal last_err
            global _client
            try:
                app = Client(
                    config.TG_SESSION,
                    api_id=int(config.TG_API_ID),  # type: ignore[arg-type]
                    api_hash=config.TG_API_HASH,  # type: ignore[arg-type]
                    proxy=proxy,
                    no_updates=True,
                    workdir=".",
                )
                await app.start()
                me = await app.get_me()
                logger.info("Pyrogram connected via %s as @%s", kind, me.username or "(no username)")
                _client = app
                return True
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning("Pyrogram %s attempt failed: %s", kind, e)
                # Pyrogram leaves a half-open client on failure — make sure it's stopped
                try:
                    await app.stop()  # type: ignore[name-defined]
                except Exception:
                    pass
                return False

        last_err: Exception | None = None
        for kind, proxy in _proxy_candidates():
            if await _try(kind, proxy):
                return _client

        # Direct (no proxy) as the next fallback.
        if await _try("direct", None):
            return _client

        # Last-resort: sample the remote SOCKS5 pool and find a proxy that can
        # actually reach the Telegram MTProto DC (not just api.telegram.org).
        # This is the same logic as proxy_pool.find_working_proxy but tested
        # against 149.154.167.51:443 — what Pyrogram really needs.
        try:
            import proxy_pool
            configured_urls = list(config.TG_PROXIES) + (
                [config.TG_TELETHON_PROXY] if config.TG_TELETHON_PROXY else []
            )
            mtproto_url = await proxy_pool.find_working_mtproto_proxy(configured_urls)
        except Exception:
            logger.exception("MTProto pool lookup crashed during Pyrogram fallback")
            mtproto_url = None
        if mtproto_url:
            try:
                parsed = urlparse(mtproto_url)
                tup = {
                    "scheme": "socks5",
                    "hostname": parsed.hostname,
                    "port": parsed.port,
                }
            except Exception:
                tup = None
            if tup and await _try(f"mtproto-pool({mtproto_url})", tup):
                return _client

        raise RuntimeError(
            f"All Pyrogram connection attempts failed. Last error: {last_err}"
        )


async def close_client() -> None:
    global _client
    if _client is not None:
        try:
            await _client.stop()  # type: ignore[attr-defined]
        except Exception:
            logger.exception("pyrogram stop failed")
    _client = None


def _to_post(channel: str, msg, *, require_text: bool = True) -> Optional[Post]:
    """Build a Post from a Pyrogram Message. By default skips media-only posts
    (no caption). Pass require_text=False to keep them — caller can then fetch
    their image bytes via download_post_image()."""
    text = getattr(msg, "text", None) or getattr(msg, "caption", None)
    has_media = bool(getattr(msg, "media", None) or getattr(msg, "photo", None)
                     or getattr(msg, "document", None))
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
    client = await get_client()
    target = _resolve(channel)
    try:
        msg = await client.get_messages(target, message_ids=tg_id)  # type: ignore[attr-defined]
        if not msg or not (getattr(msg, "photo", None) or getattr(msg, "document", None)):
            return None
        buf = await client.download_media(msg, in_memory=True)  # type: ignore[attr-defined]
        if buf is None:
            return None
        # Pyrogram returns BytesIO; read it as bytes
        data = buf.getvalue() if hasattr(buf, "getvalue") else bytes(buf)
        return data if data else None
    except Exception:
        logger.exception("download_post_image failed for %s/%d", channel, tg_id)
        return None


async def _fetch(client, target: str, limit: int,
                 last_tg_id: Optional[int], require_text: bool) -> list:
    """Common fetch loop with FloodWait handling. Returns the raw Pyrogram
    Message list (newest first). Stops early when we cross last_tg_id."""
    out: list = []
    try:
        from pyrogram.errors import FloodWait
    except ImportError:
        FloodWait = ()  # type: ignore[assignment,misc]
    offset = 0
    try:
        while len(out) < limit:
            batch: list = []
            async for msg in client.get_chat_history(target, limit=50, offset=offset):  # type: ignore[attr-defined]
                batch.append(msg)
            if not batch:
                break
            for msg in batch:
                if last_tg_id is not None and msg.id <= last_tg_id:
                    return out  # reached already-seen messages
                out.append(msg)
                if len(out) >= limit:
                    break
            offset += len(batch)
            if len(batch) < 50:
                break  # end of channel history
        return out
    except FloodWait as e:  # type: ignore[misc]
        wait = min(int(getattr(e, "value", 30)) + 1, 60)
        logger.warning("FloodWait %ss on %s; backing off", getattr(e, "value", 30), target)
        await asyncio.sleep(wait)
        return out
    except Exception:
        logger.exception("get_chat_history failed for %s", target)
        return out


async def fetch_recent(channel: str, limit: int = 50) -> list[Post]:
    """Fetch up to `limit` most recent text-bearing posts from a channel."""
    target = _resolve(channel)
    client = await get_client()
    msgs = await _fetch(client, target, limit, None, require_text=True)
    return [p for p in (_to_post(channel, m) for m in msgs) if p]


async def fetch_since(channel: str, last_tg_id: Optional[int], limit: int = 100,
                      include_media_only: bool = False) -> list[Post]:
    """Fetch posts newer than last_tg_id. If last_tg_id is None, behaves like
    fetch_recent (initial backfill).

    Pass include_media_only=True to also keep posts that have only an image
    (no caption) — caller can then call download_post_image() to get bytes
    and run them through a vision model."""
    target = _resolve(channel)
    client = await get_client()
    msgs = await _fetch(client, target, limit, last_tg_id,
                        require_text=not include_media_only)
    return [
        p for p in (_to_post(channel, m, require_text=not include_media_only) for m in msgs)
        if p
    ]
