import pytest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, patch, MagicMock

from database import init_db, migrate_db, save_summary, get_last_summaries, DB_PATH
from summarizer import preprocess_messages, _estimate_tokens, format_character_registry, _parse_last_fragment, _fix_fragment_number


class TestDateLogic:
    def test_yesterday_calculation_msk(self):
        msk = ZoneInfo("Europe/Moscow")
        now = datetime.now(msk)
        yesterday = (now.date() - timedelta(days=1)).isoformat()
        assert yesterday == (datetime.now(msk).date() - timedelta(days=1)).isoformat()

    def test_daily_summarize_uses_yesterday_when_no_date(self):
        msk = ZoneInfo("Europe/Moscow")
        expected = (datetime.now(msk).date() - timedelta(days=1)).isoformat()
        with patch("main.get_filtered_messages_for_date", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = []
            from main import daily_summarize
            import asyncio
            asyncio.get_event_loop().run_until_complete(daily_summarize())
            called_date = mock_get.call_args[0][1]
            assert called_date == expected

    def test_daily_summarize_uses_provided_date(self):
        target = "2026-04-17"
        with patch("main.get_filtered_messages_for_date", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = []
            from main import daily_summarize
            import asyncio
            asyncio.get_event_loop().run_until_complete(daily_summarize(target_date=target))
            called_date = mock_get.call_args[0][1]
            assert called_date == target


class TestPreprocessing:
    def test_preprocess_empty(self):
        assert preprocess_messages([]) == ""

    def test_preprocess_formats_timestamps(self):
        msgs = [(1713400000, "Dima", "Hello", None)]
        result = preprocess_messages(msgs)
        assert "Dima" in result
        assert "Hello" in result

    def test_preprocess_includes_reply_to(self):
        msgs = [(1713400000, "Dima", "Yes", "Original message text")]
        result = preprocess_messages(msgs)
        assert "в ответ на" in result
        assert "Original message text" in result

    def test_preprocess_reply_to_truncated(self):
        long_text = "A" * 200
        msgs = [(1713400000, "Dima", "Yes", long_text)]
        result = preprocess_messages(msgs)
        assert "..." in result
        assert len([l for l in result.split("\n") if "в ответ на" in l][0]) < 200

    def test_preprocess_counts_participants(self):
        msgs = [
            (1713400000, "Dima", "Hi", None),
            (1713400100, "Anna", "Hey", None),
            (1713400200, "Dima", "Again", None),
        ]
        result = preprocess_messages(msgs)
        assert "Участников: 2" in result
        assert "Всего сообщений: 3" in result

    def test_preprocess_time_blocks(self):
        msgs = [
            (1713360000, "Dima", "Morning msg", None),
        ]
        result = preprocess_messages(msgs)
        assert "УТРО" in result or "НОЧЬ" in result or "ДЕНЬ" in result or "ВЕЧЕР" in result


class TestFragmentParsing:
    def test_parse_roman_liv(self):
        prev = [("2026-04-18", "«Хроника Ереси, Фрагмент LIV. Лог Сектора «МВК»»\nSome text")]
        assert _parse_last_fragment(prev) == 54

    def test_parse_roman_lii(self):
        prev = [("2026-04-18", "«Хроника Ереси, Фрагмент LII. Лог Сектора «МВК»»\nSome text")]
        assert _parse_last_fragment(prev) == 52

    def test_parse_roman_xlvii(self):
        prev = [("2026-04-17", "«Хроника Ереси, Фрагмент XLVII. Лог Сектора «МВК»»\nSome text")]
        assert _parse_last_fragment(prev) == 47

    def test_parse_roman_iii(self):
        prev = [("2026-04-17", "«Хроника Ереси, Фрагмент III. Лог Сектора «МВК»»")]
        assert _parse_last_fragment(prev) == 3

    def test_parse_arabic_number(self):
        prev = [("2026-04-17", "«Хроника Ереси, Фрагмент 42. Лог Сектора «МВК»»")]
        assert _parse_last_fragment(prev) == 42

    def test_parse_no_fragment(self):
        prev = [("2026-04-17", "Just some text without fragment number")]
        assert _parse_last_fragment(prev) == 0

    def test_parse_empty(self):
        assert _parse_last_fragment([]) == 0

    def test_parse_uses_first_summary(self):
        prev = [
            ("2026-04-18", "Фрагмент LII"),
            ("2026-04-17", "Фрагмент L"),
        ]
        assert _parse_last_fragment(prev) == 52


class TestFixFragmentNumber:
    def test_fix_wrong_arabic(self):
        text = "«Хроника Ереси, Фрагмент 2. Лог Сектора «МВК»»\nSome text"
        result = _fix_fragment_number(text, 53)
        assert "Фрагмент 53" in result
        assert "Фрагмент 2" not in result

    def test_fix_wrong_roman(self):
        text = "«Хроника Ереси, Фрагмент I. Лог Сектора «МВК»»\nSome text"
        result = _fix_fragment_number(text, 53)
        assert "Фрагмент 53" in result

    def test_no_change_when_correct(self):
        text = "«Хроника Ереси, Фрагмент 53. Лог Сектора «МВК»»\nSome text"
        result = _fix_fragment_number(text, 53)
        assert result == text

    def test_fix_preserves_rest(self):
        text = "«Хроника Ереси, Фрагмент 1. Лог Сектора «МВК»»\nParagraph one.\nParagraph two."
        result = _fix_fragment_number(text, 53)
        assert "Paragraph one." in result
        assert "Paragraph two." in result


class TestTokenEstimation:
    def test_estimate_tokens(self):
        text = "A" * 100
        tokens = _estimate_tokens(text)
        assert tokens == 25  # 100 / 4

    def test_estimate_tokens_empty(self):
        assert _estimate_tokens("") == 0


class TestCharacterRegistry:
    def test_format_empty(self):
        assert "нет" in format_character_registry([])

    def test_format_characters(self):
        chars = [("Dima", "Легионер-Декард"), ("Anna", "Адептка Серафима")]
        result = format_character_registry(chars)
        assert "Dima = Легионер-Декард" in result
        assert "Anna = Адептка Серафима" in result


class TestDatabase:
    @pytest.fixture(autouse=True)
    async def setup_db(self, tmp_path):
        import database
        original_db = database.DB_PATH
        database.DB_PATH = str(tmp_path / "test.db")
        await init_db()
        await migrate_db()
        yield
        database.DB_PATH = original_db

    @pytest.mark.asyncio
    async def test_save_and_get_summary(self):
        await save_summary(123, "2026-04-17", "Test summary")
        summaries = await get_last_summaries(123, limit=1)
        assert len(summaries) == 1
        assert summaries[0][0] == "2026-04-17"
        assert summaries[0][1] == "Test summary"

    @pytest.mark.asyncio
    async def test_summary_overwrite(self):
        await save_summary(123, "2026-04-17", "First")
        await save_summary(123, "2026-04-17", "Second")
        summaries = await get_last_summaries(123, limit=1)
        assert summaries[0][1] == "Second"

    @pytest.mark.asyncio
    async def test_get_last_summaries_order(self):
        await save_summary(123, "2026-04-15", "Old")
        await save_summary(123, "2026-04-17", "New")
        summaries = await get_last_summaries(123, limit=5)
        assert summaries[0][0] == "2026-04-17"
        assert summaries[1][0] == "2026-04-15"

    @pytest.mark.asyncio
    async def test_cleanup_old_data(self):
        from database import cleanup_old_data, save_message, save_rating

        old_date = (datetime.now().date() - timedelta(days=20)).isoformat()
        recent_date = (datetime.now().date() - timedelta(days=2)).isoformat()

        await save_message(123, "User", "Old msg", message_id=1)
        from database import aiosqlite
        import database
        async with aiosqlite.connect(database.DB_PATH) as db:
            await db.execute("UPDATE messages SET date=? WHERE id=1", (old_date,))
            await db.commit()

        await save_summary(123, old_date, "Old summary")
        await save_summary(123, recent_date, "Recent summary")
        await save_rating(old_date, 3)

        msg_count, sum_count, rat_count = await cleanup_old_data(123, days=14)

        assert msg_count == 1
        assert sum_count == 1
        assert rat_count == 1

        summaries = await get_last_summaries(123, limit=5)
        assert len(summaries) == 1
        assert summaries[0][0] == recent_date

    @pytest.mark.asyncio
    async def test_character_upsert(self):
        from database import upsert_character, get_character, get_all_characters

        await upsert_character("Dima", "Легионер")
        title = await get_character("Dima")
        assert title == "Легионер"

        await upsert_character("Dima", "Капеллан")
        title = await get_character("Dima")
        assert title == "Капеллан"

        chars = await get_all_characters()
        assert len(chars) == 1
        assert chars[0] == ("Dima", "Капеллан")

    @pytest.mark.asyncio
    async def test_settings(self):
        from database import get_setting, set_setting, delete_setting

        assert await get_setting("test_key") is None

        await set_setting("test_key", "test_value")
        assert await get_setting("test_key") == "test_value"

        await delete_setting("test_key")
        assert await get_setting("test_key") is None


class TestBotNoiseFilter:
    """The observed chat has bots that post and self-delete; their text must
    never reach the chronicle source material."""

    def _message(self, *, is_bot=False, sender_chat=None, reply=None, text="привет"):
        from aiogram.types import Chat, User

        msg = MagicMock()
        msg.chat = MagicMock(spec=Chat)
        msg.chat.id = -100123
        msg.chat.type = "supergroup"
        msg.text = text
        msg.message_id = 42
        msg.sender_chat = sender_chat
        msg.from_user = MagicMock(spec=User)
        msg.from_user.is_bot = is_bot
        msg.from_user.full_name = "Human"
        msg.from_user.username = "human"
        if reply is not None:
            msg.reply_to_message = reply
        else:
            msg.reply_to_message = None
        return msg

    async def _collect(self, message):
        from main import collect_message

        saved = {}

        async def _capture(chat_id, username, text, **kwargs):
            saved.update(kwargs)
            saved["text"] = text

        with patch("main.TARGET_CHAT_ID", -100123), \
                patch("main.save_message", new=AsyncMock(side_effect=_capture)):
            await collect_message(message)
        return saved

    @pytest.mark.asyncio
    async def test_bot_author_message_not_stored(self):
        assert await self._collect(self._message(is_bot=True)) == {}

    @pytest.mark.asyncio
    async def test_sender_chat_message_not_stored(self):
        assert await self._collect(self._message(sender_chat=MagicMock())) == {}

    @pytest.mark.asyncio
    async def test_reply_to_bot_drops_bot_text(self):
        from aiogram.types import Message, User

        replied = MagicMock(spec=Message)
        replied.text = "БОТСКИЙ СПАМ, который скоро удалят"
        replied.from_user = MagicMock(spec=User)
        replied.from_user.is_bot = True

        saved = await self._collect(self._message(reply=replied))
        assert saved["text"] == "привет"
        assert saved["reply_to_text"] is None

    @pytest.mark.asyncio
    async def test_reply_to_human_keeps_context(self):
        from aiogram.types import Message, User

        replied = MagicMock(spec=Message)
        replied.text = "человеческий ответ"
        replied.from_user = MagicMock(spec=User)
        replied.from_user.is_bot = False

        saved = await self._collect(self._message(reply=replied))
        assert saved["reply_to_text"] == "человеческий ответ"
