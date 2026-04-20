import os
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from dotenv import load_dotenv

try:
    from openai import AsyncOpenAI
except Exception:  # pragma: no cover
    AsyncOpenAI = None

load_dotenv()

DB_PATH = "botreminder.sqlite3"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-nano")
OPENAI_TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
API_MONTHLY_LIMIT_USD = float(os.getenv("API_MONTHLY_LIMIT_USD", "5"))
BOT_TIMEZONE = ZoneInfo(os.getenv("BOT_TIMEZONE", "Asia/Novosibirsk"))
BOT_OWNER_ID = os.getenv("BOT_OWNER_ID")
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "")
GOOGLE_APPS_SCRIPT_URL = os.getenv("GOOGLE_APPS_SCRIPT_URL", "")
GOOGLE_APPS_SCRIPT_SECRET = os.getenv("GOOGLE_APPS_SCRIPT_SECRET", "")

TEXT_MODEL_PRICING = {
    "gpt-5.4-nano": {"input": 0.20, "output": 1.25},
    "gpt-5.4-mini": {"input": 0.75, "output": 4.50},
}
TRANSCRIBE_PRICING_PER_MINUTE = {
    "gpt-4o-mini-transcribe": 0.003,
    "gpt-4o-transcribe": 0.006,
    "whisper-1": 0.006,
}

bot = Bot(token=TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None
dp = Dispatcher()
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY and AsyncOpenAI else None

