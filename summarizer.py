import json
import logging
import re
import base64
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

_MSK = ZoneInfo("Europe/Moscow")

_ROMAN_MAP = {
    "I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7,
    "VIII": 8, "IX": 9, "X": 10, "XI": 11, "XII": 12, "XIII": 13,
    "XIV": 14, "XV": 15, "XVI": 16, "XVII": 17, "XVIII": 18, "XIX": 19,
    "XX": 20, "XXI": 21, "XXII": 22, "XXIII": 23, "XXIV": 24, "XXV": 25,
    "XXVI": 26, "XXVII": 27, "XXVIII": 28, "XXIX": 29, "XXX": 30,
    "XL": 40, "XLI": 41, "XLII": 42, "XLIII": 43, "XLIV": 44, "XLV": 45,
    "XLVI": 46, "XLVII": 47, "XLVIII": 48, "XLIX": 49, "L": 50,
    "LI": 51, "LII": 52, "LIII": 53, "LIV": 54, "LV": 55, "LVI": 56,
    "LVII": 57, "LVIII": 58, "LIX": 59, "LX": 60, "LXX": 70, "LXXX": 80,
    "XC": 90, "C": 100,
}


def _parse_last_fragment(prev_summaries: list[tuple]) -> int:
    if not prev_summaries:
        return 0
    for _, text in prev_summaries:
        match = re.search(r"Фрагмент\s+([IVXLCDM]+)", text, re.IGNORECASE)
        if match:
            roman = match.group(1).upper()
            if roman in _ROMAN_MAP:
                return _ROMAN_MAP[roman]
        match = re.search(r"Фрагмент\s+(\d+)", text)
        if match:
            return int(match.group(1))
    return 0

from config import (
    GLM_API_KEY,
    GLM_URL,
    PRIMARY_MODEL,
    FALLBACK_MODEL,
    MAX_TOKENS,
    MODEL_RESPONSE_TIMEOUT,
    TWO_PASS_ENABLED,
    TOKEN_LIMIT_INPUT,
    CHARS_PER_TOKEN,
    MESSAGES_PER_CHUNK,
    VISION_MODEL,
    VISION_MAX_TOKENS,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    GEMINI_URL,
    OPENAI_API_KEY,
    OPENAI_MODEL,
    OPENAI_URL,
)
from prompts import (
    WARHAMMER_SYSTEM,
    ANALYST_SYSTEM,
    EDITOR_SYSTEM,
    QUIET_DAY_SYSTEM,
    VISION_DESC_PROMPT,
)

logger = logging.getLogger(__name__)

_headers = {"Authorization": f"Bearer {GLM_API_KEY}", "Content-Type": "application/json"}


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=4, max=30),
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.HTTPStatusError)),
    reraise=True,
)
async def _call_glm(payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=MODEL_RESPONSE_TIMEOUT) as client:
        try:
            response = await client.post(GLM_URL, headers=_headers, json=payload)
            if response.status_code >= 400:
                # Log the response body on 4xx/5xx — z.ai usually returns a JSON
                # error like {"error":{"code":"...","message":"..."}} that explains
                # the cause (bad model name, malformed image, quota, etc).
                body = response.text
                logger.error(
                    "GLM HTTP %s on model=%s; body: %s",
                    response.status_code, payload.get("model"), body[:1000]
                )
            response.raise_for_status()
            logger.info("GLM response OK, model=%s", payload.get("model"))
            return response.json()
        except httpx.TimeoutException:
            logger.warning(
                "GLM timeout on model=%s, falling back to %s",
                payload.get("model"),
                FALLBACK_MODEL,
            )
            payload = {**payload, "model": FALLBACK_MODEL}
            response = await client.post(GLM_URL, headers=_headers, json=payload)
            response.raise_for_status()
            logger.info("Fallback GLM response OK, model=%s", FALLBACK_MODEL)
            return response.json()


async def describe_image(image_bytes: bytes, mime: str = "image/jpeg") -> str:
    """Vision-describe an image. Returns a short Russian description usable
    as a TG-channel caption.

    Backend priority (with graceful fallback):
      1. Gemini (if GEMINI_API_KEY) — best quality, but Google-accounts from
         RF blocked and 403 falls through to OCR.
      2. OpenAI (if OPENAI_API_KEY) — also blocked from RF (403).
      3. Tesseract OCR — LOCAL, always works if installed. For text-based
         memes (image + caption) this captures the joke that's the essence
         of the post. Default for users without vision-API access.
      4. GLM via VISION_MODEL — last resort (z.ai usually has no vision).

    Raises only on Tesseract failures (which are rare); other backends
    log + fall through."""
    if not image_bytes:
        return ""

    # 1. Gemini
    if GEMINI_API_KEY:
        try:
            text = await _describe_image_gemini(image_bytes, mime)
            if text:
                return text.strip()
            logger.warning("Gemini returned empty; falling back")
        except Exception:
            logger.warning("Gemini failed; falling back", exc_info=True)

    # 2. OpenAI
    if OPENAI_API_KEY:
        try:
            text = await _describe_image_openai(image_bytes, mime)
            if text:
                return text.strip()
            logger.warning("OpenAI returned empty; falling back")
        except Exception:
            logger.warning("OpenAI failed; falling back", exc_info=True)

    # 3. Tesseract (local OCR — no API key needed)
    text = _describe_image_tesseract(image_bytes)
    if text:
        return text

    # 4. GLM-4V (rarely available)
    return (await _describe_image_glm(image_bytes, mime)).strip()


def _describe_image_tesseract(image_bytes: bytes) -> str:
    """Extract text from image via Tesseract OCR. Returns the recognized
    text (Russian + English) found on the image. For text-based memes
    (image + caption) this captures the joke. Requires system tesseract
    binary plus rus+eng language data."""
    try:
        import io as _io
        import pytesseract
        from PIL import Image
        img = Image.open(_io.BytesIO(image_bytes))
        text = pytesseract.image_to_string(img, lang="rus+eng")
        text = (text or "").strip()
        logger.info("OCR: %d bytes image -> %d chars text", len(image_bytes), len(text))
        return text
    except Exception as e:
        logger.exception("Tesseract OCR failed: %s", e)
        return ""


async def _get_vision_http_client(timeout: int) -> "httpx.AsyncClient":
    """Build an httpx.AsyncClient for vision API calls. If a cached working
    SOCKS5 exists in DB (settings.last_pool_proxy or last_mtproto_proxy), use
    it — Google/OpenAI block RF IPs directly, so we need to route through the
    same proxy that Pyrogram already uses for Telegram."""
    cached_url = None
    try:
        # Lazy import to avoid pulling DB into GLM-only summarization paths.
        from database import get_setting
        for key in ("last_mtproto_proxy", "last_pool_proxy"):
            v = await get_setting(key)
            if v:
                cached_url = v
                break
    except Exception:
        logger.exception("Failed to read cached SOCKS5 for vision client")

    if cached_url:
        try:
            from httpx_socks import AsyncProxyTransport
            transport = AsyncProxyTransport.from_url(cached_url)
            client = httpx.AsyncClient(timeout=timeout, transport=transport)
            logger.debug("vision HTTP client via proxy %s", cached_url)
            return client
        except Exception:
            logger.exception("Failed to build SOCKS5 transport for vision; falling back to direct")
    return httpx.AsyncClient(timeout=timeout)


async def _describe_image_gemini(image_bytes: bytes, mime: str) -> str:
    """Describe an image via Google Gemini REST API (no SDK needed)."""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "contents": [{
            "parts": [
                {"text": VISION_DESC_PROMPT},
                {"inline_data": {"mime_type": mime, "data": b64}},
            ]
        }],
        "generationConfig": {
            "maxOutputTokens": VISION_MAX_TOKENS,
            "temperature": 0.4,
        },
    }
    url = f"{GEMINI_URL}/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    async with await _get_vision_http_client(MODEL_RESPONSE_TIMEOUT) as client:
        response = await client.post(url, json=payload)
        if response.status_code >= 400:
            logger.error(
                "Gemini HTTP %s on model=%s; body: %s",
                response.status_code, GEMINI_MODEL, response.text[:1000]
            )
        response.raise_for_status()
        data = response.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"] or ""
    except (KeyError, IndexError):
        logger.warning("Gemini response shape unexpected: %s", str(data)[:500])
        return ""


async def _describe_image_openai(image_bytes: bytes, mime: str) -> str:
    """Describe an image via OpenAI Chat Completions API (gpt-4o-mini by
    default). Works from any region; ~$0.15/1M input tokens."""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": VISION_DESC_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }
        ],
        "max_tokens": VISION_MAX_TOKENS,
        "temperature": 0.4,
    }
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    async with await _get_vision_http_client(MODEL_RESPONSE_TIMEOUT) as client:
        response = await client.post(OPENAI_URL, headers=headers, json=payload)
        if response.status_code >= 400:
            logger.error(
                "OpenAI HTTP %s on model=%s; body: %s",
                response.status_code, OPENAI_MODEL, response.text[:1000]
            )
        response.raise_for_status()
        data = response.json()
    try:
        return data["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError):
        logger.warning("OpenAI response shape unexpected: %s", str(data)[:500])
        return ""


async def _describe_image_glm(image_bytes: bytes, mime: str) -> str:
    """Describe an image via z.ai GLM-4V (rarely available — most tariffs
    don't include vision). Kept as fallback for accounts that do have it."""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "model": VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    },
                    {"type": "text", "text": VISION_DESC_PROMPT},
                ],
            }
        ],
        "max_tokens": VISION_MAX_TOKENS,
        "temperature": 0.4,
    }
    result = await _call_glm(payload)
    try:
        return result["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError):
        return ""


def _estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


def _ts_to_hhmm(ts: Optional[int]) -> str:
    if not ts:
        return "??:??"
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(_MSK)
        return dt.strftime("%H:%M")
    except (OSError, ValueError):
        return "??:??"


_TIME_BLOCKS = [
    (0, 6, "НОЧЬ"),
    (6, 12, "УТРО"),
    (12, 18, "ДЕНЬ"),
    (18, 24, "ВЕЧЕР"),
]


def _get_time_block(hour: int) -> str:
    for start, end, name in _TIME_BLOCKS:
        if start <= hour < end:
            return name
    return "НОЧЬ"


def preprocess_messages(messages: list[tuple]) -> str:
    """
    messages: list of (ts, username, text, reply_to_text)
    Returns formatted string with time blocks and reply context.
    """
    if not messages:
        return ""

    total = len(messages)
    participants = set()
    blocks: dict[str, list[str]] = {}
    current_block = None

    for ts, username, text, reply_to in messages:
        participants.add(username)
        hour = 0
        if ts:
            try:
                hour = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(_MSK).hour
            except (OSError, ValueError):
                pass

        block_name = _get_time_block(hour)
        if block_name != current_block:
            current_block = block_name
            if current_block not in blocks:
                blocks[current_block] = []

        time_str = _ts_to_hhmm(ts)
        line = f"[{time_str}] {username}: {text}"
        if reply_to:
            snippet = reply_to[:80] + "..." if len(reply_to) > 80 else reply_to
            line += f"  (в ответ на: «{snippet}»)"
        blocks[current_block].append(line)

    header = f"Всего сообщений: {total}. Участников: {len(participants)}.\n"
    parts = [header]

    for block_name, lines in blocks.items():
        parts.append(f"\n=== {block_name} ({len(lines)} сообщений) ===")
        parts.extend(lines)

    return "\n".join(parts)


def format_character_registry(characters: list[tuple]) -> str:
    if not characters:
        return "(пока нет известных персонажей)"
    return ", ".join(f"{name} = {title}" for name, title in characters)


def _build_writer_prompt(
    analysis: dict,
    formatted_messages: str,
    character_registry_str: str,
    prev_summaries_text: str,
    custom_writer_prompt: Optional[str] = None,
) -> list[dict]:
    system = custom_writer_prompt or WARHAMMER_SYSTEM

    analysis_text = json.dumps(analysis, ensure_ascii=False, indent=2)
    user_content = (
        f"{prev_summaries_text}"
        f"СПРАВОЧНИК ПЕРСОНАЖЕЙ:\n{character_registry_str}\n\n"
        f"ДАННЫЕ АНАЛИТИКА:\n{analysis_text}\n\n"
        f"ИСХОДНЫЕ СООБЩЕНИЯ:\n{formatted_messages}\n\n"
        f"Напиши летопись дня на основе данных аналитика."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]


async def _run_analyst(
    formatted_messages: str,
    character_registry_str: str,
    prev_summaries_text: str,
) -> dict:
    user_content = (
        f"{prev_summaries_text}"
        f"СПРАВОЧНИК ПЕРСОНАЖЕЙ:\n{character_registry_str}\n\n"
        f"ПЕРЕПИСКА:\n{formatted_messages}"
    )
    payload = {
        "model": PRIMARY_MODEL,
        "messages": [
            {"role": "system", "content": ANALYST_SYSTEM},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.3,
    }
    result = await _call_glm(payload)
    raw = result["choices"][0]["message"]["content"]

    try:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            first_newline = cleaned.find("\n")
            last_backtick = cleaned.rfind("```")
            if first_newline != -1 and last_backtick > first_newline:
                cleaned = cleaned[first_newline + 1 : last_backtick]
        return json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("Analyst returned non-JSON, using fallback structure")
        return {
            "fragment_number": 1,
            "key_topics": [],
            "atmosphere": raw[:200],
            "new_characters": [],
            "plot_threads": [],
            "best_quotes": [],
        }


async def _run_writer(messages_payload: list[dict]) -> str:
    payload = {
        "model": PRIMARY_MODEL,
        "messages": messages_payload,
        "max_tokens": MAX_TOKENS,
        "temperature": 0.8,
    }
    result = await _call_glm(payload)
    return result["choices"][0]["message"]["content"]


async def _run_editor(
    summary: str, character_registry_str: str, ratings_feedback: str = ""
) -> str:
    editor_prompt = EDITOR_SYSTEM.format(character_registry=character_registry_str)
    user_content = summary
    if ratings_feedback:
        user_content = f"{ratings_feedback}\n\n---\n\n{summary}"
    else:
        user_content = summary
    payload = {
        "model": PRIMARY_MODEL,
        "messages": [
            {"role": "system", "content": editor_prompt},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.3,
    }
    try:
        result = await _call_glm(payload)
        return result["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error("GLM editor failed: %s", e)
        return summary


async def _run_quiet_day(
    formatted_messages: str,
    character_registry_str: str,
    prev_summaries_text: str,
) -> str:
    user_content = (
        f"{prev_summaries_text}"
        f"СПРАВОЧНИК ПЕРСОНАЖЕЙ:\n{character_registry_str}\n\n"
        f"ПЕРЕПИСКА:\n{formatted_messages}\n\n"
        f"Напиши миниатюру."
    )
    payload = {
        "model": PRIMARY_MODEL,
        "messages": [
            {"role": "system", "content": QUIET_DAY_SYSTEM},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": 4000,
        "temperature": 0.8,
    }
    result = await _call_glm(payload)
    return result["choices"][0]["message"]["content"]


async def _map_reduce_analyze(
    formatted_messages: str,
    character_registry_str: str,
    prev_summaries_text: str,
) -> dict:
    """Map-Reduce для дней с 200+ сообщениями."""
    lines = formatted_messages.split("\n")
    chunks = []
    for i in range(0, len(lines), MESSAGES_PER_CHUNK):
        chunk = "\n".join(lines[i : i + MESSAGES_PER_CHUNK])
        if chunk.strip():
            chunks.append(chunk)

    logger.info("Map-Reduce: %d chunks from %d lines", len(chunks), len(lines))

    mini_analyses = []
    for idx, chunk in enumerate(chunks):
        logger.info("Analyzing chunk %d/%d", idx + 1, len(chunks))
        analysis = await _run_analyst(chunk, character_registry_str, prev_summaries_text)
        mini_analyses.append(analysis)

    merged_topics = []
    merged_quotes = []
    merged_new_chars = []
    max_fragment = 0

    for a in mini_analyses:
        if isinstance(a.get("key_topics"), list):
            merged_topics.extend(a["key_topics"])
        if isinstance(a.get("best_quotes"), list):
            merged_quotes.extend(a["best_quotes"])
        if isinstance(a.get("new_characters"), list):
            merged_new_chars.extend(a["new_characters"])
        fn = a.get("fragment_number", 0)
        if fn > max_fragment:
            max_fragment = fn

    unique_topics = sorted(merged_topics, key=lambda t: t.get("priority", 1), reverse=True)
    low_prio = [t for t in unique_topics if t.get("priority", 1) == 1]
    high_prio = [t for t in unique_topics if t.get("priority", 1) >= 2]
    unique_topics = high_prio + (low_prio[:1] if low_prio else [])
    unique_topics = unique_topics[:5]
    unique_quotes = list(dict.fromkeys(merged_quotes))[:5]

    seen_names = set()
    unique_chars = []
    for c in merged_new_chars:
        name = c.get("username", "")
        if name not in seen_names:
            seen_names.add(name)
            unique_chars.append(c)

    return {
        "fragment_number": max_fragment,
        "key_topics": unique_topics,
        "atmosphere": mini_analyses[0].get("atmosphere", "") if mini_analyses else "",
        "new_characters": unique_chars,
        "plot_threads": mini_analyses[-1].get("plot_threads", []) if mini_analyses else [],
        "best_quotes": unique_quotes,
    }


async def generate_character_titles(
    messages: list[tuple], existing_registry: list[tuple], regenerate_unknown: bool = True
) -> list[tuple[str, str]]:
    """Генерирует WH40K-титулы для участников без титула."""
    existing_map = {name: title for name, title in existing_registry}
    target_names = set()
    for _, username, _, _ in messages:
        if username not in existing_map:
            target_names.add(username)
        elif regenerate_unknown and existing_map[username] == "Неизвестный":
            target_names.add(username)

    if not target_names:
        return []

    names_str = "; ".join(sorted(target_names))
    prompt = (
        "Назначь каждому участнику WH40K-титул. Верни JSON-массив: "
        f'[{{"username": "ник", "title": "WH40K-титул"}}]\n\n'
        f"Участники (каждый с новой строки, используй ПОЛНОЕ имя включая запятые):\n"
    )
    for name in sorted(target_names):
        prompt += f"- {name}\n"
    prompt += (
        "\nТитулы: Легионер, Капеллан, Адептка, Техножрец, Инквизитор, "
        "Лорд-Командир, Сёстра-Армингер, Псайкер, Арбитр, Миссионер, "
        "Ассасин, Исповедник, Сангвинарный Жрец, Хронист, Навигатор, Комиссар и т.д.\n"
        "ВАЖНО: username в ответе должен точно совпадать с исходным, включая запятые и спецсимволы."
    )
    payload = {
        "model": PRIMARY_MODEL,
        "messages": [
            {"role": "system", "content": "Ты — генератор WH40K-титулов. Отвечай только JSON."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 2000,
        "temperature": 0.5,
    }

    try:
        result = await _call_glm(payload)
        raw = result["choices"][0]["message"]["content"].strip()
        if raw.startswith("```"):
            first_nl = raw.find("\n")
            last_bt = raw.rfind("```")
            if first_nl != -1 and last_bt > first_nl:
                raw = raw[first_nl + 1 : last_bt]
        parsed = json.loads(raw)
        return [(item["username"], item["title"]) for item in parsed if "username" in item and "title" in item]
    except Exception as e:
        logger.error("Character title generation failed: %s", e)
        return []


def _fix_fragment_number(summary: str, expected: int) -> str:
    pattern = r"(Хроника Ереси,\s*Фрагмент\s+)[IVXLCDM\d]+"
    return re.sub(pattern, rf"\g<1>{expected}", summary, count=1)


async def summarize(
    messages: list[tuple],
    prev_summaries: list[tuple],
    characters: list[tuple],
    custom_writer_prompt: Optional[str] = None,
    ratings_feedback: str = "",
) -> tuple[str, list[tuple[str, str]]]:
    """
    messages: list of (ts, username, text, reply_to_text)
    prev_summaries: list of (date, summary_text)
    characters: list of (username, wh40k_title)
    custom_writer_prompt: optional override for writer system prompt

    Returns (summary_text, new_characters: list of (username, title))
    """
    formatted = preprocess_messages(messages)
    character_registry_str = format_character_registry(characters)

    next_fragment = _parse_last_fragment(prev_summaries) + 1

    prev_summaries_text = ""
    if prev_summaries:
        prev_summaries_text = "ПРЕДЫДУЩИЕ ЛЕТОПИСИ:\n"
        for day, s in reversed(prev_summaries):
            prev_summaries_text += f"[{day}]: {s}\n\n"

    prev_summaries_text += f"\nСЛЕДУЮЩИЙ НОМЕР ФРАГМЕНТА: {next_fragment}\n"

    new_char_titles: list[tuple[str, str]] = []
    need_map_reduce = _estimate_tokens(formatted) > TOKEN_LIMIT_INPUT
    is_quiet_day = len(messages) < 10

    if is_quiet_day and not need_map_reduce:
        logger.info("Quiet day mode: %d messages", len(messages))
        summary = await _run_quiet_day(formatted, character_registry_str, prev_summaries_text)
    elif TWO_PASS_ENABLED:
        if need_map_reduce:
            logger.info("Map-Reduce mode: %d messages, ~%d tokens", len(messages), _estimate_tokens(formatted))
            analysis = await _map_reduce_analyze(formatted, character_registry_str, prev_summaries_text)
        else:
            logger.info("Two-pass mode: %d messages", len(messages))
            analysis = await _run_analyst(formatted, character_registry_str, prev_summaries_text)

        if isinstance(analysis.get("new_characters"), list):
            for char in analysis["new_characters"]:
                if isinstance(char, dict) and "username" in char and "suggested_title" in char:
                    new_char_titles.append((char["username"], char["suggested_title"]))

        writer_payload = _build_writer_prompt(
            analysis, formatted, character_registry_str, prev_summaries_text, custom_writer_prompt
        )
        summary = await _run_writer(writer_payload)
    else:
        dialog = "\n".join(f"{user}: {text}" for _, user, text, _ in messages)
        user_prompt = (
            f"{prev_summaries_text}"
            f"СПРАВОЧНИК ПЕРСОНАЖЕЙ:\n{character_registry_str}\n\n"
            f"ПЕРЕПИСКА:\n{dialog}\n\n"
            f"Создай летопись дня."
        )
        payload = {
            "model": PRIMARY_MODEL,
            "messages": [
                {"role": "system", "content": custom_writer_prompt or WARHAMMER_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": MAX_TOKENS,
            "temperature": 0.8,
        }
        result = await _call_glm(payload)
        summary = result["choices"][0]["message"]["content"]

    summary = _fix_fragment_number(summary, next_fragment)
    summary = await _run_editor(
        summary, character_registry_str, ratings_feedback=ratings_feedback
    )
    summary = _fix_fragment_number(summary, next_fragment)
    return summary, new_char_titles


async def edit_summary(summary: str, characters: list[tuple]) -> str:
    character_registry_str = format_character_registry(characters)
    return await _run_editor(summary, character_registry_str)
