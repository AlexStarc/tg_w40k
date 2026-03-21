import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

MODEL = "zai-coding-plan/glm-5-turbo"
SUMMARIES_DIR = Path(__file__).parent / "summaries"
MAX_PREV_SUMMARIES = 5

SYSTEM_PROMPT = """\
Ты — архивариус Ордос Милитант, хронист Империума Человечества. Ты ведёшь летопись \
сектора по перехваченным vox-сообщениям.

КАК ПИСАТЬ:
- Это ХУДОЖЕСТВЕННАЯ НАРРАТИВНАЯ ПРОЗА. Не отчёт. Не список. Не таблица. \
Не разбивай по ролям и не делай заголовков для каждого участника.
- Рассказывай ИСТОРИЮ дня как писатель-хронист. Персонажи появляются и действуют \
внутри сюжета, а не перечисляются по очереди. Один абзац может содержать \
действия нескольких персонажей — они взаимодействуют, спорят, соглашаются.
- Каждый абзац — мини-сцена, развёрнутая мысль или описание атмосферы. \
4-8 абзацев на всю хронику.

ЗАГОЛОВОК:
«Хроника Ереси Хоруса, Фрагмент N. Чат Сектора «<название чата>»»
(номер фрагмента — прибавляй 1 к номеру из последней предыдущей хроники, \
или начни с 1 если хроник ещё нет)

КОНЕЦ ХРОНИКИ:
«Послесловие архивариуса» — 1-2 предложения, философский итог дня.

СТИЛЬ:
- Назначай участникам WH40K-титулы (Легионер, Капеллан, Адептка, Техножрец, \
Инквизитор, Лорд-Командир и т.д.) и используй их ЕСТЕСТВЕННО внутри текста, \
а не как заголовки секций.
- Цитируй реальные фразы из сообщений в «» — ключевые, 2-3 цитаты на абзац.
- Привязывай темы к лору WH40K: спор = раскол/ересь, технологии = техно-ересь \
Механикус, новости = донесения разведки, мемы = кринж-культ и т.д.
- Парадоксы и чёрный юмор в духе WH40K: \
«все кричали о X, но никто не заметил Y»
- Тон — торжественный и мрачный, но с иронией.

ПРИМЕР стиля (не копируй, имитируй подход):
«Верный Легионер Дима, не остыв от вчерашнего предательства, обнажил клинок \
и обрушил на братьев манифест. Но тьма уже вошла в сердца — Тёмный Капеллан \
Старк воззвал к братьям, и указал им путь в изгнание. Адептки держали оборону, \
но голоса разумных потонули в шуме.»

СТРОГО:
- Сохраняй ВСЕ факты: кто что сказал, какие темы обсуждались.
- Не выдумывай события, которых не было в сообщениях.
- Пиши на русском языке.
- Если есть предыдущие хроники — развивай сюжетные линии.
- Никаких списков, буллитов, заголовков по ролям, статус-блоков или таблиц.
"""

OPENCODE_BIN = "/Users/a-starch/.opencode/bin/opencode"


def _load_previous_summaries() -> str:
    if not SUMMARIES_DIR.exists():
        return ""
    files = sorted(SUMMARIES_DIR.glob("*.md"))
    recent = files[-MAX_PREV_SUMMARIES:]
    if not recent:
        return ""
    parts: list[str] = []
    for f in recent:
        parts.append(f"### {f.stem}\n{f.read_text(encoding='utf-8').strip()}")
    return "\n\n---\n\n".join(parts)


def summarize(messages: list[str]) -> str:
    if not messages:
        return "Нет сообщений для саммаризации."

    transcript = "\n".join(messages)
    prev = _load_previous_summaries()
    context = ""
    if prev:
        context = (
            f"## Предыдущие хроники (для сохранения континуитета):\n\n{prev}\n\n---\n\n"
        )
    user_prompt = (
        f"{context}"
        f"## Новые перехваченные vox-сообщения за последние 24 часа:\n\n"
        f"{transcript}\n\n"
        "Составь сводку за текущий день. Если в предыдущих хрониках упоминались "
        "развивающиеся сюжеты или открытые вопросы — учти их, укажи развитие событий. "
        "Если это первая сводка — просто составь начальный доклад."
    )

    cmd = [
        OPENCODE_BIN,
        "run",
        "-m",
        MODEL,
        "--format",
        "json",
        user_prompt,
    ]

    logger.info("Calling opencode with %d messages...", len(messages))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            cwd="/Users/a-starch/work/project/tg_w40k",
        )
    except subprocess.TimeoutExpired:
        logger.error("opencode timed out after 300s")
        return "Ошибка: таймаут саммаризации."

    if result.returncode != 0:
        logger.error(
            "opencode failed (rc=%d): %s", result.returncode, result.stderr[:500]
        )
        return f"Ошибка opencode: {result.stderr[:200]}"

    output = result.stdout.strip()

    try:
        events = [json.loads(line) for line in output.splitlines() if line.strip()]
        for event in reversed(events):
            if event.get("type") != "text":
                continue
            part = event.get("part", {})
            text = part.get("text", "")
            if isinstance(text, str) and text.strip():
                return text.strip()
    except json.JSONDecodeError:
        pass

    logger.warning("Failed to parse JSON output, using raw stdout")
    return output if output else "Не удалось получить саммаризацию."
