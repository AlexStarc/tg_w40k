import asyncio
import logging
import os
import signal
from datetime import timedelta, datetime
from zoneinfo import ZoneInfo

from aiogram import F, Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import ADMIN_ID, TARGET_CHAT_ID, BAD_SUBSTRINGS, CHUNK_SIZE
from database import (
    init_db,
    migrate_db,
    save_message,
    get_filtered_messages_for_date,
    save_summary,
    get_last_summaries,
    delete_old_messages,
    cleanup_old_data,
    get_all_characters,
    get_character,
    upsert_character,
    save_rating,
    get_avg_rating,
    get_setting,
    set_setting,
    delete_setting,
)
import database as database_mod
from summarizer import summarize, generate_character_titles, _fix_fragment_number
import re as _re

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

bot: Bot = None  # type: ignore[assignment]
dp = Dispatcher()


@dp.message(Command("summary"))
async def cmd_summary(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    summaries = await get_last_summaries(TARGET_CHAT_ID, limit=1)
    if not summaries:
        await message.answer("Летописей не найдено.")
        return

    day, text = summaries[0]
    logger.info(
        "/summary: date=%s chat_id=%s fragment_line=%s len=%d",
        day, TARGET_CHAT_ID, text.split("\n")[0][:80], len(text),
    )
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

    parts = message.text.strip().split()
    if len(parts) != 2 or parts[1] not in {"1", "2", "3", "4", "5"}:
        await message.answer("Использование: /rate <1-5>")
        return

    summaries = await get_last_summaries(TARGET_CHAT_ID, limit=1)
    if not summaries:
        await message.answer("Летописей не найдено.")
        return

    day, _ = summaries[0]
    rating = int(parts[1])
    await save_rating(day, rating)

    avg = await get_avg_rating()
    avg_text = f" (средняя оценка: {avg:.1f})" if avg else ""
    await message.answer(f"Оценка {rating} за {day} сохранена.{avg_text}")


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

    summary, new_chars = await summarize(
        messages, prev_summaries, characters, custom_writer_prompt=custom_prompt
    )

    for name, title in new_chars:
        existing = await get_character(name)
        if not existing or existing == "Неизвестный":
            await upsert_character(name, title)
            logger.info("Auto-assigned character: %s = %s", name, title)

    await save_summary(TARGET_CHAT_ID, yesterday, summary)
    logger.info("Саммаризация за %s сохранена (%d символов).", yesterday, len(summary))

    await bot.send_message(
        ADMIN_ID,
        f"✅ Летопись за {yesterday} готова.\n"
        f"/summary — посмотреть\n"
        f"/send_to_chat — отправить в чат\n"
        f"/rate <1-5> — оценить\n"
        f"/cleanup — удалить сырые сообщения",
    )
    logger.info("daily_summarize FINISHED")
    return True


async def main():
    await init_db()
    await migrate_db()
    logger.info("DB initialized and migrated")

    bot_token = os.getenv("BOT_TOKEN")
    global bot
    bot = Bot(token=bot_token)

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
        await bot.session.close()
        logger.info("Bot stopped.")


async def _shutdown(scheduler):
    logger.info("Shutdown signal received")
    scheduler.shutdown(wait=False)
    await dp.stop_polling()


if __name__ == "__main__":
    asyncio.run(main())
