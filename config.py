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
# Meme-trend feed: memepedia.ru main page (RU-hosted, reachable directly from
# the VM without a proxy) — fresh article titles + all-time top block feed a
# weekly GLM digest that is injected into /meme_seed as trend inspiration.
TREND_SOURCE_URLS = [
    u.strip() for u in os.getenv(
        "TREND_SOURCE_URLS", "https://memepedia.ru/"
    ).split(",") if u.strip()
]
try:
    MEME_HARVEST_PER_CHANNEL = int(os.getenv("MEME_HARVEST_PER_CHANNEL", "3"))
except ValueError:
    MEME_HARVEST_PER_CHANNEL = 3

TG_PROXIES = [
    p.strip()
    for p in os.getenv(
        "TG_PROXIES",
        "socks5://119.28.13.138:1080,socks5://220.158.233.26:1080,socks5://70.166.65.160:4145",
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

# Fallback SOCKS5 pool: when all TG_PROXIES fail at startup, the bot samples a
# public socks5 list, tests PROXY_POOL_SAMPLE entries concurrently against
# api.telegram.org, and uses the first that responds. Cached in DB across runs.
PROXY_REMOTE_SOURCES = [
    s.strip() for s in os.getenv(
        "PROXY_REMOTE_SOURCES",
        "https://raw.githubusercontent.com/SevenworksDev/proxy-list/main/proxies/socks5.txt,"
        "https://raw.githubusercontent.com/ProxyScraper/ProxyScraper/main/socks5.txt"
    ).split(",") if s.strip()
]
# Priority tier: small HEALTH-CHECKED socks5 lists (CI-verified upstream).
# Tested in full BEFORE sampling the big raw pools above — a few hundred
# verified entries beat a random sample of 100k unverified ones.
PROXY_PRIORITY_SOURCES = [
    s.strip() for s in os.getenv(
        "PROXY_PRIORITY_SOURCES",
        "https://raw.githubusercontent.com/xyzs996/free-proxy-health-list/main/socks5.txt"
    ).split(",") if s.strip()
]
try:
    PROXY_POOL_SAMPLE = int(os.getenv("PROXY_POOL_SAMPLE", "100"))
    PROXY_POOL_TIMEOUT = float(os.getenv("PROXY_POOL_TIMEOUT", "4"))
    PROXY_POOL_CONCURRENCY = int(os.getenv("PROXY_POOL_CONCURRENCY", "20"))
    PROXY_POOL_CACHE_TTL = int(os.getenv("PROXY_POOL_CACHE_TTL", "86400"))
except ValueError:
    PROXY_POOL_SAMPLE = 100
    PROXY_POOL_TIMEOUT = 4.0
    PROXY_POOL_CONCURRENCY = 20
    PROXY_POOL_CACHE_TTL = 86400

# Periodic session liveness probe (seconds). Every interval, the bot pings
# get_me() through the current session; on failure it rotates to a fresh proxy
# (TG_PROXIES first, then the remote pool) without restarting the process.
try:
    HEALTH_CHECK_INTERVAL = int(os.getenv("HEALTH_CHECK_INTERVAL", "1800"))
except ValueError:
    HEALTH_CHECK_INTERVAL = 1800

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
VISION_MODEL = os.getenv("VISION_MODEL", "glm-4v-plus")
VISION_MAX_TOKENS = int(os.getenv("VISION_MAX_TOKENS", "600"))
# Gemini is used as the primary vision backend when available — z.ai currently
# exposes only text models. Get a free key at https://aistudio.google.com/app/apikey
# (Gemini 1.5/2.0 Flash: 15 rpm, no charge for low volume).
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"
# OpenAI is the second vision backend. Needs OPENAI_API_KEY from
# https://platform.openai.com/apikeys. gpt-4o-mini is the cheapest vision
# option (~$0.15/1M input, $0.60/1M output). Works from any region if your
# OpenAI account is in good standing.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
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
