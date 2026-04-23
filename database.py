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


async def migrate_db():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("PRAGMA table_info(messages)")
        columns = {row[1] for row in await cursor.fetchall()}

        if "message_id" not in columns:
            await db.execute("ALTER TABLE messages ADD COLUMN message_id INTEGER")
        if "reply_to_text" not in columns:
            await db.execute("ALTER TABLE messages ADD COLUMN reply_to_text TEXT")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS characters (
                username TEXT PRIMARY KEY,
                wh40k_title TEXT,
                first_seen TEXT,
                updated_at INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ratings (
                date TEXT PRIMARY KEY,
                rating INTEGER,
                created_at INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_chat_date ON messages(chat_id, date)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_username ON messages(username)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_summaries_chat_date ON summaries(chat_id, date)"
        )
        await db.commit()


async def save_message(chat_id, username, text, message_id=None, reply_to_text=None):
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages (chat_id, date, ts, username, text, message_id, reply_to_text) "
            "VALUES (?,?,strftime('%s','now'),?,?,?,?)",
            (chat_id, today, username, text, message_id, reply_to_text),
        )
        await db.commit()


async def get_messages_for_date(chat_id, day: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT username, text FROM messages WHERE chat_id=? AND date=? ORDER BY ts",
            (chat_id, day),
        ) as cursor:
            return await cursor.fetchall()


async def get_filtered_messages_for_date(chat_id, day: str):
    """
    Возвращает (ts, username, text, reply_to_text) от юзеров,
    которые за день написали >1 сообщения И они не все одинаковые.
    """
    query = """
    WITH user_stats AS (
        SELECT username, COUNT(*) AS cnt, COUNT(DISTINCT text) AS distinct_cnt
        FROM messages
        WHERE chat_id = ? AND date = ?
        GROUP BY username
    ),
    good_users AS (
        SELECT username FROM user_stats WHERE cnt > 1 AND distinct_cnt > 1
    )
    SELECT m.ts, m.username, m.text, m.reply_to_text
    FROM messages m
    JOIN good_users g ON g.username = m.username
    WHERE m.chat_id = ? AND m.date = ?
    ORDER BY m.ts
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(query, (chat_id, day, chat_id, day)) as cursor:
            return await cursor.fetchall()


async def save_summary(chat_id, day: str, summary: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO summaries (chat_id, date, summary, created_at) "
            "VALUES (?,?,?,strftime('%s','now'))",
            (chat_id, day, summary),
        )
        await db.commit()


async def get_last_summaries(chat_id, limit=5):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT date, summary FROM summaries WHERE chat_id=? ORDER BY date DESC LIMIT ?",
            (chat_id, limit),
        ) as cursor:
            return await cursor.fetchall()


async def delete_old_messages(chat_id, day: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM messages WHERE chat_id=? AND date=?",
            (chat_id, day),
        )
        await db.commit()


async def cleanup_old_data(chat_id, days: int = 14):
    cutoff = (date.today() - __import__("datetime").timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM messages WHERE chat_id=? AND date<?",
            (chat_id, cutoff),
        )
        msg_count = cur.rowcount
        cur = await db.execute(
            "DELETE FROM summaries WHERE chat_id=? AND date<?",
            (chat_id, cutoff),
        )
        sum_count = cur.rowcount
        cur = await db.execute(
            "DELETE FROM ratings WHERE date<?",
            (cutoff,),
        )
        rat_count = cur.rowcount
        await db.commit()
    return msg_count, sum_count, rat_count


async def get_all_characters():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT username, wh40k_title FROM characters ORDER BY first_seen"
        ) as cursor:
            return await cursor.fetchall()


async def get_character(username: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT wh40k_title FROM characters WHERE username = ?",
            (username,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def upsert_character(username: str, wh40k_title: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO characters (username, wh40k_title, first_seen, updated_at) "
            "VALUES (?, ?, date('now'), strftime('%s','now')) "
            "ON CONFLICT(username) DO UPDATE SET wh40k_title=?, updated_at=strftime('%s','now')",
            (username, wh40k_title, wh40k_title),
        )
        await db.commit()


async def save_rating(day: str, rating: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO ratings (date, rating, created_at) "
            "VALUES (?, ?, strftime('%s','now'))",
            (day, rating),
        )
        await db.commit()


async def get_avg_rating():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT AVG(rating) FROM ratings") as cursor:
            row = await cursor.fetchone()
            return row[0] if row and row[0] else None


async def get_last_ratings(chat_id: int, limit: int = 5):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT r.date, r.rating, s.summary "
            "FROM ratings r "
            "LEFT JOIN summaries s ON r.date = s.date AND s.chat_id = ? "
            "ORDER BY r.date DESC LIMIT ?",
            (chat_id, limit),
        ) as cursor:
            return await cursor.fetchall()


async def get_setting(key: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM settings WHERE key = ?",
            (key,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        await db.commit()


async def delete_setting(key: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM settings WHERE key = ?", (key,))
        await db.commit()
