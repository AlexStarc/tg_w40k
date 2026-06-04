import os
import sys
import logging

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID_STR = os.getenv("ADMIN_ID")
CHAT_ID_STR = os.getenv("CHAT_ID")
GLM_API_KEY = os.getenv("GLM_API_KEY")
TG_PROXY = os.getenv("TG_PROXY")

_missing = []
if not BOT_TOKEN:
    _missing.append("BOT_TOKEN")
if not ADMIN_ID_STR:
    _missing.append("ADMIN_ID")
if not CHAT_ID_STR:
    _missing.append("CHAT_ID")
if not GLM_API_KEY:
    _missing.append("GLM_API_KEY")

if _missing:
    logging.error("Missing env variables: %s", ", ".join(_missing))
    sys.exit(1)

ADMIN_ID = int(ADMIN_ID_STR)
TARGET_CHAT_ID = int(CHAT_ID_STR)

BAD_SUBSTRINGS = [
    "подработка",
    "легкая подработка",
    "лёгкая подработка",
    "работа",
    "доход",
    "финансы",
    "выплаты",
    "смотри в профиле",
    "смотри в био",
    "смотри в описании",
    "заработок",
]
CHUNK_SIZE = 4096
MAX_SUMMARY_CHARS = 4000

GLM_URL = "https://api.z.ai/api/coding/paas/v4/chat/completions"
PRIMARY_MODEL = "glm-5.1"
FALLBACK_MODEL = "glm-5-turbo"
MAX_TOKENS = 15000
MODEL_RESPONSE_TIMEOUT = 120

TWO_PASS_ENABLED = True
TOKEN_LIMIT_INPUT = 8000
CHARS_PER_TOKEN = 4
MESSAGES_PER_CHUNK = 60
