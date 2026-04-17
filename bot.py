import asyncio
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
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


@dataclass
class ParsedIntent:
    action: str
    title: Optional[str] = None
    kind: str = "event"
    starts_at: Optional[str] = None
    reminders: Optional[List[int]] = None
    repeat_rule: Optional[str] = None
    assumptions: Optional[List[str]] = None
    needs_time_question: bool = False
    original_text: str = ""


@dataclass
class CommandIntent:
    action: str
    confidence: float = 0
    minutes: Optional[int] = None
    starts_at: Optional[str] = None
    question: Optional[str] = None
    assumptions: Optional[List[str]] = None


def now() -> datetime:
    return datetime.now(BOT_TIMEZONE).replace(second=0, microsecond=0)


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(BOT_TIMEZONE)


def fmt_dt(value: Optional[str]) -> str:
    dt = parse_dt(value)
    if not dt:
        return "без времени"
    return dt.strftime("%d.%m %H:%M")


def scope_window(scope: str) -> tuple[datetime, datetime]:
    current = now()
    start = current.replace(hour=0, minute=0)
    days = {"today": 1, "week": 7, "month": 31, "all": 3650}.get(scope, 1)
    return start, start + timedelta(days=days)


def owner_allowed(message: Message) -> bool:
    return not BOT_OWNER_ID or str(message.from_user.id) == BOT_OWNER_ID


def rough_token_count(text: str) -> int:
    return max(1, int(len(text) / 4))


def text_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    pricing = TEXT_MODEL_PRICING.get(model, TEXT_MODEL_PRICING["gpt-5.4-nano"])
    return (input_tokens / 1_000_000) * pricing["input"] + (output_tokens / 1_000_000) * pricing["output"]


def transcription_cost_usd(model: str, seconds: float) -> float:
    per_minute = TRANSCRIBE_PRICING_PER_MINUTE.get(model, TRANSCRIBE_PRICING_PER_MINUTE["gpt-4o-mini-transcribe"])
    return (seconds / 60) * per_minute


def month_start_iso() -> str:
    current = now()
    return current.replace(day=1, hour=0, minute=0).isoformat()


async def month_api_spend(user_id: int) -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (
            await db.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM api_usage WHERE user_id=? AND created_at >= ?",
                (user_id, month_start_iso()),
            )
        ).fetchone()
    return float(row[0] or 0)


async def api_budget_available(user_id: int) -> bool:
    if API_MONTHLY_LIMIT_USD <= 0:
        return True
    return await month_api_spend(user_id) < API_MONTHLY_LIMIT_USD


async def record_api_usage(
    user_id: int,
    kind: str,
    model: str,
    cost_usd: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    audio_seconds: float = 0,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO api_usage(user_id, kind, model, input_tokens, output_tokens, audio_seconds, cost_usd, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, kind, model, input_tokens, output_tokens, audio_seconds, cost_usd, now().isoformat()),
        )
        await db.commit()


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'event',
                starts_at TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                reminders_json TEXT NOT NULL DEFAULT '[60, 30]',
                sent_reminders_json TEXT NOT NULL DEFAULT '[]',
                seen INTEGER NOT NULL DEFAULT 0,
                departed INTEGER NOT NULL DEFAULT 0,
                confirmed INTEGER NOT NULL DEFAULT 0,
                done INTEGER NOT NULL DEFAULT 0,
                last_ping_at TEXT,
                next_ping_at TEXT,
                repeat_rule TEXT,
                raw_text TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_questions (
                user_id INTEGER PRIMARY KEY,
                payload_json TEXT NOT NULL,
                question TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS api_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                audio_seconds REAL NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        await db.commit()


def event_keyboard(event_id: int, phase: str, kind: str) -> InlineKeyboardMarkup:
    if kind == "task":
        rows = [
            [InlineKeyboardButton(text="Готово", callback_data=f"done:{event_id}")],
            [
                InlineKeyboardButton(text="Отложить", callback_data=f"snooze:{event_id}"),
                InlineKeyboardButton(text="Отменить", callback_data=f"cancel:{event_id}"),
            ],
        ]
        return InlineKeyboardMarkup(inline_keyboard=rows)

    if phase == "started":
        rows = [
            [
                InlineKeyboardButton(text="Я тут", callback_data=f"arrived:{event_id}"),
                InlineKeyboardButton(text="Готово", callback_data=f"done:{event_id}"),
            ],
            [
                InlineKeyboardButton(text="Опаздываю", callback_data=f"late:{event_id}"),
                InlineKeyboardButton(text="Отложить", callback_data=f"snooze:{event_id}"),
            ],
            [InlineKeyboardButton(text="Отменить", callback_data=f"cancel:{event_id}")],
        ]
    else:
        rows = [
            [
                InlineKeyboardButton(text="Вижу", callback_data=f"seen:{event_id}"),
                InlineKeyboardButton(text="Выдвигаюсь", callback_data=f"departed:{event_id}"),
            ],
            [InlineKeyboardButton(text="Готово", callback_data=f"done:{event_id}")],
            [
                InlineKeyboardButton(text="Отложить", callback_data=f"snooze:{event_id}"),
                InlineKeyboardButton(text="Изменить", callback_data=f"edit:{event_id}"),
            ],
            [InlineKeyboardButton(text="Отменить", callback_data=f"cancel:{event_id}")],
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def snooze_keyboard(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="+5 мин", callback_data=f"snooze_set:{event_id}:5"),
                InlineKeyboardButton(text="+10 мин", callback_data=f"snooze_set:{event_id}:10"),
            ],
            [
                InlineKeyboardButton(text="+15 мин", callback_data=f"snooze_set:{event_id}:15"),
                InlineKeyboardButton(text="+30 мин", callback_data=f"snooze_set:{event_id}:30"),
            ],
            [
                InlineKeyboardButton(text="+1 час", callback_data=f"snooze_set:{event_id}:60"),
                InlineKeyboardButton(text="Сегодня вечером", callback_data=f"snooze_at:{event_id}:evening"),
            ],
            [
                InlineKeyboardButton(text="Завтра утром", callback_data=f"snooze_at:{event_id}:tomorrow_morning"),
                InlineKeyboardButton(text="Завтра", callback_data=f"snooze_set:{event_id}:1440"),
            ],
        ]
    )


def confirm_delete_keyboard(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, удалить", callback_data=f"confirm_cancel:{event_id}"),
                InlineKeyboardButton(text="Нет", callback_data=f"open:{event_id}"),
            ]
        ]
    )


def calendar_keyboard(rows) -> InlineKeyboardMarkup:
    buttons = []
    for event_id, title, kind, starts_at, _status in rows:
        marker = "задача" if kind == "task" else "событие"
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{fmt_dt(starts_at)} · {marker} · {title}"[:64],
                    callback_data=f"open:{event_id}",
                )
            ]
        )
    buttons.append(
        [
            InlineKeyboardButton(text="Сегодня", callback_data="list:today"),
            InlineKeyboardButton(text="Неделя", callback_data="list:week"),
            InlineKeyboardButton(text="Месяц", callback_data="list:month"),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(text="Что горит", callback_data="list:hot"),
            InlineKeyboardButton(text="Все активные", callback_data="list:all"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def manage_keyboard(event_id: int, kind: str, phase: str = "before") -> InlineKeyboardMarkup:
    if kind == "task":
        rows = [
            [InlineKeyboardButton(text="Готово", callback_data=f"done:{event_id}")],
            [
                InlineKeyboardButton(text="Отложить", callback_data=f"snooze:{event_id}"),
                InlineKeyboardButton(text="Удалить", callback_data=f"cancel:{event_id}"),
            ],
        ]
    elif phase == "started":
        rows = [
            [
                InlineKeyboardButton(text="Я тут", callback_data=f"arrived:{event_id}"),
                InlineKeyboardButton(text="Готово", callback_data=f"done:{event_id}"),
            ],
            [
                InlineKeyboardButton(text="Опаздываю", callback_data=f"late:{event_id}"),
                InlineKeyboardButton(text="Отложить", callback_data=f"snooze:{event_id}"),
            ],
            [InlineKeyboardButton(text="Удалить", callback_data=f"cancel:{event_id}")],
        ]
    else:
        rows = [
            [
                InlineKeyboardButton(text="Вижу", callback_data=f"seen:{event_id}"),
                InlineKeyboardButton(text="Выдвигаюсь", callback_data=f"departed:{event_id}"),
            ],
            [InlineKeyboardButton(text="Готово", callback_data=f"done:{event_id}")],
            [
                InlineKeyboardButton(text="Отложить", callback_data=f"snooze:{event_id}"),
                InlineKeyboardButton(text="Изменить", callback_data=f"edit:{event_id}"),
            ],
            [InlineKeyboardButton(text="Удалить", callback_data=f"cancel:{event_id}")],
        ]
    rows.append([InlineKeyboardButton(text="Назад к списку", callback_data="list:all")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def has_time_context(text: str) -> bool:
    lower = text.lower()
    return bool(
        re.search(
            r"\b(примерно|около|районе|к|в)\s+(?:\d{1,2})(?::|\.)?(?:\d{2})?\b",
            lower,
        )
    )


def has_vague_time(text: str) -> bool:
    lower = text.lower()
    return any(
        marker in lower
        for marker in ["примерно", "около", "в районе", "где-то", "гдето", "примерно около", "после"]
    )


def default_reminders(text: str) -> List[int]:
    matches = re.findall(r"за\s+(\d+)\s*(минут|мин|час|часа|часов)", text.lower())
    reminders: List[int] = []
    for amount, unit in matches:
        value = int(amount)
        reminders.append(value * 60 if unit.startswith("час") else value)
    if reminders:
        return sorted(set(reminders), reverse=True)
    if has_vague_time(text):
        return [60, 30, 15, 10]
    return [60, 30]


def extract_time(lower: str) -> tuple[Optional[int], Optional[int], bool]:
    time_match = re.search(
        r"\b(?:(?:примерно|около|после|в\s+районе|районе|где-то|гдето|к|в|на)\s+){1,3}(\d{1,2})(?:(?::|\.|\s+)(\d{2}))?\b",
        lower,
    )
    if not time_match:
        return None, None, False
    hour = int(time_match.group(1))
    minute = int(time_match.group(2) or 0)
    if hour > 23 or minute > 59:
        return None, None, False
    vague = any(marker in time_match.group(0) for marker in ["примерно", "около", "после", "районе", "где-то", "гдето"])
    return hour, minute, vague


def extract_bare_time(lower: str) -> tuple[Optional[int], Optional[int], bool]:
    time_match = re.search(r"\b(\d{1,2})(?:(?::|\.|\s+)(\d{2}))?\b", lower)
    if not time_match:
        return None, None, False
    hour = int(time_match.group(1))
    minute = int(time_match.group(2) or 0)
    if hour > 23 or minute > 59:
        return None, None, False
    vague = any(marker in lower for marker in ["после", "примерно", "около", "районе", "где-то", "гдето"])
    return hour, minute, vague


def clean_title(text: str) -> str:
    title = re.sub(
        r"\b(сегодня|завтра|послезавтра|напомни|за\s+\d+\s*(минут|мин|час|часа|часов))\b",
        "",
        text,
        flags=re.I,
    )
    parts = [part.strip(" ,.") for part in re.split(r"[,;]", title) if part.strip(" ,.")]
    if len(parts) > 1:
        non_time_parts = [part for part in parts if not has_time_context(part)]
        if non_time_parts:
            title = non_time_parts[0]
    title = re.sub(
        r"\b(примерно|около|после|в\s+районе|районе|где-то|гдето|к|в|на)\s+\d{1,2}(?:(?::|\.|\s+)\d{2})?\b",
        "",
        title,
        flags=re.I,
    )
    return re.sub(r"\s+", " ", title).strip(" ,.")


def fallback_parse(text: str) -> ParsedIntent:
    lower = text.lower().strip()
    assumptions: List[str] = []
    if any(word in lower for word in ["удали", "удалить", "отмени", "отменить"]):
        title = re.sub(r"^(удали|удалить|отмени|отменить)\s+", "", lower).strip()
        return ParsedIntent(action="delete", title=title or None, original_text=text)

    if any(phrase in lower for phrase in ["что у меня сегодня", "покажи сегодня", "события сегодня"]):
        return ParsedIntent(action="list", title="today", original_text=text)
    if any(phrase in lower for phrase in ["что у меня на неделю", "покажи неделю", "покажи на неделю", "события на неделю"]):
        return ParsedIntent(action="list", title="week", original_text=text)
    if any(phrase in lower for phrase in ["что у меня на месяц", "покажи месяц", "события на месяц"]):
        return ParsedIntent(action="list", title="month", original_text=text)
    if any(phrase in lower for phrase in ["что горит", "горит", "важное сейчас", "срочное", "что срочно"]):
        return ParsedIntent(action="list", title="hot", original_text=text)
    if any(phrase in lower for phrase in ["список", "покажи все", "все события", "управление", "что у меня"]):
        return ParsedIntent(action="list", title="all", original_text=text)

    base = now()
    starts_at = None
    date_was_given = False
    if "завтра" in lower:
        base = base + timedelta(days=1)
        date_was_given = True
    elif "послезавтра" in lower:
        base = base + timedelta(days=2)
        date_was_given = True
    elif "сегодня" in lower:
        date_was_given = True

    hour, minute, vague_time = extract_time(lower)
    if hour is not None and minute is not None:
        candidate = base.replace(hour=hour, minute=minute)
        if not date_was_given:
            if candidate < now() - timedelta(minutes=15):
                candidate = candidate + timedelta(days=1)
                assumptions.append("дату не указал, время уже прошло — поставил на завтра")
            else:
                assumptions.append("дату не указал — считаю, что это сегодня")
        if vague_time:
            assumptions.append("время звучит примерным — поставил частые напоминания")
        starts_at = candidate.isoformat()

    soft_task = any(word in lower for word in ["на неделе", "купить", "сделать", "задача"])
    kind = "task" if soft_task and not starts_at else "event"
    needs_time = kind == "task" and starts_at is None
    title = clean_title(text)
    return ParsedIntent(
        action="create",
        title=title or text,
        kind=kind,
        starts_at=starts_at,
        reminders=default_reminders(text),
        assumptions=assumptions,
        needs_time_question=needs_time,
        original_text=text,
    )


async def ai_parse(text: str, user_id: int = 0) -> ParsedIntent:
    if not openai_client:
        return fallback_parse(text)
    if not await api_budget_available(user_id):
        return fallback_parse(text)

    system = f"""
Ты парсер личного Telegram-бота напоминаний. Сегодня {now().strftime('%Y-%m-%d %H:%M')}, часовой пояс Asia/Novosibirsk.
Верни только JSON:
{{
  "action": "create|delete|list|unknown",
  "title": "короткое название или null",
  "kind": "event|task",
  "starts_at": "ISO datetime with timezone или null",
  "reminders": [минуты до события, например 60,30],
  "repeat_rule": "человеческое описание повтора или null",
  "assumptions": ["короткие объяснения твоих догадок"],
  "needs_time_question": true|false
}}
Если задача без времени, поставь kind=task, starts_at=null, needs_time_question=true.
Если есть примерное время ("около 17", "после 14", "ближе к вечеру", "после обеда"), НЕ спрашивай точное время:
создай событие на разумное рабочее время, поставь starts_at и добавь assumptions.
Для "около 17" ставь 17:00 сегодня, если время еще впереди, иначе завтра.
Для "после 14" ставь 14:00 сегодня, если время еще впереди, иначе завтра.
Для "ближе к вечеру" ставь 18:00 сегодня, если время еще впереди, иначе завтра.
Для "после обеда" ставь 14:00 сегодня, если время еще впереди, иначе завтра.
Если время примерное, reminders=[60,30,15,10].
Спрашивай уточнение только если вообще нет даты/времени или невозможно сделать полезную догадку.
"""
    try:
        response = await openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
        )
        usage = response.usage
        input_tokens = getattr(usage, "prompt_tokens", 0) if usage else rough_token_count(system + text)
        output_tokens = getattr(usage, "completion_tokens", 0) if usage else rough_token_count(response.choices[0].message.content or "")
        await record_api_usage(
            user_id=user_id,
            kind="text_parse",
            model=OPENAI_MODEL,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=text_cost_usd(OPENAI_MODEL, input_tokens, output_tokens),
        )
        payload = json.loads(response.choices[0].message.content or "{}")
        return ParsedIntent(
            action=payload.get("action", "unknown"),
            title=payload.get("title"),
            kind=payload.get("kind") or "event",
            starts_at=payload.get("starts_at"),
            reminders=payload.get("reminders") or default_reminders(text),
            repeat_rule=payload.get("repeat_rule"),
            assumptions=payload.get("assumptions") or [],
            needs_time_question=bool(payload.get("needs_time_question")),
            original_text=text,
        )
    except Exception:
        return fallback_parse(text)


async def transcribe_voice(message: Message) -> Optional[str]:
    if not openai_client:
        await message.answer(
            "Голос я очень хочу понимать, но мне нужен OPENAI_API_KEY. Пока можешь писать текстом."
        )
        return None
    if not await api_budget_available(message.from_user.id):
        spend = await month_api_spend(message.from_user.id)
        await message.answer(
            f"Бро, месячный API-лимит уже около ${spend:.2f}. Голос пока выключаю, текстом работаю локально."
        )
        return None

    voice = message.voice
    if not voice:
        return None
    file = await bot.get_file(voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg") as temp:
        await bot.download_file(file.file_path, temp.name)
        with open(temp.name, "rb") as audio:
            result = await openai_client.audio.transcriptions.create(
                model=OPENAI_TRANSCRIBE_MODEL,
                file=audio,
                language="ru",
            )
    seconds = float(voice.duration or 0)
    await record_api_usage(
        user_id=message.from_user.id,
        kind="transcription",
        model=OPENAI_TRANSCRIBE_MODEL,
        audio_seconds=seconds,
        cost_usd=transcription_cost_usd(OPENAI_TRANSCRIBE_MODEL, seconds),
    )
    return result.text


async def save_pending_question(user_id: int, parsed: ParsedIntent, question: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO pending_questions(user_id, payload_json, question, created_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                payload_json=excluded.payload_json,
                question=excluded.question,
                created_at=excluded.created_at
            """,
            (user_id, json.dumps(parsed.__dict__, ensure_ascii=False), question, now().isoformat()),
        )
        await db.commit()


async def pop_pending_question(user_id: int) -> Optional[ParsedIntent]:
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT payload_json FROM pending_questions WHERE user_id=?", (user_id,))).fetchone()
        if not row:
            return None
        await db.execute("DELETE FROM pending_questions WHERE user_id=?", (user_id,))
        await db.commit()
    return ParsedIntent(**json.loads(row[0]))


async def create_event(user_id: int, parsed: ParsedIntent) -> int:
    created = now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO events(user_id, title, kind, starts_at, reminders_json, repeat_rule, raw_text, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                parsed.title or "Без названия",
                parsed.kind,
                parsed.starts_at,
                json.dumps(parsed.reminders or [60, 30]),
                parsed.repeat_rule,
                parsed.original_text,
                created,
                created,
            ),
        )
        await db.commit()
        return int(cursor.lastrowid)


async def finish_create(message: Message, parsed: ParsedIntent) -> None:
    event_id = await create_event(message.from_user.id, parsed)
    if parsed.kind == "task":
        text = f"Записал задачу: {parsed.title}\nПинги: утром каждый день, пока не нажмешь «Готово»."
        await message.answer(text, reply_markup=event_keyboard(event_id, "before", "task"))
    else:
        text = f"Записал: {parsed.title}\nКогда: {fmt_dt(parsed.starts_at)}\nНапомню: {', '.join(str(x) + ' мин' for x in (parsed.reminders or [60, 30]))}."
        if parsed.assumptions:
            text += "\nЯ предположил: " + "; ".join(parsed.assumptions) + "."
        await message.answer(text, reply_markup=event_keyboard(event_id, "before", "event"))


async def find_focus_event(user_id: int):
    current = now()
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (
            await db.execute(
                """
                SELECT id, title, kind, starts_at, reminders_json, sent_reminders_json,
                       seen, departed, confirmed, done
                FROM events
                WHERE user_id=? AND status='active'
                ORDER BY
                  CASE
                    WHEN kind='event' AND starts_at IS NOT NULL AND starts_at <= ? AND confirmed=0 THEN 0
                    WHEN kind='event' AND starts_at IS NOT NULL AND starts_at > ? THEN 1
                    WHEN kind='task' AND done=0 THEN 2
                    ELSE 3
                  END,
                  starts_at IS NULL,
                  starts_at
                LIMIT 1
                """,
                (user_id, current.isoformat(), current.isoformat()),
            )
        ).fetchone()
    return rows


async def mark_event_seen(event_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE events SET seen=1, updated_at=? WHERE id=?", (now().isoformat(), event_id))
        await db.commit()


async def mark_event_departed(event_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE events SET departed=1, updated_at=? WHERE id=?", (now().isoformat(), event_id))
        await db.commit()


async def mark_event_arrived(event_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE events SET confirmed=1, status='confirmed', updated_at=? WHERE id=?",
            (now().isoformat(), event_id),
        )
        await db.commit()


async def mark_task_done(event_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE events SET done=1, status='done', updated_at=? WHERE id=?",
            (now().isoformat(), event_id),
        )
        await db.commit()


async def snooze_event_to(event_id: int, new_start: datetime) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE events SET starts_at=?, sent_reminders_json='[]', seen=0, departed=0,
                confirmed=0, next_ping_at=NULL, updated_at=? WHERE id=?
            """,
            (new_start.isoformat(), now().isoformat(), event_id),
        )
        await db.commit()


def named_snooze_time(kind: str) -> datetime:
    current = now()
    if kind == "evening":
        evening = current.replace(hour=19, minute=0)
        if evening <= current:
            evening = evening + timedelta(days=1)
        return evening
    if kind == "tomorrow_morning":
        return (current + timedelta(days=1)).replace(hour=9, minute=0)
    return current + timedelta(minutes=15)


def time_from_text(text: str) -> Optional[datetime]:
    lower = text.lower()
    hour, minute, _vague = extract_time(lower)
    if hour is None or minute is None:
        hour, minute, _vague = extract_bare_time(lower)
    if hour is None or minute is None:
        return None
    candidate = now().replace(hour=hour, minute=minute)
    if "завтра" in lower:
        candidate = candidate + timedelta(days=1)
    elif candidate < now() - timedelta(minutes=15):
        candidate = candidate + timedelta(days=1)
    return candidate


def local_command_intent(text: str) -> Optional[CommandIntent]:
    lower = text.lower().strip(" .,!?:;")
    done_words = [
        "выполнена",
        "выполнено",
        "выполнил",
        "завершить",
        "заверши",
        "закрыть",
        "закрой",
        "готово",
        "сделал",
        "задача готова",
        "можешь завершить",
    ]
    if any(word in lower for word in done_words):
        return CommandIntent(action="done", confidence=0.95, assumptions=["понял как команду закрыть текущую задачу или событие"])

    if any(phrase in lower for phrase in ["отмени это", "удали это", "удалить задачу", "удали задачу"]):
        return CommandIntent(action="delete", confidence=0.95)

    if any(phrase in lower for phrase in ["я на месте", "на месте", "я тут", "приехал", "пришел"]):
        return CommandIntent(action="arrived", confidence=0.95)

    if any(phrase in lower for phrase in ["вижу", "понял", "принял", "увидел"]):
        return CommandIntent(action="seen", confidence=0.9)

    if any(phrase in lower for phrase in ["выезжаю", "выехал", "еду", "выдвигаюсь"]):
        return CommandIntent(action="departed", confidence=0.9)

    if lower.startswith(("перенеси", "перенести", "отложи", "отложить")):
        minutes_match = re.search(r"на\s+(\d+)\s*(минут|мин|час|часа|часов)", lower)
        if minutes_match:
            value = int(minutes_match.group(1))
            minutes = value * 60 if minutes_match.group(2).startswith("час") else value
            return CommandIntent(action="snooze", confidence=0.9, minutes=minutes)
        new_start = time_from_text(lower)
        if new_start:
            return CommandIntent(action="reschedule", confidence=0.9, starts_at=new_start.isoformat())

    return None


async def ai_command_intent(text: str, focus_title: str, user_id: int) -> Optional[CommandIntent]:
    if not openai_client or not await api_budget_available(user_id):
        return None

    system = f"""
Ты классификатор команд для Telegram-бота напоминаний.
Сейчас пользователь может говорить либо новую задачу, либо команду к текущему событию.
Текущее событие: {focus_title}
Сегодня {now().strftime('%Y-%m-%d %H:%M')}, часовой пояс Asia/Novosibirsk.

Верни только JSON:
{{
  "action": "done|delete|seen|departed|arrived|snooze|reschedule|ask|new",
  "confidence": 0.0,
  "minutes": null,
  "starts_at": null,
  "question": null,
  "assumptions": []
}}

Правила:
- Если пользователь говорит, что "эта задача выполнена", "можешь завершить", "закрой", это action=done.
- Если просит "удали/отмени это", action=delete.
- Если "вижу/понял", action=seen.
- Если "выезжаю/еду", action=departed.
- Если "я тут/на месте/приехал", action=arrived.
- Если просит перенести на относительное время, action=snooze и minutes.
- Если просит перенести на конкретное время, action=reschedule и starts_at ISO.
- Если непонятно, но похоже на команду к текущему событию, action=ask и короткий question.
- Если это явно новая задача/событие, action=new.
"""
    try:
        response = await openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        usage = response.usage
        input_tokens = getattr(usage, "prompt_tokens", 0) if usage else rough_token_count(system + text)
        output_tokens = getattr(usage, "completion_tokens", 0) if usage else rough_token_count(response.choices[0].message.content or "")
        await record_api_usage(
            user_id=user_id,
            kind="command_parse",
            model=OPENAI_MODEL,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=text_cost_usd(OPENAI_MODEL, input_tokens, output_tokens),
        )
        payload = json.loads(response.choices[0].message.content or "{}")
        return CommandIntent(
            action=payload.get("action", "new"),
            confidence=float(payload.get("confidence") or 0),
            minutes=payload.get("minutes"),
            starts_at=payload.get("starts_at"),
            question=payload.get("question"),
            assumptions=payload.get("assumptions") or [],
        )
    except Exception:
        return None


async def apply_command_intent(message: Message, focus, intent: CommandIntent) -> bool:
    event_id, title, kind, starts_at, _reminders, _sent, _seen, _departed, _confirmed, _done = focus
    if intent.action == "new":
        return False
    if intent.action == "ask":
        await message.answer(intent.question or "Ты про текущее событие или это новая задача?")
        return True
    if intent.action == "done":
        if kind == "task":
            await mark_task_done(event_id)
            await message.answer(f"Готово, закрыл задачу: {title}.")
        else:
            await mark_event_arrived(event_id)
            await message.answer(f"Готово, закрыл событие: {title}. Больше по нему не пингую.")
        return True
    if intent.action == "delete":
        await message.answer(f"Удаляем «{title}»?", reply_markup=confirm_delete_keyboard(event_id))
        return True
    if intent.action == "seen":
        await mark_event_seen(event_id)
        await message.answer(f"Принял по «{title}»: ты видел.")
        return True
    if intent.action == "departed":
        await mark_event_departed(event_id)
        await message.answer(f"Отметил по «{title}»: ты выдвинулся. До старта не душню.")
        return True
    if intent.action == "arrived":
        await mark_event_arrived(event_id)
        await message.answer(f"Красавчик, зафиксировал: ты на месте по «{title}».")
        return True
    if intent.action == "snooze" and intent.minutes:
        new_start = (parse_dt(starts_at) or now()) + timedelta(minutes=int(intent.minutes))
        await snooze_event_to(event_id, new_start)
        await message.answer(f"Отложил «{title}» на {intent.minutes} мин. Новое время: {new_start.strftime('%d.%m %H:%M')}.")
        return True
    if intent.action == "reschedule" and intent.starts_at:
        new_start = parse_dt(intent.starts_at)
        if new_start:
            await snooze_event_to(event_id, new_start)
            await message.answer(f"Перенес «{title}» на {new_start.strftime('%d.%m %H:%M')}.")
            return True
    return False


def merge_pending_with_parsed(pending: ParsedIntent, parsed: ParsedIntent) -> ParsedIntent:
    if not parsed.title or parsed.title.lower().strip() in {"после", "в", "к", "на"}:
        parsed.title = pending.title
    if not parsed.original_text:
        parsed.original_text = pending.original_text
    parsed.assumptions = (pending.assumptions or []) + (parsed.assumptions or [])
    return parsed


def apply_time_to_pending(pending: ParsedIntent, text: str) -> Optional[ParsedIntent]:
    lower = text.lower()
    hour, minute, vague = extract_time(lower)
    if hour is None or minute is None:
        hour, minute, vague = extract_bare_time(lower)
    if hour is None or minute is None:
        return None

    candidate = now().replace(hour=hour, minute=minute)
    assumptions = list(pending.assumptions or [])
    if "завтра" in lower:
        candidate = candidate + timedelta(days=1)
    elif candidate < now() - timedelta(minutes=15):
        candidate = candidate + timedelta(days=1)
        assumptions.append("уточнил время, но оно уже прошло — поставил на завтра")
    else:
        assumptions.append("уточнил время — считаю, что это сегодня")

    if vague:
        assumptions.append("уточнение звучит примерным — поставил частые напоминания")

    pending.starts_at = candidate.isoformat()
    pending.kind = "event"
    pending.needs_time_question = False
    pending.reminders = [60, 30, 15, 10] if vague else (pending.reminders or [60, 30])
    pending.assumptions = assumptions
    return pending


async def handle_quick_reply(message: Message, text: str) -> bool:
    lower = text.lower().strip(" .,!?:;")
    if lower in {"что горит", "горит", "срочное", "что срочно", "важное сейчас"}:
        await send_calendar(message, "hot")
        return True

    focus = await find_focus_event(message.from_user.id)
    if not focus:
        return False

    local_intent = local_command_intent(text)
    if local_intent and await apply_command_intent(message, focus, local_intent):
        return True

    event_id, title, _kind, _starts_at, _reminders, _sent, _seen, _departed, _confirmed, _done = focus
    commandish = any(
        marker in lower
        for marker in [
            "эта",
            "это",
            "эту",
            "ее",
            "её",
            "его",
            "задач",
            "событ",
            "заверш",
            "закр",
            "удал",
            "отмен",
            "перенес",
            "отлож",
            "выполн",
        ]
    )
    if commandish:
        ai_intent = await ai_command_intent(text, title, message.from_user.id)
        if ai_intent and ai_intent.confidence >= 0.55:
            return await apply_command_intent(message, focus, ai_intent)
        if ai_intent and ai_intent.action == "ask":
            await message.answer(ai_intent.question or "Я не до конца понял: это команда к текущему событию?")
            return True
        await message.answer(
            f"Я не до конца понял. Ты хочешь что-то сделать с «{title}»?",
            reply_markup=manage_keyboard(event_id, _kind, "before"),
        )
        return True

    return False


async def handle_text(message: Message, text: str) -> None:
    if not owner_allowed(message):
        await message.answer("Этот бот пока личный. Я чужих не трогаю.")
        return

    if await handle_quick_reply(message, text):
        return

    pending = await pop_pending_question(message.from_user.id)
    if pending:
        parsed = apply_time_to_pending(pending, text)
        if not parsed:
            combined = f"{pending.original_text}. Уточнение пользователя: {text}"
            parsed = merge_pending_with_parsed(pending, await ai_parse(combined, message.from_user.id))
    else:
        parsed = await ai_parse(text, message.from_user.id)

    if parsed.action == "list":
        await send_calendar(message, parsed.title or "today")
        return

    if parsed.action == "delete":
        await ask_delete(message, parsed.title or text)
        return

    if parsed.action != "create":
        await message.answer("Я не до конца понял. Скажи проще: что, когда и как часто напоминать?")
        return

    if parsed.needs_time_question and not parsed.starts_at:
        question = "Когда тебя пинать по этой задаче? Могу утром каждый день, пока не нажмешь «Готово»."
        await save_pending_question(message.from_user.id, parsed, question)
        await message.answer(question)
        return

    if parsed.kind == "event" and not parsed.starts_at:
        question = "Во сколько это? Дай дату/время, и я запишу."
        await save_pending_question(message.from_user.id, parsed, question)
        await message.answer(question)
        return

    await finish_create(message, parsed)


async def ask_delete(message: Message, query: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (
            await db.execute(
                """
                SELECT id, title, starts_at, kind FROM events
                WHERE user_id=? AND status='active' AND lower(title) LIKE ?
                ORDER BY starts_at IS NULL, starts_at
                LIMIT 8
                """,
                (message.from_user.id, f"%{query.lower()}%"),
            )
        ).fetchall()
    if not rows:
        await message.answer("Не нашел похожее событие. Скажи название чуть точнее.")
        return
    if len(rows) == 1:
        event_id, title, _starts_at, _kind = rows[0]
        await message.answer(f"Удаляем «{title}»?", reply_markup=confirm_delete_keyboard(event_id))
        return
    buttons = [[InlineKeyboardButton(text=f"{title} · {fmt_dt(starts_at)}", callback_data=f"open:{event_id}")]
               for event_id, title, starts_at, _kind in rows]
    await message.answer("Нашел несколько похожих. Какое открыть для удаления?", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


async def cancel_event(event_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE events SET status='cancelled', updated_at=? WHERE id=?",
            (now().isoformat(), event_id),
        )
        await db.commit()


def hot_score(row) -> int:
    _id, _title, kind, starts_at_raw, _status, seen, _departed, confirmed, done = row
    if kind == "task":
        return 20 if not done else 1000
    starts_at = parse_dt(starts_at_raw)
    if not starts_at or confirmed:
        return 1000
    delta_minutes = int((starts_at - now()).total_seconds() // 60)
    if delta_minutes <= 0:
        return 0
    if delta_minutes <= 120 and not seen:
        return 1
    if delta_minutes <= 120:
        return 2
    return 100 + delta_minutes


async def fetch_calendar_rows(user_id: int, scope: str):
    if scope == "hot":
        horizon = now() + timedelta(hours=6)
        async with aiosqlite.connect(DB_PATH) as db:
            rows = await (
                await db.execute(
                    """
                    SELECT id, title, kind, starts_at, status, seen, departed, confirmed, done
                    FROM events
                    WHERE user_id=? AND status='active'
                      AND (
                        kind='task'
                        OR starts_at IS NULL
                        OR starts_at <= ?
                      )
                    LIMIT 80
                    """,
                    (user_id, horizon.isoformat()),
                )
            ).fetchall()
        rows = [row for row in rows if hot_score(row) < 1000]
        rows.sort(key=hot_score)
        return [(event_id, title, kind, starts_at, status) for event_id, title, kind, starts_at, status, *_rest in rows[:20]]

    start, end = scope_window(scope)
    async with aiosqlite.connect(DB_PATH) as db:
        if scope == "all":
            return await (
                await db.execute(
                    """
                    SELECT id, title, kind, starts_at, status FROM events
                    WHERE user_id=? AND status='active'
                    ORDER BY starts_at IS NULL, starts_at
                    LIMIT 50
                    """,
                    (user_id,),
                )
            ).fetchall()
        return await (
            await db.execute(
                """
                SELECT id, title, kind, starts_at, status FROM events
                WHERE user_id=? AND status='active'
                  AND (starts_at IS NULL OR starts_at BETWEEN ? AND ?)
                ORDER BY starts_at IS NULL, starts_at
                LIMIT 50
                """,
                (user_id, start.isoformat(), end.isoformat()),
            )
        ).fetchall()


async def send_calendar(message: Message, scope: str) -> None:
    rows = await fetch_calendar_rows(message.from_user.id, scope)
    if not rows:
        await message.answer("Пусто. Приятная редкость.")
        return
    lines = ["Что горит:" if scope == "hot" else "Вот что вижу:"]
    for _id, title, kind, starts_at, _status in rows:
        marker = "задача" if kind == "task" else "событие"
        lines.append(f"• {fmt_dt(starts_at)} · {marker} · {title}")
    lines.append("\nТыкни на событие ниже, и я дам кнопки управления.")
    await message.answer("\n".join(lines), reply_markup=calendar_keyboard(rows))


async def send_calendar_to_chat(chat_id: int, user_id: int, scope: str) -> None:
    rows = await fetch_calendar_rows(user_id, scope)
    if not rows:
        await bot.send_message(chat_id, "Пусто. Приятная редкость.")
        return
    lines = ["Что горит:" if scope == "hot" else "Вот список для управления:"]
    for _id, title, kind, starts_at, _status in rows:
        marker = "задача" if kind == "task" else "событие"
        lines.append(f"• {fmt_dt(starts_at)} · {marker} · {title}")
    await bot.send_message(chat_id, "\n".join(lines), reply_markup=calendar_keyboard(rows))


async def send_event_details(chat_id: int, event_id: int) -> None:
    row = await get_event(event_id)
    if not row:
        await bot.send_message(chat_id, "Не нашел событие.")
        return
    _id, _user_id, title, kind, starts_at, reminders_raw, _sent_raw, seen, departed, confirmed, done = row
    reminders = ", ".join(str(x) + " мин" for x in json.loads(reminders_raw))
    status_bits = []
    if seen:
        status_bits.append("видел")
    if departed:
        status_bits.append("выдвинулся")
    if confirmed:
        status_bits.append("на месте")
    if done:
        status_bits.append("готово")
    phase = "started" if starts_at and parse_dt(starts_at) and now() >= parse_dt(starts_at) else "before"
    text = (
        f"{title}\n"
        f"Тип: {'задача' if kind == 'task' else 'событие'}\n"
        f"Когда: {fmt_dt(starts_at)}\n"
        f"Напоминания: {reminders}\n"
        f"Статус: {', '.join(status_bits) if status_bits else 'активно'}"
    )
    await bot.send_message(chat_id, text, reply_markup=manage_keyboard(event_id, kind, phase))


async def send_api_stats(message: Message) -> None:
    spend = await month_api_spend(message.from_user.id)
    left = max(0, API_MONTHLY_LIMIT_USD - spend)
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (
            await db.execute(
                """
                SELECT kind, COUNT(*), COALESCE(SUM(cost_usd), 0)
                FROM api_usage
                WHERE user_id=? AND created_at >= ?
                GROUP BY kind
                ORDER BY kind
                """,
                (message.from_user.id, month_start_iso()),
            )
        ).fetchall()
    lines = [
        f"API за месяц: примерно ${spend:.4f} из ${API_MONTHLY_LIMIT_USD:.2f}.",
        f"Осталось: примерно ${left:.4f}.",
    ]
    if rows:
        for kind, count, cost in rows:
            label = "голос" if kind == "transcription" else "понимание текста"
            lines.append(f"• {label}: {count} шт, около ${float(cost):.4f}")
    else:
        lines.append("Пока API не тратился.")
    await message.answer("\n".join(lines))


async def get_event(event_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        return await (
            await db.execute(
                "SELECT id, user_id, title, kind, starts_at, reminders_json, sent_reminders_json, seen, departed, confirmed, done FROM events WHERE id=?",
                (event_id,),
            )
        ).fetchone()


@dp.message(Command("start"))
async def start(message: Message) -> None:
    if not owner_allowed(message):
        await message.answer("Этот бот пока личный.")
        return
    await message.answer(
        "Я на связи. Кидай текстом или голосом: событие, время и как напоминать.\n"
        "Например: «Стоматолог завтра в 10, напомни за час и за 30 минут».\n"
        "Чтобы управлять событиями кнопками, напиши /list или «список».\n"
        "Чтобы увидеть только срочное, напиши /hot или «что горит».\n"
        "Чтобы проверить расходы API, напиши /cost."
    )


@dp.message(Command("today"))
async def today(message: Message) -> None:
    await send_calendar(message, "today")


@dp.message(Command("week"))
async def week(message: Message) -> None:
    await send_calendar(message, "week")


@dp.message(Command("month"))
async def month(message: Message) -> None:
    await send_calendar(message, "month")


@dp.message(Command("list"))
async def list_events(message: Message) -> None:
    await send_calendar(message, "all")


@dp.message(Command("hot"))
async def hot_events(message: Message) -> None:
    await send_calendar(message, "hot")


@dp.message(Command("cost"))
async def cost(message: Message) -> None:
    await send_api_stats(message)


@dp.message(F.voice)
async def voice(message: Message) -> None:
    text = await transcribe_voice(message)
    if text:
        await message.answer(f"Услышал: {text}")
        await handle_text(message, text)


@dp.message(F.text)
async def text(message: Message) -> None:
    await handle_text(message, message.text or "")


@dp.callback_query()
async def callbacks(query: CallbackQuery) -> None:
    data = query.data or ""
    if data.startswith("list:"):
        _action, scope = data.split(":", 1)
        await send_calendar_to_chat(query.message.chat.id, query.from_user.id, scope)
        await query.answer()
        return

    if data.startswith("open:"):
        _action, event_id_raw = data.split(":", 1)
        await send_event_details(query.message.chat.id, int(event_id_raw))
        await query.answer()
        return

    action, event_id_raw, *rest = data.split(":")
    event_id = int(event_id_raw)
    row = await get_event(event_id)
    if not row:
        await query.answer("Не нашел событие")
        return

    if action == "seen":
        await mark_event_seen(event_id)
        await query.message.answer("Окей, ты видел. Но я еще проверю ближе к делу.")
    elif action == "departed":
        await mark_event_departed(event_id)
        await query.message.answer("Принял, выдвинулся. До старта не душню.")
    elif action == "arrived":
        await mark_event_arrived(event_id)
        await query.message.answer("Красавчик, зафиксировал: ты на месте.")
    elif action == "late":
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE events SET next_ping_at=?, updated_at=? WHERE id=?", ((now() + timedelta(minutes=5)).isoformat(), now().isoformat(), event_id))
            await db.commit()
        await query.message.answer("Понял, опаздываешь. Через 5 минут снова спрошу.")
    elif action == "done":
        if row[3] == "task":
            await mark_task_done(event_id)
            await query.message.answer("Готово. Вычеркиваю.")
        else:
            await mark_event_arrived(event_id)
            await query.message.answer("Готово, закрыл событие. Больше по нему не пингую.")
    elif action == "snooze":
        await query.message.answer("На сколько отложить?", reply_markup=snooze_keyboard(event_id))
    elif action == "snooze_set":
        minutes = int(rest[0])
        row = await get_event(event_id)
        current_start = parse_dt(row[4]) or now()
        new_start = current_start + timedelta(minutes=minutes)
        await snooze_event_to(event_id, new_start)
        await query.message.answer(f"Отложил. Новое время: {new_start.strftime('%d.%m %H:%M')}")
    elif action == "snooze_at":
        new_start = named_snooze_time(rest[0])
        await snooze_event_to(event_id, new_start)
        await query.message.answer(f"Перенес. Новое время: {new_start.strftime('%d.%m %H:%M')}")
    elif action == "cancel":
        await query.message.answer("Точно удалить?", reply_markup=confirm_delete_keyboard(event_id))
    elif action == "confirm_cancel":
        await cancel_event(event_id)
        await query.message.answer("Удалил.")
        await send_calendar_to_chat(query.message.chat.id, query.from_user.id, "all")
    elif action == "edit":
        await query.message.answer("Редактирование голосом уже можно пробовать: скажи «перенеси ...». Кнопочную правку добавлю следующей итерацией.")
    await query.answer()


async def scheduler_loop() -> None:
    await asyncio.sleep(3)
    while True:
        try:
            await send_due_notifications()
        except Exception as exc:
            print(f"scheduler error: {exc}")
        await asyncio.sleep(30)


async def send_due_notifications() -> None:
    current = now()
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (
            await db.execute(
                """
                SELECT id, user_id, title, kind, starts_at, reminders_json, sent_reminders_json,
                       seen, departed, confirmed, done, last_ping_at, next_ping_at
                FROM events
                WHERE status='active'
                """
            )
        ).fetchall()

    for row in rows:
        event_id, user_id, title, kind, starts_at_raw, reminders_raw, sent_raw, seen, departed, confirmed, done, last_ping_raw, next_ping_raw = row
        starts_at = parse_dt(starts_at_raw)
        if kind == "task":
            if done:
                continue
            last_ping = parse_dt(last_ping_raw)
            if not last_ping or last_ping.date() < current.date() and current.hour >= 9:
                await bot.send_message(user_id, f"Бро, задача еще висит: {title}", reply_markup=event_keyboard(event_id, "before", "task"))
                await mark_ping(event_id)
            continue

        if not starts_at or confirmed:
            continue

        reminders = json.loads(reminders_raw)
        sent = set(json.loads(sent_raw))
        for minutes_before in reminders:
            due_time = starts_at - timedelta(minutes=int(minutes_before))
            if current >= due_time and int(minutes_before) not in sent and not departed:
                phase_text = "Вижу, Отложить, Изменить, Отменить — выбирай, а то я буду нервничать за нас обоих."
                await bot.send_message(
                    user_id,
                    f"Бро, {title} в {starts_at.strftime('%H:%M')}.\nДо старта примерно {minutes_before} мин. {phase_text}",
                    reply_markup=event_keyboard(event_id, "before", "event"),
                )
                sent.add(int(minutes_before))
                await update_sent(event_id, sorted(sent, reverse=True))

        if current >= starts_at:
            next_ping = parse_dt(next_ping_raw)
            if not next_ping or current >= next_ping:
                interval = escalation_interval(starts_at, current, seen)
                await bot.send_message(
                    user_id,
                    f"Ты на мероприятии «{title}»? Пока не нажмешь «Я тут», я считаю, что тебя там нет.",
                    reply_markup=event_keyboard(event_id, "started", "event"),
                )
                await set_next_ping(event_id, current + timedelta(minutes=interval))


def escalation_interval(starts_at: datetime, current: datetime, seen: int) -> int:
    minutes_after_start = max(0, int((current - starts_at).total_seconds() // 60))
    if minutes_after_start >= 30:
        return 2
    if minutes_after_start >= 10:
        return 3
    return 5 if seen else 2


async def update_sent(event_id: int, sent: List[int]) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE events SET sent_reminders_json=?, updated_at=? WHERE id=?",
            (json.dumps(sent), now().isoformat(), event_id),
        )
        await db.commit()


async def mark_ping(event_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE events SET last_ping_at=?, updated_at=? WHERE id=?", (now().isoformat(), now().isoformat(), event_id))
        await db.commit()


async def set_next_ping(event_id: int, next_ping: datetime) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE events SET last_ping_at=?, next_ping_at=?, updated_at=? WHERE id=?",
            (now().isoformat(), next_ping.isoformat(), now().isoformat(), event_id),
        )
        await db.commit()


async def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Нужен TELEGRAM_BOT_TOKEN в .env. Его дает @BotFather в Telegram.")
    await init_db()
    asyncio.create_task(scheduler_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
