import json
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

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
)
from prompts import (
    WARHAMMER_SYSTEM,
    ANALYST_SYSTEM,
    EDITOR_SYSTEM,
    QUIET_DAY_SYSTEM,
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


def _estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


def _ts_to_hhmm(ts: Optional[int]) -> str:
    if not ts:
        return "??:??"
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
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
                hour = datetime.fromtimestamp(ts, tz=timezone.utc).hour
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


async def _run_editor(summary: str, character_registry_str: str) -> str:
    editor_prompt = EDITOR_SYSTEM.format(character_registry=character_registry_str)
    payload = {
        "model": PRIMARY_MODEL,
        "messages": [
            {"role": "system", "content": editor_prompt},
            {"role": "user", "content": summary},
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

    unique_topics = merged_topics[:5]
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
    messages: list[tuple], existing_registry: list[tuple]
) -> list[tuple[str, str]]:
    """Генерирует WH40K-титулы для новых участников."""
    existing_names = {name for name, _ in existing_registry}
    new_names = set()
    for _, username, _, _ in messages:
        if username not in existing_names:
            new_names.add(username)

    if not new_names:
        return []

    names_str = ", ".join(new_names)
    prompt = (
        "Назначь каждому участнику WH40K-титул. Верни JSON-массив: "
        f'[{{"username": "ник", "title": "WH40K-титул"}}]\nУчастники: {names_str}\n\n'
        "Титулы: Легионер, Капеллан, Адептка, Техножрец, Инквизитор, "
        "Лорд-Командир, Сёстра-Армингер, Псайкер, Арбитр, Миссионер, "
        "Ассасин, Исповедник, Санеус, Хронист, Навигатор, Комиссар и т.д."
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
        return [(name, "Неизвестный") for name in new_names]


async def summarize(
    messages: list[tuple],
    prev_summaries: list[tuple],
    characters: list[tuple],
    custom_writer_prompt: Optional[str] = None,
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

    prev_summaries_text = ""
    if prev_summaries:
        prev_summaries_text = "ПРЕДЫДУЩИЕ ЛЕТОПИСИ:\n"
        for day, s in reversed(prev_summaries):
            prev_summaries_text += f"[{day}]: {s}\n\n"

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

    summary = await _run_editor(summary, character_registry_str)
    return summary, new_char_titles


async def edit_summary(summary: str, characters: list[tuple]) -> str:
    character_registry_str = format_character_registry(characters)
    return await _run_editor(summary, character_registry_str)
