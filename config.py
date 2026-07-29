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
MEME_CHANNEL_ID_STR = os.getenv("MEME_CHANNEL_ID")
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")
UNSPLASH_API_KEY = os.getenv("UNSPLASH_API_KEY")
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY")

# Telethon user-session for reading source channels (optional).
# Run `python auth_telethon.py` once to create the .session file.
TG_API_ID = os.getenv("TG_API_ID")
TG_API_HASH = os.getenv("TG_API_HASH")
TG_SESSION = os.getenv("TG_SESSION", "tg_w40k_user")
MEME_SOURCE_CHANNELS = [
    c.strip() for c in os.getenv("MEME_SOURCE_CHANNELS", "").split(",") if c.strip()
]
try:
    MEME_HARVEST_PER_CHANNEL = int(os.getenv("MEME_HARVEST_PER_CHANNEL", "3"))
except ValueError:
    MEME_HARVEST_PER_CHANNEL = 3

TG_PROXIES = [
    p.strip()
    for p in os.getenv(
        "TG_PROXIES",
        "socks5://182.48.78.141:8008,socks5://72.195.34.35:27360,socks5://174.75.211.193:4145",
    ).split(",")
    if p.strip()
]

# Telethon (channel harvest) proxy override. If unset, channel_sources falls
# back to TG_PROXIES[0]. Format: 'socks5://host:port' or 'http://host:port'.
TG_TELETHON_PROXY = os.getenv("TG_TELETHON_PROXY")

# Telethon MTProto proxies (Telegram-native, only for Telethon — aiogram can't
# use them). Format: 'host:port:secret' comma-separated. secret is a 32-char
# hex or base64 string from the proxy provider. channel_sources tries them in
# order before falling back to SOCKS/direct.
TG_MTPROTO_PROXIES_RAW = os.getenv("TG_MTPROTO_PROXIES", "")

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
# channel may be numeric id or "@username"
MEME_CHANNEL_ID = MEME_CHANNEL_ID_STR if MEME_CHANNEL_ID_STR and MEME_CHANNEL_ID_STR.startswith("@") else (int(MEME_CHANNEL_ID_STR) if MEME_CHANNEL_ID_STR else None)

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
MEME_MODEL = os.getenv("MEME_MODEL", "glm-5.2")
MAX_TOKENS = 15000
MODEL_RESPONSE_TIMEOUT = 120

TWO_PASS_ENABLED = True
TOKEN_LIMIT_INPUT = 8000
CHARS_PER_TOKEN = 4
MESSAGES_PER_CHUNK = 60


# PySocks / python_socks numeric constants (used by Telethon's `proxy=` arg):
#   SOCKS4 = 1, SOCKS5 = 2, HTTP = 3
_SOCKS_TYPE = {"socks4": 1, "socks5": 2, "socks5h": 2, "http": 3, "https": 3}


def telethon_proxy_tuple(url: str | None) -> tuple | None:
    """Parse 'socks5://host:port' / 'http://host:port' into the (type, host,
    port) tuple Telethon expects (uses PySocks numeric constants). Returns
    None if `url` is empty or unparseable."""
    if not url:
        return None
    import urllib.parse as up
    try:
        parsed = up.urlparse(url)
        scheme = (parsed.scheme or "").lower()
        if scheme not in _SOCKS_TYPE or not parsed.hostname or not parsed.port:
            return None
        return (_SOCKS_TYPE[scheme], parsed.hostname, parsed.port)
    except Exception:
        return None


def parse_mtproto_proxies(raw: str | None) -> list[tuple]:
    """Parse 'host:port:secret[,host:port:secret...]' into a list of
    (host, port, secret) tuples for Telethon MTProto. `host` may itself
    contain ':' (IPv6) — the last two colon-separated tokens are port+secret."""
    out: list[tuple] = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        if len(parts) < 3:
            continue
        secret = parts[-1].strip()
        try:
            port = int(parts[-2])
        except ValueError:
            continue
        host = ":".join(parts[:-2]).strip()
        if host and port > 0 and secret:
            out.append((host, port, secret))
    return out


TG_MTPROTO_PROXIES = parse_mtproto_proxies(TG_MTPROTO_PROXIES_RAW)
