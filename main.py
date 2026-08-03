import asyncio
import logging
import os
import signal
from datetime import timedelta, datetime
from zoneinfo import ZoneInfo

from aiogram import F, Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, BufferedInputFile, InputMediaPhoto, BotCommand, BotCommandScopeChat
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import (
    ADMIN_ID,
    TARGET_CHAT_ID,
    BAD_SUBSTRINGS,
    CHUNK_SIZE,
    TG_PROXY,
    TG_PROXIES,
    MEME_CHANNEL_ID,
    PEXELS_API_KEY,
    UNSPLASH_API_KEY,
    PIXABAY_API_KEY,
    MAX_TOKENS,
    MEME_MODEL,
    MEME_SOURCE_CHANNELS,
    MEME_HARVEST_PER_CHANNEL,
    HEALTH_CHECK_INTERVAL,
)
from database import (
    init_db,
    migrate_db,
    save_message,
    get_filtered_messages_for_date,
    save_summary,
    get_last_summaries,
    get_all_summaries,
    delete_old_messages,
    cleanup_old_data,
    get_all_characters,
    get_character,
    upsert_character,
    save_rating,
    get_avg_rating,
    get_last_ratings,
    get_setting,
    set_setting,
    delete_setting,
    save_meme_rating,
    get_meme_bias,
    get_channel_state,
    save_channel_posts,
    get_channel_posts,
    count_channel_posts,
    get_channels_state,
)
import database as database_mod
from summarizer import summarize, generate_character_titles, _fix_fragment_number, _call_glm, describe_image
import meme as meme_mod
import channel_sources
import json as _json
import random as _random
import re as _re
from prompts import MEME_SEED_SYSTEM, MEME_HARVEST_SYSTEM, MEME_STYLE_FEWSHOT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

bot: Bot = None  # type: ignore[assignment]
dp = Dispatcher()


@dp.message(Command("help"))
async def cmd_help(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "📜 <b>Летописи</b>\n"
        "/summary — последняя летопись\n"
        "/send_to_chat — отправить последнюю в чат\n"
        "/run_summary — сгенерировать сейчас\n"
        "/regenerate <i>YYYY-MM-DD</i> — перегенерировать (по умолч. вчера)\n"
        "/rate <i>1-5</i> <i>[коммент]</i> — оценить последнюю\n"
        "/history — список последних летописей\n"
        "/stats — статистика\n\n"
        "🎭 <b>Данные и промпты</b>\n"
        "/characters — справочник персонажей\n"
        "/character <i>user титул</i> — задать титул\n"
        "/set_prompt <i>[текст|reset]</i> — кастомный промпт летописца\n"
        "/fix_fragments — починить нумерацию фрагментов\n"
        "/cleanup — удалить сообщения за вчера\n"
        "/purge — удалить всё старше 14 дней\n\n"
        "🎨 <b>Мемы (Crisiswoman)</b>\n"
        "/meme — сгенерить мём на ревью (кнопки: опубликовать/стиль/цитата/⭐/❌)\n"
        "/meme_add <i>цитата :: панчлайн :: dark</i> — добавить пару в банк\n"
        "/meme_seed <i>[N]</i> — освежить банк через GLM\n"
        "/meme_harvest — собрать посты из каналов-источников\n"
        "/meme_channels — статус каналов-источников\n"
        "/proxy_rotate — сменить прокси вручную\n\n"
        "<i>Авто: летопись в 00:05 МСК, харвест+освежение банка мемов в 04:00, health-check каждые 30 мин.</i>"
    )


@dp.message(Command("summary"))
async def cmd_summary(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    summaries = await get_last_summaries(TARGET_CHAT_ID, limit=1)
    if not summaries:
        await message.answer("Летописей не найдено.")
        return

    day, text = summaries[0]
    logger.info("/summary: date=%s len=%d", day, len(text))
    full_text = f"📜 Летопись {day}:\n\n{text}"

    for i in range(0, len(full_text), CHUNK_SIZE):
        await message.answer(full_text[i : i + CHUNK_SIZE])


@dp.message(Command("send_to_chat"))
async def cmd_send_to_chat(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    summaries = await get_last_summaries(TARGET_CHAT_ID, limit=1)
    if not summaries:
        await message.answer("Летописей не найдено.")
        return

    day, text = summaries[0]
    full_text = f"📜 Летопись {day}:\n\n{text}"

    for i in range(0, len(full_text), CHUNK_SIZE):
        await bot.send_message(TARGET_CHAT_ID, full_text[i : i + CHUNK_SIZE])

    await message.answer("Отправлено в чат.")


@dp.message(Command("run_summary"))
async def cmd_run_summary(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("Начинаю генерацию...")
    try:
        await daily_summarize()
        await message.answer("Готово")
    except Exception as e:
        logger.exception("run_summary failed")
        await message.answer(f"Ошибка: {e}")


@dp.message(Command("cleanup"))
async def cmd_cleanup(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    msk = ZoneInfo("Europe/Moscow")
    yesterday = (datetime.now(msk).date() - timedelta(days=1)).isoformat()

    await delete_old_messages(TARGET_CHAT_ID, yesterday)
    await message.answer(f"🗑 Сообщения за {yesterday} удалены.")


@dp.message(Command("purge"))
async def cmd_purge(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    msg_count, sum_count, rat_count = await cleanup_old_data(TARGET_CHAT_ID, days=14)
    await message.answer(
        f"🧹 Удалено старше 14 дней:\n"
        f"• Сообщений: {msg_count}\n"
        f"• Летописей: {sum_count}\n"
        f"• Оценок: {rat_count}"
    )


@dp.message(Command("rate"))
async def cmd_rate(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    parts = message.text.strip().split(maxsplit=2)
    if len(parts) < 2 or parts[1] not in {"1", "2", "3", "4", "5"}:
        await message.answer("Использование: /rate <1-5> [комментарий]")
        return

    summaries = await get_last_summaries(TARGET_CHAT_ID, limit=1)
    if not summaries:
        await message.answer("Летописей не найдено.")
        return

    day, _ = summaries[0]
    rating = int(parts[1])
    comment = parts[2] if len(parts) > 2 else None
    await save_rating(day, rating, comment=comment)

    avg = await get_avg_rating()
    avg_text = f" (средняя: {avg:.1f})" if avg else ""
    comment_text = f"\n💬 {comment}" if comment else ""
    await message.answer(f"Оценка {rating} за {day} сохранена.{avg_text}{comment_text}")


@dp.message(Command("regenerate"))
async def cmd_regenerate(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    parts = message.text.strip().split()
    msk = ZoneInfo("Europe/Moscow")

    if len(parts) >= 2:
        target_date = parts[1]
    else:
        target_date = (datetime.now(msk).date() - timedelta(days=1)).isoformat()

    await message.answer(f"Перегенерация за {target_date}...")
    try:
        ok = await daily_summarize(target_date=target_date)
        if ok:
            await message.answer(f"✅ Летопись за {target_date} перегенерирована.")
        else:
            await message.answer(f"⚠ Нет сообщений за {target_date} — летопись не создана.")
    except Exception as e:
        logger.exception("regenerate failed")
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command("characters"))
async def cmd_characters(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    chars = await get_all_characters()
    if not chars:
        await message.answer("Справочник персонажей пуст.")
        return

    lines = ["📜 Справочник персонажей:\n"]
    for name, title in chars:
        lines.append(f"• {name} = {title}")
    await message.answer("\n".join(lines))


@dp.message(Command("fix_fragments"))
async def cmd_fix_fragments(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    import aiosqlite
    async with aiosqlite.connect(database_mod.DB_PATH) as db:
        async with db.execute(
            "SELECT date, summary FROM summaries WHERE chat_id=? ORDER BY date ASC",
            (TARGET_CHAT_ID,),
        ) as cursor:
            rows = await cursor.fetchall()

    fixes = []
    num = 1
    for date, text in rows:
        fixed = _fix_fragment_number(text, num)
        if fixed != text:
            fixes.append(f"{date}: → {num}")
            await save_summary(TARGET_CHAT_ID, date, fixed)
        num += 1

    if fixes:
        await message.answer(f"✅ Исправлено:\n" + "\n".join(fixes))
    else:
        await message.answer("Все номера фрагментов уже корректны.")


@dp.message(Command("character"))
async def cmd_character_set(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    parts = message.text.strip().split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Использование: /character <username> <WH40K титул>")
        return

    username = parts[1]
    title = parts[2]
    await upsert_character(username, title)
    await message.answer(f"✅ {username} = {title}")


@dp.message(Command("set_prompt"))
async def cmd_set_prompt(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        current = await get_setting("writer_prompt")
        if current:
            await message.answer(f"Текущий кастомный промпт:\n\n{current[:1000]}...")
        else:
            await message.answer("Используется дефолтный промпт.")
        return

    new_prompt = parts[1]
    if new_prompt.lower() in ("reset", "сброс", "default"):
        await delete_setting("writer_prompt")
        await message.answer("Промпт сброшен к дефолтному.")
    else:
        await set_setting("writer_prompt", new_prompt)
        await message.answer(f"✅ Промпт обновлён ({len(new_prompt)} символов).")


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    avg = await get_avg_rating()
    chars = await get_all_characters()
    avg_text = f"{avg:.1f}" if avg else "нет оценок"
    await message.answer(
        f"📊 Статистика:\n"
        f"• Персонажей в справочнике: {len(chars)}\n"
        f"• Средняя оценка летописей: {avg_text}"
    )


@dp.message(Command("history"))
async def cmd_history(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    rows = await get_all_summaries(TARGET_CHAT_ID)
    if not rows:
        await message.answer("Летописей нет.")
        return

    lines = ["📜 Все летописи:\n"]
    for dt, preview in rows:
        lines.append(f"• {dt} — {preview}...")
    await message.answer("\n".join(lines))


@dp.callback_query(F.data.startswith("show:"))
async def cb_show(callback: CallbackQuery):
    _, day = callback.data.split(":", 1)
    summaries = await get_last_summaries(TARGET_CHAT_ID, limit=10)
    text = None
    for d, t in summaries:
        if d == day:
            text = t
            break
    if not text:
        await callback.answer("Летопись не найдена")
        return
    full_text = f"📜 Летопись {day}:\n\n{text}"
    for i in range(0, len(full_text), CHUNK_SIZE):
        await callback.message.answer(full_text[i : i + CHUNK_SIZE])
    await callback.answer()


@dp.callback_query(F.data.startswith("send:"))
async def cb_send(callback: CallbackQuery):
    _, day = callback.data.split(":", 1)
    summaries = await get_last_summaries(TARGET_CHAT_ID, limit=10)
    text = None
    for d, t in summaries:
        if d == day:
            text = t
            break
    if not text:
        await callback.answer("Летопись не найдена")
        return
    full_text = f"📜 Летопись {day}:\n\n{text}"
    for i in range(0, len(full_text), CHUNK_SIZE):
        await bot.send_message(TARGET_CHAT_ID, full_text[i : i + CHUNK_SIZE])
    await callback.answer("Отправлено в чат")


@dp.callback_query(F.data.startswith("retry:"))
async def cb_retry(callback: CallbackQuery):
    _, day = callback.data.split(":", 1)
    await callback.message.edit_text("Генерирую ещё раз...")
    try:
        ok = await daily_summarize(target_date=day)
        if ok:
            await callback.message.edit_text(f"✅ Летопись за {day} перегенерирована.")
        else:
            await callback.message.edit_text(f"⚠ Нет сообщений за {day}.")
    except Exception as e:
        await callback.message.edit_text(f"❌ Снова ошибка:\n{e}")
    await callback.answer()


@dp.callback_query(F.data.startswith("rate:"))
async def cb_rate(callback: CallbackQuery):
    _, day, val = callback.data.split(":")
    rating = int(val)
    await save_rating(day, rating)
    avg = await get_avg_rating()
    avg_text = f" (средняя: {avg:.1f})" if avg else ""
    await callback.answer(f"Оценка {rating} за {day}{avg_text}")
    await callback.message.edit_reply_markup(reply_markup=None)


# ---- meme generator (crisiswoman): review-then-publish, parallel to summary ----

async def _gen_meme(entry_id: str | None = None):
    bias = await get_meme_bias()
    res = await meme_mod.generate_meme(
        entry_id=entry_id, bias=bias,
        pexels_key=PEXELS_API_KEY, unsplash_key=UNSPLASH_API_KEY,
        pixabay_key=PIXABAY_API_KEY,
    )
    meme_mod.cache_meme(res)   # small rotating history on disk (gitignored)
    return res


def _meme_caption(res: dict) -> str:
    fx = f" · 🎞{res['effect']}" if res.get("effect") else ""
    return (f"стиль: <b>{res['layout']}</b>{fx} · фон: {res.get('img_source','?')} · "
            f"{res['source']} · [{res['tone']}] · {res['quote'][:40]}…")


def _meme_kb(entry_id: str, layout: str):
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="📤 Опубликовать", callback_data="meme:pub"))
    b.row(InlineKeyboardButton(text="🎨 Другой стиль", callback_data=f"meme:stl:{entry_id}"),
          InlineKeyboardButton(text="🔄 Другая цитата", callback_data="meme:new"))
    b.row(*[InlineKeyboardButton(text=label, callback_data=f"meme:rate:{entry_id}:{layout}:{n}")
            for n, label in ((5, "⭐5"), (4, "4"), (3, "3"), (2, "2"), (1, "1💩"))])
    b.row(InlineKeyboardButton(text="❌", callback_data="meme:del"))
    return b.as_markup()


@dp.message(Command("meme"))
async def cmd_meme(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    if not MEME_CHANNEL_ID:
        await message.answer("MEME_CHANNEL_ID не задан в .env — публикация отключена.")
        return
    status = await message.answer("🎨 Генерирую мём…")
    try:
        res = await _gen_meme()
        await status.delete()
        await bot.send_photo(
            ADMIN_ID,
            BufferedInputFile(res["image"], filename="meme.jpg"),
            caption=_meme_caption(res),
            reply_markup=_meme_kb(res["entry_id"], res["layout"]),
        )
    except Exception as e:
        logger.exception("meme generation failed")
        await message.answer(f"❌ Ошибка генерации мема: {e}")


async def _meme_edit(callback: CallbackQuery, entry_id: str | None):
    res = await _gen_meme(entry_id=entry_id)
    media = InputMediaPhoto(
        media=BufferedInputFile(res["image"], filename="meme.jpg"),
        caption=_meme_caption(res),
    )
    await callback.message.edit_media(media, reply_markup=_meme_kb(res["entry_id"], res["layout"]))


@dp.callback_query(F.data == "meme:new")
async def cb_meme_new(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.answer("Новый мём")
    try:
        await _meme_edit(callback, None)
    except Exception as e:
        await callback.answer(f"Ошибка: {e}", show_alert=True)


@dp.callback_query(F.data.startswith("meme:stl:"))
async def cb_meme_style(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    entry_id = callback.data.split(":", 2)[2]
    await callback.answer("Другой стиль")
    try:
        await _meme_edit(callback, entry_id)
    except Exception as e:
        await callback.answer(f"Ошибка: {e}", show_alert=True)


@dp.callback_query(F.data == "meme:pub")
async def cb_meme_pub(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    if not callback.message.photo:
        await callback.answer("Фото недоступно — сгенерируй заново /meme", show_alert=True)
        return
    try:
        await bot.send_photo(MEME_CHANNEL_ID, callback.message.photo[-1].file_id)
        await callback.message.edit_caption(
            caption=(callback.message.caption or "") + "\n\n✅ ОПУБЛИКОВАНО",
            reply_markup=None,
        )
        await callback.answer("Опубликовано в канал")
    except Exception as e:
        logger.exception("meme publish failed")
        await callback.answer(f"Ошибка публикации: {e}", show_alert=True)


@dp.callback_query(F.data == "meme:del")
async def cb_meme_del(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.delete()
    await callback.answer()


@dp.callback_query(F.data.startswith("meme:rate:"))
async def cb_meme_rate(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    _, _, entry_id, layout, val = callback.data.split(":")
    await save_meme_rating(entry_id, layout, int(val))
    bias = await get_meme_bias()
    e_avg = bias["entry"].get(entry_id)
    l_avg = bias["layout"].get(layout)
    msg = f"Оценка {val}"
    bits = []
    if e_avg:
        bits.append(f"цитата {e_avg:.1f}")
    if l_avg:
        bits.append(f"стиль {l_avg:.1f}")
    if bits:
        msg += f" (ср. {' / '.join(bits)})"
    await callback.answer(msg)


@dp.message(Command("meme_add"))
async def cmd_meme_add(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    payload = message.text.partition(" ")[2].strip()
    parts = [p.strip() for p in payload.split("::")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        await message.answer("Формат: <code>/meme_add цитата :: панчлайн :: dark</code>\n"
                             "(третья часть — тон, по умолчанию light)")
        return
    quote, punch = parts[0], parts[1]
    tone = parts[2].strip().lower() if len(parts) > 2 and parts[2].strip() else "light"
    eid = meme_mod.append_entry(quote, punch, tone)
    if not eid:
        await message.answer("⚠️ Такая цитата (или очень похожая) уже есть в банке — не добавлено")
        return
    await message.answer(f"✅ Добавлено в банк (<code>{eid}</code>):\n"
                         f"«{quote}» → {punch}")


def _extract_pairs(raw: str) -> list:
    """Robustly pull a JSON array out of an LLM response that may wrap it in
    prose, markdown fences, or return {quotes:[...]}."""
    raw = raw.strip()
    try:
        v = _json.loads(raw)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            return v.get("quotes") or v.get("pairs") or v.get("data") or []
    except _json.JSONDecodeError:
        pass
    m = _re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw, _re.DOTALL)
    if m:
        try:
            return _json.loads(m.group(1))
        except _json.JSONDecodeError:
            pass
    s, e = raw.find("["), raw.rfind("]")
    if s != -1 and e > s:
        try:
            return _json.loads(raw[s:e + 1])
        except _json.JSONDecodeError:
            pass
    return []


async def refresh_meme_bank(n: int = 8, with_channels: bool = True) -> dict:
    """Generate n new quote+punchline pairs via GLM and append (deduped) to
    bank.json. Quality is policed downstream by the rating→bias feedback loop:
    weak pairs get low ratings and stop appearing.

    When `with_channels` is True and Telethon is configured, a small random
    sample of captions harvested from source channels is prepended to the
    user prompt as style orientation (few-shot)."""
    bank = meme_mod.load_bank()
    examples = _random.sample(bank, min(6, len(bank)))
    ex_block = "\n".join(f'- «{e.quote}» → {e.punchline} [{e.tone}]' for e in examples)
    existing = "; ".join(e.quote for e in bank)[:1500]

    fewshot_block = ""
    if with_channels and channel_sources.is_configured() and MEME_SOURCE_CHANNELS:
        try:
            sample = await get_channel_posts(limit=10)
            if sample:
                lines = [f"- [{ch}] {text.strip()[:300]}" for ch, text in sample if text]
                if lines:
                    fewshot_block = MEME_STYLE_FEWSHOT + "\n".join(lines) + "\n\n"
        except Exception:
            logger.exception("few-shot fetch failed; continuing without")

    user_content = (
        f"{fewshot_block}"
        f"Примеры стиля:\n{ex_block}\n\n"
        f"Уже есть (не повторяй): {existing}\n\n"
        f"Придумай {n} НОВЫх пар. Ответ — СТРОГО JSON-массив, без markdown и пояснений."
    )
    payload = {
        "model": MEME_MODEL,
        "messages": [
            {"role": "system", "content": MEME_SEED_SYSTEM},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.9,
    }
    result = await _call_glm(payload)
    choice = result["choices"][0]
    msg = choice.get("message") or {}
    finish = choice.get("finish_reason")
    raw = (msg.get("content") or "").strip()
    if not raw:
        raw = (msg.get("reasoning_content") or "").strip()
        if raw:
            logger.info("meme seed: content empty, using reasoning_content (%d chars)", len(raw))
    if not raw:
        logger.warning("meme seed: empty response. finish_reason=%s msg_keys=%s",
                       finish, list(msg.keys()))
    pairs = _extract_pairs(raw)
    if not pairs:
        logger.warning("meme seed: no JSON parsed (%d chars). raw[:300]=%s", len(raw), raw[:300])
    added = 0
    skipped = 0
    for p in pairs:
        q = (p.get("quote") or "").strip().strip("«»\"'\"")
        punch = (p.get("punchline") or "").strip()
        if not q or not punch:
            continue
        tone = "dark" if str(p.get("tone", "")).lower().startswith("d") else "light"
        src = (p.get("source") or "").strip() or "—"
        eid = meme_mod.append_entry(q, punch, tone, source=src)
        if eid:
            added += 1
        else:
            skipped += 1
    meme_mod.cap_bank()
    return {"added": added, "received": len(pairs), "skipped": skipped,
            "raw_len": len(raw), "sample": pairs[0] if pairs else None,
            "fewshot": bool(fewshot_block)}


async def _extract_pairs_from_posts(channel: str, posts: list, n: int) -> int:
    """GLM-extract up to n punchline-pairs from raw channel captions and
    append them to bank.json with source = channel handle. Returns the count
    actually added (after dedup)."""
    sample = posts if len(posts) <= 15 else _random.sample(posts, 15)
    captions = "\n---\n".join((p.text or "").strip()[:600] for p in sample if (p.text or "").strip())
    captions = captions[:6000]
    if not captions.strip():
        return 0
    bank = meme_mod.load_bank()
    existing = "; ".join(e.quote for e in bank)[:1500]
    payload = {
        "model": MEME_MODEL,
        "messages": [
            {"role": "system", "content": MEME_HARVEST_SYSTEM},
            {"role": "user", "content": (
                f"Подписи постов канала {channel}:\n\n{captions}\n\n"
                f"Уже есть (не повторяй): {existing}\n\n"
                f"Извлеки до {n} пар. Ответ — СТРОГО JSON-массив."
            )},
        ],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.85,
    }
    try:
        result = await _call_glm(payload)
    except Exception:
        logger.exception("harvest GLM call failed for %s", channel)
        return 0
    raw = ((result.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    pairs = _extract_pairs(raw)
    added = 0
    for p in pairs:
        q = (p.get("quote") or "").strip().strip("«»\"'\"")
        punch = (p.get("punchline") or "").strip()
        if not q or not punch:
            continue
        tone = "dark" if str(p.get("tone", "")).lower().startswith("d") else "light"
        eid = meme_mod.append_entry(q, punch, tone, source=channel)
        if eid:
            added += 1
    return added


async def harvest_channels(channels: list[str] | None = None,
                           per_channel: int | None = None) -> dict:
    """For each source channel: pull new captions via Telethon → cache in DB
    → extract N punchline-pairs via GLM → append to bank.json with
    source=channel handle. Returns aggregate stats.

    `channels` defaults to config.MEME_SOURCE_CHANNELS, `per_channel` to
    config.MEME_HARVEST_PER_CHANNEL. No-op (returns skipped marker) if
    Telethon is not configured or no channels are set."""
    if not channel_sources.is_configured():
        return {"skipped": "not_configured", "channels": [], "added": 0}
    chans = channels if channels is not None else MEME_SOURCE_CHANNELS
    per = per_channel if per_channel is not None else MEME_HARVEST_PER_CHANNEL
    if not chans:
        return {"skipped": "no_channels", "channels": [], "added": 0}
    total_added = 0
    per_chan = []
    for ch in chans:
        last = await get_channel_state(ch)
        posts = await channel_sources.fetch_since(ch, last, limit=80, include_media_only=True)
        if not posts:
            per_chan.append({"channel": ch, "fetched": 0, "new_cached": 0, "added": 0,
                             "vision_described": 0})
            continue
        # Enrich image-only posts with a vision-generated pseudo-caption.
        vision_count = 0
        for p in posts:
            if p.text or not p.has_media:
                continue
            img = await channel_sources.download_post_image(ch, p.tg_id)
            if not img:
                continue
            try:
                desc = await describe_image(img)
            except Exception:
                logger.exception("vision describe failed for %s/%d", ch, p.tg_id)
                continue
            if desc:
                p.text = desc
                vision_count += 1
        # Drop anything still without text (e.g. vision failed or non-photo media).
        usable = [p for p in posts if p.text]
        if not usable:
            per_chan.append({"channel": ch, "fetched": len(posts), "new_cached": 0,
                             "added": 0, "vision_described": vision_count})
            continue
        rows = [(p.tg_id, p.text) for p in usable]
        try:
            new_cached = await save_channel_posts(ch, rows)
        except Exception:
            logger.exception("save_channel_posts failed for %s", ch)
            new_cached = 0
        added = await _extract_pairs_from_posts(ch, usable, per)
        per_chan.append({"channel": ch, "fetched": len(posts),
                         "new_cached": new_cached, "added": added,
                         "vision_described": vision_count})
        total_added += added
    meme_mod.cap_bank()
    return {"channels": per_chan, "added": total_added}


async def daily_refresh_bank():
    try:
        if channel_sources.is_configured() and MEME_SOURCE_CHANNELS:
            try:
                h = await harvest_channels()
                logger.info("harvest: %s", h)
                await bot.send_message(
                    ADMIN_ID,
                    f"📡 Харвест каналов: +{h['added']} цитат из {len(h['channels'])} каналов",
                )
            except Exception:
                logger.exception("harvest in daily refresh failed")
                await bot.send_message(ADMIN_ID, "⚠️ Харвест каналов упал — см. журнал")
        res = await refresh_meme_bank(8)
        logger.info("meme bank refreshed: +%d (received %d, skipped %d, fewshot=%s)",
                    res["added"], res["received"], res["skipped"], res["fewshot"])
        await bot.send_message(
            ADMIN_ID,
            f"➕ Банк мемов: +{res['added']} цитат (GLM вернул {res['received']}, дубли пропущены {res['skipped']})",
        )
    except Exception:
        logger.exception("meme bank refresh failed")


@dp.message(Command("meme_seed"))
async def cmd_meme_seed(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = (message.text or "").split()
    n = 8
    if len(args) > 1:
        try:
            n = max(1, min(int(args[1]), 20))
        except ValueError:
            n = 8
    status = await message.answer(f"🌱 Генерирую {n} новых цитат через GLM…")
    try:
        res = await refresh_meme_bank(n)
        await status.delete()
        if res["added"]:
            dup = f", {res['skipped']} дублей пропущено" if res["skipped"] else ""
            await message.answer(
                f"✅ Добавлено {res['added']} цитат (GLM вернул {res['received']} пар{dup})")
        else:
            tail = ""
            if res["received"]:
                tail = f" — но все {res['skipped']} дубли/пустые" if res["skipped"] else " — но все пустые"
            else:
                tail = f" — GLM не вернул разборный JSON ({res['raw_len']} симв). Подробности в журнале."
            await message.answer(f"⚠️ Не добавлено ни одной (получено {res['received']}){tail}")
    except Exception as e:
        logger.exception("meme_seed failed")
        await message.answer(f"❌ {e}")


@dp.message(Command("meme_harvest"))
async def cmd_meme_harvest(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    if not channel_sources.is_configured():
        await message.answer("⚠️ Telethon не настроен. Задай <code>TG_API_ID</code>/<code>TG_API_HASH</code> в .env "
                             "и выполни <code>python auth_telethon.py</code>.")
        return
    if not MEME_SOURCE_CHANNELS:
        await message.answer("⚠️ <code>MEME_SOURCE_CHANNELS</code> пуст — добавь каналы в .env.")
        return
    status = await message.answer(f"📡 Собираю посты из {len(MEME_SOURCE_CHANNELS)} каналов…")
    try:
        res = await harvest_channels()
        if res.get("skipped"):
            await status.edit_text(f"⚠️ Пропущено: {res['skipped']}")
            return
        lines = [f"📡 Харвест завершён: +{res['added']} цитат"]
        for s in res["channels"]:
            vision = s.get("vision_described", 0)
            vtag = f", vision={vision}" if vision else ""
            lines.append(
                f"  • {s['channel']}: fetched={s['fetched']}, new_cached={s['new_cached']}, +{s['added']}{vtag}"
            )
        await status.edit_text("\n".join(lines))
    except Exception as e:
        logger.exception("meme_harvest failed")
        try:
            await status.edit_text(f"❌ {e}")
        except Exception:
            await message.answer(f"❌ {e}")


@dp.message(Command("meme_channels"))
async def cmd_meme_channels(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    if not MEME_SOURCE_CHANNELS:
        await message.answer("ℹ️ Источники не заданы (<code>MEME_SOURCE_CHANNELS</code> пуст).")
        return
    flag = "✅" if channel_sources.is_configured() else "❌ (нет TG_API_ID/HASH или сессия не авторизована)"
    counts = await count_channel_posts()
    states = {s["channel"]: s for s in await get_channels_state()}
    lines = [f"Каналов: {len(MEME_SOURCE_CHANNELS)}  Telethon: {flag}"]
    for ch in MEME_SOURCE_CHANNELS:
        c = counts.get(ch, 0)
        st = states.get(ch)
        last = st["last_fetch_at"] if st and st["last_fetch_at"] else "—"
        lines.append(f"  • {ch}: {c} постов в кэше, last_fetch={last}")
    await message.answer("\n".join(lines))


@dp.message(Command("proxy_rotate"))
async def cmd_proxy_rotate(message: Message):
    """Manually trigger proxy rotation: close current session, find a fresh
    proxy (TG_PROXIES first, then the remote pool), rebind bot.session."""
    if message.from_user.id != ADMIN_ID:
        return
    status = await message.answer("🔄 Ротирую прокси (закрываю session, ищу новый)…")
    try:
        try:
            await bot.session.close()
        except Exception:
            logger.exception("proxy_rotate: failed to close old session")
        new_proxy = await _find_any_working_proxy()
        bot.session = AiohttpSession(proxy=new_proxy) if new_proxy else AiohttpSession()
        if new_proxy:
            await status.edit_text(f"✅ Новый прокси: <code>{new_proxy}</code>")
        else:
            await status.edit_text(
                "⚠️ Не нашёл рабочего прокси (ни TG_PROXIES, ни пул). Сессия — direct."
            )
    except Exception as e:
        logger.exception("proxy_rotate failed")
        try:
            await status.edit_text(f"❌ {e}")
        except Exception:
            await message.answer(f"❌ {e}")


@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def collect_message(message: Message):
    if message.chat.id != TARGET_CHAT_ID:
        return
    if not message.text:
        return
    if message.from_user.is_bot:
        return

    text_lower = message.text.lower()
    if any(bad in text_lower for bad in BAD_SUBSTRINGS):
        logger.info("Skip spam message: %s", message.text)
        return

    username = message.from_user.full_name or message.from_user.username

    reply_to_text = None
    if message.reply_to_message and message.reply_to_message.text:
        reply_to_text = message.reply_to_message.text

    await save_message(
        message.chat.id,
        username,
        message.text,
        message_id=message.message_id,
        reply_to_text=reply_to_text,
    )


async def auto_cleanup():
    logger.info("auto_cleanup STARTED")
    msg_count, sum_count, rat_count = await cleanup_old_data(TARGET_CHAT_ID, days=14)
    logger.info(
        "auto_cleanup: deleted %d messages, %d summaries, %d ratings older than 14 days",
        msg_count, sum_count, rat_count,
    )


async def daily_summarize(target_date: str = None) -> bool:
    logger.info("daily_summarize STARTED")

    msk = ZoneInfo("Europe/Moscow")
    if target_date:
        yesterday = target_date
    else:
        yesterday = (datetime.now(msk).date() - timedelta(days=1)).isoformat()

    messages = await get_filtered_messages_for_date(TARGET_CHAT_ID, yesterday)
    if not messages:
        logger.info("Нет подходящих сообщений за %s", yesterday)
        return False

    characters = await get_all_characters()
    prev_summaries = await get_last_summaries(TARGET_CHAT_ID, limit=5)

    existing_names = {name for name, _ in characters}
    unknown_names = {name for name, title in characters if title == "Неизвестный"}
    missing_names = set()
    for _, username, _, _ in messages:
        if username not in existing_names:
            missing_names.add(username)

    need_regenerate = missing_names | unknown_names
    if need_regenerate:
        new_titles = await generate_character_titles(messages, characters)
        for name, title in new_titles:
            await upsert_character(name, title)
            logger.info("Character title: %s = %s", name, title)
        characters = await get_all_characters()

    custom_prompt = await get_setting("writer_prompt")

    ratings_feedback = ""
    last_ratings = await get_last_ratings(TARGET_CHAT_ID, limit=3)
    if last_ratings:
        lines = ["ОЦЕНКИ ПРЕДЫДУЩИХ ЛЕТОПИСЕЙ (учти при редактировании):"]
        for r_date, r_val, r_summary, r_comment in last_ratings:
            first_line = r_summary.split("\n")[0][:60] if r_summary else "(нет текста)"
            if r_val >= 4:
                hint = "хорошо, сохраняй стиль"
            elif r_val == 3:
                hint = "средне, старайся лучше"
            else:
                hint = "слабо, больше цитат и связных историй"
            comment_text = f" | отзыв: {r_comment}" if r_comment else ""
            lines.append(
                f"- {r_date}: оценка {r_val}/5 ({first_line}...) — {hint}{comment_text}"
            )
        ratings_feedback = "\n".join(lines)

    try:
        summary, new_chars = await summarize(
            messages,
            prev_summaries,
            characters,
            custom_writer_prompt=custom_prompt,
            ratings_feedback=ratings_feedback,
        )
    except Exception as e:
        logger.exception("Generation failed for %s", yesterday)
        builder = InlineKeyboardBuilder()
        builder.button(text="🔄 Повторить за %s" % yesterday, callback_data=f"retry:{yesterday}")
        await bot.send_message(
            ADMIN_ID,
            f"❌ Ошибка генерации за {yesterday}:\n{e}\n\nНажми кнопку для повтора.",
            reply_markup=builder.as_markup(),
        )
        return False

    for name, title in new_chars:
        existing = await get_character(name)
        if not existing or existing == "Неизвестный":
            await upsert_character(name, title)
            logger.info("Auto-assigned character: %s = %s", name, title)

    await save_summary(TARGET_CHAT_ID, yesterday, summary)
    logger.info("Саммаризация за %s сохранена (%d символов).", yesterday, len(summary))

    builder = InlineKeyboardBuilder()
    builder.button(text="📜 Прочитать", callback_data=f"show:{yesterday}")
    builder.button(text="📤 В чат", callback_data=f"send:{yesterday}")
    builder.row(
        InlineKeyboardButton(text="⭐5", callback_data=f"rate:{yesterday}:5"),
        InlineKeyboardButton(text="4", callback_data=f"rate:{yesterday}:4"),
        InlineKeyboardButton(text="3", callback_data=f"rate:{yesterday}:3"),
        InlineKeyboardButton(text="2", callback_data=f"rate:{yesterday}:2"),
        InlineKeyboardButton(text="1💩", callback_data=f"rate:{yesterday}:1"),
    )

    await bot.send_message(
        ADMIN_ID,
        f"✅ Летопись за {yesterday} готова ({len(summary)} символов).",
        reply_markup=builder.as_markup(),
    )
    logger.info("daily_summarize FINISHED")
    return True


async def _find_any_working_proxy() -> str | None:
    """Try each TG_PROXIES in order, then fall back to the remote pool. Returns
    a 'socks5://host:port' (or http://) URL, or None if nothing reaches Telegram."""
    token = os.getenv("BOT_TOKEN")
    for proxy_url in TG_PROXIES:
        try:
            test_session = AiohttpSession(proxy=proxy_url)
            test_bot = Bot(token=token, session=test_session, request_timeout=10)
            await test_bot.get_me()
            await test_session.close()
            return proxy_url
        except Exception:
            continue
    try:
        import proxy_pool
        return await proxy_pool.find_working_proxy(TG_PROXIES)
    except Exception:
        logger.exception("proxy_pool lookup failed during health-check")
        return None


async def health_check_proxy():
    """Periodic liveness probe. If bot.get_me() through the current session
    fails, rotate to a fresh proxy (TG_PROXIES first, then the remote pool)
    and rebind bot.session without restarting the process."""
    global bot
    if not bot:
        return
    try:
        await bot.get_me()
        return  # session is alive
    except Exception as e:
        logger.warning("health_check: current session failed (%s); rotating proxy", e)

    try:
        await bot.session.close()
    except Exception:
        logger.exception("health_check: failed to close old session")

    new_proxy = await _find_any_working_proxy()
    bot.session = AiohttpSession(proxy=new_proxy) if new_proxy else AiohttpSession()
    if new_proxy:
        logger.info("health_check: switched to proxy %s", new_proxy)
        try:
            await bot.send_message(
                ADMIN_ID,
                f"🔄 health_check: переключил прокси на <code>{new_proxy}</code>",
            )
        except Exception:
            logger.warning("health_check: couldn't deliver switch notification")
    else:
        logger.error("health_check: no working proxy found; session is now direct")


async def main():
    await init_db()
    await migrate_db()
    meme_mod.ensure_bank()
    logger.info("DB initialized and migrated")

    bot_token = os.getenv("BOT_TOKEN")
    global bot

    session = None
    proxies_to_try = []

    try:
        logger.info("Testing direct connection without proxy...")
        test_bot = Bot(token=bot_token, request_timeout=10)
        await test_bot.get_me()
        logger.info("Direct connection works, no proxy needed")
    except Exception as e:
        logger.warning("Direct connection failed: %s, trying proxies...", e)
        proxies_to_try = TG_PROXIES if TG_PROXY else TG_PROXIES

        for proxy_url in proxies_to_try:
            if not proxy_url:
                continue
            try:
                logger.info("Testing proxy: %s", proxy_url)
                session = AiohttpSession(proxy=proxy_url)
                test_bot = Bot(token=bot_token, session=session, request_timeout=10)
                await test_bot.get_me()
                logger.info("Proxy works: %s", proxy_url)
                break
            except Exception as pe:
                logger.warning("Proxy %s failed: %s, trying next...", proxy_url, pe)
                session = None
                continue

        if not session:
            logger.warning("All configured proxies failed; sampling remote SOCKS5 pool...")
            try:
                import proxy_pool
                pool_proxy = await proxy_pool.find_working_proxy(TG_PROXIES)
                if pool_proxy:
                    logger.info("Pool fallback proxy: %s", pool_proxy)
                    session = AiohttpSession(proxy=pool_proxy)
            except Exception:
                logger.exception("Proxy pool fallback failed")

    bot = Bot(
        token=bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        request_timeout=60,
        session=session,
    )
    logger.info("Bot initialized%s", " with active session (direct or proxy)" if session else " WITHOUT working session — set_my_commands will likely fail")

    await bot.set_my_commands(
        [BotCommand(command=c, description=d) for c, d in (
            ("help", "список команд"),
            ("summary", "последняя летопись"),
            ("send_to_chat", "отправить летопись в чат"),
            ("run_summary", "сгенерировать сейчас"),
            ("regenerate", "перегенерировать за дату"),
            ("rate", "оценить летопись 1-5"),
            ("meme", "сгенерить мём на ревью"),
            ("meme_add", "добавить цитату в банк"),
            ("meme_seed", "освежить банк через GLM"),
            ("meme_harvest", "харвест цитат из каналов"),
            ("meme_channels", "статус источников-каналов"),
            ("proxy_rotate", "сменить прокси вручную"),
        )],
        scope=BotCommandScopeChat(chat_id=ADMIN_ID),
    )

    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(
        daily_summarize,
        "cron",
        hour=0,
        minute=5,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        auto_cleanup,
        "cron",
        hour=3,
        minute=0,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        daily_refresh_bank,
        "cron",
        hour=4,
        minute=0,
        misfire_grace_time=3600,
    )
    if HEALTH_CHECK_INTERVAL > 0:
        scheduler.add_job(
            health_check_proxy,
            "interval",
            seconds=HEALTH_CHECK_INTERVAL,
            misfire_grace_time=HEALTH_CHECK_INTERVAL,
        )
        logger.info("Health-check scheduled every %ds", HEALTH_CHECK_INTERVAL)
    scheduler.start()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(_shutdown(scheduler)))

    logger.info("Bot starting...")
    try:
        await dp.start_polling(bot)
    except asyncio.CancelledError:
        pass
    finally:
        scheduler.shutdown(wait=False)
        await channel_sources.close_client()
        await bot.session.close()
        logger.info("Bot stopped.")


async def _shutdown(scheduler):
    logger.info("Shutdown signal received")
    scheduler.shutdown(wait=False)
    await dp.stop_polling()
    await channel_sources.close_client()


if __name__ == "__main__":
    asyncio.run(main())
