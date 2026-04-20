import json

import aiosqlite
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from .config import API_MONTHLY_LIMIT_USD, DB_PATH, bot
from .db import (
    cancel_event,
    create_event,
    fetch_calendar_rows,
    get_event,
    log_event,
    month_api_spend,
    normalize_title,
)
from .google_sync import sync_google_event
from .keyboards import calendar_keyboard, confirm_delete_keyboard, event_keyboard, manage_keyboard
from .models import ParsedIntent
from .time_utils import fmt_dt, month_start_iso, now, parse_dt


HELP_TEXT = """Что я умею:

/today — что сегодня
/week — ближайшая неделя
/month — ближайший месяц
/list — все активные напоминания
/hot — что горит прямо сейчас
/cost — расходы OpenAI API
/logs — последние действия бота
/learned — чему я уже научился
/help — это меню

Кнопки:
👀 Вижу — я заметил напоминание
🚶 Еду — уже выдвинулся
📍 На месте — я уже там
✅ Сделано — закрыл, больше не пинговать
⏰ Позже — отложить
✏️ Править — изменить название или время
← Список — вернуться к списку

Можно писать обычным языком:
«Стоматолог завтра в 10, напомни за час и за 30 минут»
«Напомни что каждого 15 числа будет оплата за личный чат джпт»
«Купить подарок на неделе»
«Удали встречу с Сашей»
«Сделано»

Для повторов я спрошу, когда перестать повторять: навсегда, до 1 сентября или на 3 месяца."""


async def finish_create(message: Message, parsed: ParsedIntent) -> None:
    event_id = await create_event(message.from_user.id, parsed)
    await sync_google_event(event_id)
    await log_event(message.from_user.id, "event_created", parsed.original_text, {"event_id": event_id, **parsed.__dict__})
    title = normalize_title(parsed.title)
    if parsed.kind == "task":
        if parsed.starts_at:
            text = (
                f"Записал: {title}\n"
                f"Когда: {fmt_dt(parsed.starts_at)}\n"
                f"Напомню: {', '.join(str(x) + ' мин' for x in (parsed.reminders or [60, 30]))}."
            )
            if parsed.repeat_rule:
                text += repeat_text(parsed)
        else:
            text = f"Записал: {title}\nПинги: утром каждый день, пока не нажмешь «Сделано»."
        await message.answer(text, reply_markup=event_keyboard(event_id, "before", parsed.kind, parsed.starts_at, title))
    else:
        text = f"Записал: {title}\nКогда: {fmt_dt(parsed.starts_at)}\nНапомню: {', '.join(str(x) + ' мин' for x in (parsed.reminders or [60, 30]))}."
        if parsed.repeat_rule:
            text += repeat_text(parsed)
        await message.answer(text, reply_markup=event_keyboard(event_id, "before", parsed.kind, parsed.starts_at, title))

def repeat_text(parsed: ParsedIntent) -> str:
    if parsed.repeat_until == "never":
        return f"\nПовтор: {parsed.repeat_rule}, пока не отменишь."
    if parsed.repeat_until:
        return f"\nПовтор: {parsed.repeat_rule}, до {fmt_dt(parsed.repeat_until)}."
    return f"\nПовтор: {parsed.repeat_rule}."

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
        await message.answer("Не нашел похожее напоминание. Скажи название чуть точнее.")
        return
    if len(rows) == 1:
        event_id, title, _starts_at, _kind = rows[0]
        await message.answer(f"Удаляем «{normalize_title(title)}»?", reply_markup=confirm_delete_keyboard(event_id))
        return
    buttons = [[InlineKeyboardButton(text=f"{normalize_title(title)} · {fmt_dt(starts_at)}", callback_data=f"open:{event_id}")]
               for event_id, title, starts_at, _kind in rows]
    await message.answer("Нашел несколько похожих. Какое открыть для удаления?", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

async def send_calendar(message: Message, scope: str) -> None:
    rows = await fetch_calendar_rows(message.from_user.id, scope)
    if not rows:
        await message.answer("Пусто. Приятная редкость.")
        return
    lines = ["Что горит:" if scope == "hot" else "Вот что вижу:"]
    for _id, title, _kind, starts_at, _status in rows:
        lines.append(f"• {fmt_dt(starts_at)} · {normalize_title(title)}")
    lines.append("\nТыкни ниже, и я дам кнопки управления.")
    await message.answer("\n".join(lines), reply_markup=calendar_keyboard(rows))

async def send_calendar_to_chat(chat_id: int, user_id: int, scope: str) -> None:
    rows = await fetch_calendar_rows(user_id, scope)
    if not rows:
        await bot.send_message(chat_id, "Пусто. Приятная редкость.")
        return
    lines = ["Что горит:" if scope == "hot" else "Вот список для управления:"]
    for _id, title, _kind, starts_at, _status in rows:
        lines.append(f"• {fmt_dt(starts_at)} · {normalize_title(title)}")
    await bot.send_message(chat_id, "\n".join(lines), reply_markup=calendar_keyboard(rows))

async def send_event_details(chat_id: int, event_id: int) -> None:
    row = await get_event(event_id)
    if not row:
        await bot.send_message(chat_id, "Не нашел напоминание.")
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
        status_bits.append("сделано")
    phase = "started" if starts_at and parse_dt(starts_at) and now() >= parse_dt(starts_at) else "before"
    text = (
        f"{normalize_title(title)}\n"
        f"Когда: {fmt_dt(starts_at)}\n"
        f"Напоминания: {reminders}\n"
        f"Статус: {', '.join(status_bits) if status_bits else 'активно'}"
    )
    await bot.send_message(chat_id, text, reply_markup=manage_keyboard(event_id, kind, phase, starts_at, title))

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

async def send_learning_examples(message: Message) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (
            await db.execute(
                """
                SELECT user_text, expected_behavior, created_at FROM learning_examples
                WHERE user_id=?
                ORDER BY id DESC
                LIMIT 10
                """,
                (message.from_user.id,),
            )
        ).fetchall()
    if not rows:
        await message.answer("Пока нет сохраненных кейсов обучения.")
        return
    lines = ["Чему я уже научился:"]
    for user_text, expected, created_at in reversed(rows):
        lines.append(f"• {created_at[5:16]}: {user_text} -> {expected}")
    await message.answer("\n".join(lines))

async def send_recent_logs(message: Message, limit: int = 20) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (
            await db.execute(
                """
                SELECT created_at, event_type, text, payload_json FROM bot_logs
                WHERE user_id=? OR user_id IS NULL
                ORDER BY id DESC
                LIMIT ?
                """,
                (message.from_user.id, limit),
            )
        ).fetchall()
    if not rows:
        await message.answer("Лог пока пуст.")
        return
    lines = ["Последние события бота:"]
    for created_at, event_type, text, payload_json in reversed(rows):
        short_text = (text or "").replace("\n", " ")[:80]
        payload = json.loads(payload_json or "{}")
        action = payload.get("action") or payload.get("callback") or ""
        suffix = f" -> {action}" if action else ""
        lines.append(f"• {created_at[5:16]} · {event_type}{suffix} · {short_text}")
    await message.answer("\n".join(lines[-30:]))
