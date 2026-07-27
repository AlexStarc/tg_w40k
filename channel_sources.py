"""Telethon wrapper for reading posts from meme source channels.

A user-mode Telegram session (NOT the bot) is used so the bot can read any
public channel the user is subscribed to. Run `auth_telethon.py` once to
create the .session file; the bot uses it read-only afterwards.

The module is optional: if TG_API_ID / TG_API_HASH are not set, is_configured()
returns False and callers fall back to the existing GLM-only seed flow.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from telethon import TelegramClient
from telethon.errors import FloodWaitError

import config

logger = logging.getLogger(__name__)

_client: Optional[TelegramClient] = None
_lock = asyncio.Lock()


@dataclass
class Post:
    channel: str
    tg_id: int
    date: datetime
    text: Optional[str]
    has_media: bool


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


def is_configured() -> bool:
    return bool(config.TG_API_ID and config.TG_API_HASH)


async def get_client() -> TelegramClient:
    """Lazy singleton. Raises RuntimeError if not configured or if the session
    is not authorized (run `python auth_telethon.py` to authorize)."""
    global _client
    if _client and _client.is_connected():
        return _client
    if not is_configured():
        raise RuntimeError("Telethon not configured: set TG_API_ID and TG_API_HASH")
    async with _lock:
        if _client and _client.is_connected():
            return _client
        client = TelegramClient(
            config.TG_SESSION,
            int(config.TG_API_ID),  # type: ignore[arg-type]
            config.TG_API_HASH,  # type: ignore[arg-type]
        )
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError(
                f"Telethon session '{config.TG_SESSION}' is not authorized. "
                "Run `python auth_telethon.py` once to log in."
            )
        logger.info("Telethon client ready (session=%s)", config.TG_SESSION)
        _client = client
        return _client


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
        out = []
        async for msg in client.iter_messages(target, **kwargs):
            out.append(msg)
        return out
    except FloodWaitError as e:
        wait = min(int(e.seconds) + 1, 60)
        logger.warning("FloodWait %ds on %s; backing off %ds", e.seconds, target, wait)
        await asyncio.sleep(wait)
        return []
    except Exception:
        logger.exception("iter_messages failed for %s", target)
        return []


def _to_post(channel: str, msg) -> Optional[Post]:
    text = getattr(msg, "text", None) or getattr(msg, "message", None)
    if not text:
        return None
    return Post(
        channel=channel,
        tg_id=msg.id,
        date=getattr(msg, "date", None),
        text=text,
        has_media=bool(getattr(msg, "media", None)),
    )


async def fetch_recent(channel: str, limit: int = 50) -> list[Post]:
    """Fetch up to `limit` most recent text-bearing posts from a channel."""
    target = _resolve(channel)
    msgs = await _iter_posts(target, limit, None)
    posts = []
    for m in msgs:
        p = _to_post(channel, m)
        if p:
            posts.append(p)
    return posts


async def fetch_since(channel: str, last_tg_id: Optional[int], limit: int = 100) -> list[Post]:
    """Fetch text-bearing posts with id > last_tg_id (newer). If last_tg_id is
    None, behaves like fetch_recent (initial backfill)."""
    target = _resolve(channel)
    msgs = await _iter_posts(target, limit, last_tg_id)
    posts = []
    for m in msgs:
        p = _to_post(channel, m)
        if p:
            posts.append(p)
    return posts
