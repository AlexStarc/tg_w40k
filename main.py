import asyncio
import os
import logging
from datetime import date, timedelta
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
from summarizer import summarize

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))      # твой Telegram user_id
TARGET_CHAT_ID = int(os.getenv("CHAT_ID")) # chat_id группы

BAD_SUBSTRINGS = ["подработка", "легкая подработка", "лёгкая подработка", "работа"]

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

@dp.message(Command("summary"))
async def cmd_summary(message: Message):
    """Команда для тебя — показывает вчерашнюю саммаризацию"""
    logging.info(f"SUMMARY command from user_id={message.from_user.id}, ADMIN_ID={ADMIN_ID}")
    if message.from_user.id != ADMIN_ID:
        logging.warning("Rejected: not admin")
        return

    logging.info(f"SUMMARY command from user_id={message.from_user.id}, is admin!")
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    summaries = await get_last_summaries(TARGET_CHAT_ID, limit=1)
    if summaries:
        day, text = summaries[0]
        await message.answer(f"📜 Летопись {day}:\n\n{text}")
    else:
        await message.answer("Летописей не найдено.")

@dp.message(Command("send_to_chat"))
async def cmd_send_to_chat(message: Message):
    """Команда для тебя — отправляет последнюю саммаризацию в чат"""
    if message.from_user.id != ADMIN_ID:
        return
    summaries = await get_last_summaries(TARGET_CHAT_ID, limit=1)
    if summaries:
        day, text = summaries[0]
        await bot.send_message(TARGET_CHAT_ID, f"📜 *Летопись {day}*\n\n{text}", parse_mode="Markdown")
        await message.answer("Отправлено в чат.")

@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def collect_message(message: Message):
    logging.info(f"Message from chat_id={message.chat.id} type={message.chat.type}")
    if message.chat.id != TARGET_CHAT_ID:
        return
    if not message.text:
        return

    text_lower = message.text.lower()

    if any(bad in text_lower for bad in BAD_SUBSTRINGS):
        logging.info("Skip spam message: %s", message.text)
        return

    username = message.from_user.username or message.from_user.full_name
    await save_message(message.chat.id, username, message.text)

async def daily_summarize():
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    # Берём уже ОТФИЛЬТРОВАННЫЕ сообщения
    messages = await get_filtered_messages_for_date(TARGET_CHAT_ID, yesterday)
    if not messages:
        logging.info(f"Нет подходящих сообщений за {yesterday} (всё — одиночки/боты/спам)")
        return

    prev_summaries = await get_last_summaries(TARGET_CHAT_ID, limit=5)
    summary = summarize(messages, prev_summaries)
    await save_summary(TARGET_CHAT_ID, yesterday, summary)
    await delete_old_messages(TARGET_CHAT_ID, yesterday)
    logging.info(f"Саммаризация за {yesterday} сохранена.")
    await bot.send_message(
        ADMIN_ID,
        f"✅ Летопись за {yesterday} готова. /summary чтобы посмотреть, /send_to_chat чтобы отправить."
    )

async def main():
    await init_db()
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(daily_summarize, "cron", hour=0, minute=5)  # каждую ночь в 00:05
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
