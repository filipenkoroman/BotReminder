import asyncio
import json
from datetime import datetime, timedelta
from typing import List

import aiosqlite

from .config import DB_PATH, bot
from .db import mark_ping, set_next_ping, update_sent
from .keyboards import event_keyboard
from .time_utils import now, parse_dt


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
            if starts_at:
                reminders = json.loads(reminders_raw)
                sent = set(json.loads(sent_raw))
                for minutes_before in reminders:
                    due_time = starts_at - timedelta(minutes=int(minutes_before))
                    if current >= due_time and int(minutes_before) not in sent:
                        await bot.send_message(
                            user_id,
                            f"Бро, «{title}» актуально в {starts_at.strftime('%H:%M')}.\n"
                            f"Напоминаю за {minutes_before} мин. Нажми «Сделано», когда реально закроешь это.",
                            reply_markup=event_keyboard(event_id, "before", kind, starts_at_raw, title),
                        )
                        sent.add(int(minutes_before))
                        await update_sent(event_id, sorted(sent, reverse=True))
                next_ping = parse_dt(next_ping_raw)
                if current >= starts_at and (not next_ping or current >= next_ping):
                    await bot.send_message(
                        user_id,
                        f"«{title}» уже актуально. Пока не нажмешь «Сделано», я считаю, что это висит.",
                        reply_markup=event_keyboard(event_id, "before", kind, starts_at_raw, title),
                    )
                    await set_next_ping(event_id, current + timedelta(minutes=30))
                continue
            last_ping = parse_dt(last_ping_raw)
            if not last_ping or last_ping.date() < current.date() and current.hour >= 9:
                await bot.send_message(user_id, f"Бро, еще висит: {title}", reply_markup=event_keyboard(event_id, "before", kind, starts_at_raw, title))
                await mark_ping(event_id)
            continue

        if not starts_at or confirmed:
            continue

        reminders = json.loads(reminders_raw)
        sent = set(json.loads(sent_raw))
        for minutes_before in reminders:
            due_time = starts_at - timedelta(minutes=int(minutes_before))
            if current >= due_time and int(minutes_before) not in sent and not departed:
                phase_text = "Выбирай: 👀 Вижу, 🚶 Еду, ✅ Сделано, ⏰ Позже или ✏️ Править."
                await bot.send_message(
                    user_id,
                    f"Бро, {title} в {starts_at.strftime('%H:%M')}.\nДо старта примерно {minutes_before} мин. {phase_text}",
                    reply_markup=event_keyboard(event_id, "before", kind, starts_at_raw, title),
                )
                sent.add(int(minutes_before))
                await update_sent(event_id, sorted(sent, reverse=True))

        if current >= starts_at:
            next_ping = parse_dt(next_ping_raw)
            if not next_ping or current >= next_ping:
                interval = escalation_interval(starts_at, current, seen)
                await bot.send_message(
                    user_id,
                    f"Ты уже на месте по «{title}»? Нажми «На месте», если ты там, или «Сделано», если вопрос уже закрыт.",
                    reply_markup=event_keyboard(event_id, "started", kind, starts_at_raw, title),
                )
                await set_next_ping(event_id, current + timedelta(minutes=interval))

def escalation_interval(starts_at: datetime, current: datetime, seen: int) -> int:
    minutes_after_start = max(0, int((current - starts_at).total_seconds() // 60))
    if minutes_after_start >= 30:
        return 2
    if minutes_after_start >= 10:
        return 3
    return 5 if seen else 2
