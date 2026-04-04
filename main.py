import asyncio
import os
import logging
from datetime import date, timedelta, datetime
import pytz
from aiogram import F, Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

from database import (
    init_db,
    save_message,
    get_messages_for_date,
    get_filtered_messages_for_date,
    save_summary,
    get_last_summaries,
    delete_old_messages,
)
from summarizer import summarize, edit_summary

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))      # твой Telegram user_id
TARGET_CHAT_ID = int(os.getenv("CHAT_ID")) # chat_id группы

BAD_SUBSTRINGS = ["подработка", "легкая подработка", "лёгкая подработка", "работа"]

# Разбиваем на части по 4096 символов
CHUNK_SIZE = 4096

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

@dp.message(Command("summary"))
async def cmd_summary(message: Message):
    logging.info(f"SUMMARY command from user_id={message.from_user.id}, ADMIN_ID={ADMIN_ID}")
    if message.from_user.id != ADMIN_ID:
        logging.warning("Rejected: not admin")
        return

    summaries = await get_last_summaries(TARGET_CHAT_ID, limit=1)
    if not summaries:
        await message.answer("Летописей не найдено.")
        return

    day, text = summaries[0]
    full_text = f"📜 Летопись {day}:\n\n{text}"

    for i in range(0, len(full_text), CHUNK_SIZE):
        await message.answer(full_text[i:i + CHUNK_SIZE])

@dp.message(Command("send_to_chat"))
async def cmd_send_to_chat(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    summaries = await get_last_summaries(TARGET_CHAT_ID, limit=1)
    if not summaries:
        await message.answer("Летописей не найдено.")
        return

    day, text = summaries[0]
    full_text = f"📜 *Летопись {day}*\n\n{text}"

    for i in range(0, len(full_text), CHUNK_SIZE):
        await bot.send_message(TARGET_CHAT_ID, full_text[i:i + CHUNK_SIZE])

    await message.answer("Отправлено в чат.")

@dp.message(Command("run_summary"))
async def cmd_run_summary(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    logging.info("cmd_run_summary STARTED")
    await daily_summarize()
    await message.answer("Готово")

@dp.message(Command("cleanup"))
async def cmd_cleanup(message: Message):
    """Удаляет сырые сообщения за вчера после того как саммари проверен"""
    if message.from_user.id != ADMIN_ID:
        return

    msk = pytz.timezone("Europe/Moscow")
    yesterday = (datetime.now(msk).date() - timedelta(days=1)).isoformat()

    await delete_old_messages(TARGET_CHAT_ID, yesterday)
    await message.answer(f"🗑 Сообщения за {yesterday} удалены.")

@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def collect_message(message: Message):
    logging.info(f"Message from chat_id={message.chat.id} type={message.chat.type}")
    if message.chat.id != TARGET_CHAT_ID:
        return
    if not message.text:
        return
    if message.from_user.is_bot:
        return

    text_lower = message.text.lower()

    if any(bad in text_lower for bad in BAD_SUBSTRINGS):
        logging.info("Skip spam message: %s", message.text)
        return

    username =  message.from_user.full_name or message.from_user.username
    await save_message(message.chat.id, username, message.text)

async def daily_summarize():
    logging.info("daily_summarize STARTED")
    msk = pytz.timezone("Europe/Moscow")
    yesterday = (datetime.now(msk).date() - timedelta(days=1)).isoformat()

    messages = await get_filtered_messages_for_date(TARGET_CHAT_ID, yesterday)
    if not messages:
        logging.info(f"Нет подходящих сообщений за {yesterday} (всё — одиночки/боты/спам)")
        return

    prev_summaries = await get_last_summaries(TARGET_CHAT_ID, limit=5)
    summary = summarize(messages, prev_summaries)
    summary = edit_summary(summary)
    await save_summary(TARGET_CHAT_ID, yesterday, summary)
    logging.info(f"Саммаризация за {yesterday} сохранена.")

    # Уведомляем — удалять пока НЕ удаляем
    await bot.send_message(
        ADMIN_ID,
        f"✅ Летопись за {yesterday} готова.\n"
        f"/summary — посмотреть\n"
        f"/send_to_chat — отправить в чат\n"
        f"/cleanup — удалить сырые сообщения за {yesterday}"
    )
    logging.info("daily_summarize FINISHED")

async def main():
    await init_db()
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(
        daily_summarize,
        "cron",
        hour=0,
        minute=5,
        misfire_grace_time=3600
    )
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
