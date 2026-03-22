import aiosqlite
from datetime import date

DB_PATH = "bot.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                date TEXT,
                ts INTEGER,
                username TEXT,
                text TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                date TEXT UNIQUE,
                summary TEXT,
                created_at INTEGER
            )
        """)
        await db.commit()

async def save_message(chat_id, username, text):
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages (chat_id, date, ts, username, text) VALUES (?,?,strftime('%s','now'),?,?)",
            (chat_id, today, username, text)
        )
        await db.commit()

async def get_messages_for_date(chat_id, day: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT username, text FROM messages WHERE chat_id=? AND date=? ORDER BY ts",
            (chat_id, day)
        ) as cursor:
            return await cursor.fetchall()

async def save_summary(chat_id, day: str, summary: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO summaries (chat_id, date, summary, created_at) VALUES (?,?,?,strftime('%s','now'))",
            (chat_id, day, summary)
        )
        await db.commit()

async def get_last_summaries(chat_id, limit=5):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT date, summary FROM summaries WHERE chat_id=? ORDER BY date DESC LIMIT ?",
            (chat_id, limit)
        ) as cursor:
            return await cursor.fetchall()

async def delete_old_messages(chat_id, day: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM messages WHERE chat_id=? AND date=?",
            (chat_id, day)
        )
        await db.commit()
