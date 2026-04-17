import json
import re
from datetime import datetime, timedelta
from typing import List, Optional

import aiosqlite

from .config import API_MONTHLY_LIMIT_USD, DB_PATH
from .models import ParsedIntent
from .time_utils import month_start_iso, now, parse_dt, scope_window


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

async def log_event(user_id: Optional[int], event_type: str, text: Optional[str] = None, payload: Optional[dict] = None) -> None:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO bot_logs(user_id, event_type, text, payload_json, created_at)
                VALUES(?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    event_type,
                    text,
                    json.dumps(payload or {}, ensure_ascii=False),
                    now().isoformat(),
                ),
            )
            await db.commit()
    except Exception as exc:
        print(f"log error: {exc}")

async def save_learning_example(user_id: int, user_text: str, expected_behavior: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO learning_examples(user_id, user_text, expected_behavior, created_at)
            VALUES(?, ?, ?, ?)
            """,
            (user_id, user_text.strip(), expected_behavior.strip(), now().isoformat()),
        )
        await db.commit()
    await log_event(
        user_id,
        "learning_saved",
        user_text,
        {"expected_behavior": expected_behavior},
    )

async def recent_learning_examples(user_id: int, limit: int = 8) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (
            await db.execute(
                """
                SELECT user_text, expected_behavior FROM learning_examples
                WHERE user_id=?
                ORDER BY id DESC
                LIMIT ?
                """,
                (user_id, limit),
            )
        ).fetchall()
    if not rows:
        return ""
    lines = ["Примеры обучения от пользователя. Учитывай их как предпочтения:"]
    for user_text, expected in reversed(rows):
        lines.append(f"- Если пользователь пишет: {user_text!r}, надо: {expected}")
    return "\n".join(lines)

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
                repeat_until TEXT,
                raw_text TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await ensure_column(db, "events", "repeat_until", "TEXT")
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
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                event_type TEXT NOT NULL,
                text TEXT,
                payload_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS learning_examples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                user_text TEXT NOT NULL,
                expected_behavior TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        await db.commit()

async def ensure_column(db: aiosqlite.Connection, table: str, column: str, column_type: str) -> None:
    rows = await (await db.execute(f"PRAGMA table_info({table})")).fetchall()
    if column not in {row[1] for row in rows}:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")

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
            INSERT INTO events(user_id, title, kind, starts_at, reminders_json, repeat_rule, repeat_until, raw_text, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                parsed.title or "Без названия",
                parsed.kind,
                parsed.starts_at,
                json.dumps(parsed.reminders or [60, 30]),
                parsed.repeat_rule,
                parsed.repeat_until,
                parsed.original_text,
                created,
                created,
            ),
        )
        await db.commit()
        return int(cursor.lastrowid)

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
        row = await (
            await db.execute("SELECT starts_at, repeat_rule, repeat_until FROM events WHERE id=?", (event_id,))
        ).fetchone()
        next_start = next_repeat_start(row[0], row[1], row[2]) if row else None
        if next_start:
            await db.execute(
                """
                UPDATE events SET starts_at=?, status='active', sent_reminders_json='[]',
                    seen=0, departed=0, confirmed=0, done=0, last_ping_at=NULL,
                    next_ping_at=NULL, updated_at=? WHERE id=?
                """,
                (next_start.isoformat(), now().isoformat(), event_id),
            )
            await db.commit()
            return
        await db.execute(
            "UPDATE events SET confirmed=1, status='confirmed', updated_at=? WHERE id=?",
            (now().isoformat(), event_id),
        )
        await db.commit()

async def mark_task_done(event_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (
            await db.execute("SELECT starts_at, repeat_rule, repeat_until FROM events WHERE id=?", (event_id,))
        ).fetchone()
        next_start = next_repeat_start(row[0], row[1], row[2]) if row else None
        if next_start:
            await db.execute(
                """
                UPDATE events SET starts_at=?, status='active', sent_reminders_json='[]',
                    seen=0, departed=0, confirmed=0, done=0, last_ping_at=NULL,
                    next_ping_at=NULL, updated_at=? WHERE id=?
                """,
                (next_start.isoformat(), now().isoformat(), event_id),
            )
            await db.commit()
            return
        await db.execute(
            "UPDATE events SET done=1, status='done', updated_at=? WHERE id=?",
            (now().isoformat(), event_id),
        )
        await db.commit()

def next_repeat_start(
    starts_at_raw: Optional[str],
    repeat_rule: Optional[str],
    repeat_until_raw: Optional[str] = None,
) -> Optional[datetime]:
    if not starts_at_raw or not repeat_rule:
        return None
    lower = repeat_rule.lower()
    starts_at = parse_dt(starts_at_raw)
    if not starts_at:
        return None

    def allowed(candidate: datetime) -> bool:
        if not repeat_until_raw or repeat_until_raw == "never":
            return True
        repeat_until = parse_dt(repeat_until_raw)
        return bool(repeat_until and candidate <= repeat_until)

    candidate = None
    if any(marker in lower for marker in ["каждый день", "ежеднев", "раз в день"]):
        candidate = starts_at + timedelta(days=1)
        while candidate <= now():
            candidate += timedelta(days=1)
    elif any(marker in lower for marker in ["каждые 2 недели", "каждые две недели", "раз в 2 недели", "раз в две недели"]):
        candidate = starts_at + timedelta(days=14)
        while candidate <= now():
            candidate += timedelta(days=14)
    elif any(marker in lower for marker in ["каждую неделю", "каждый понедельник", "каждый вторник", "каждую среду", "каждый четверг", "каждую пятницу", "каждую субботу", "каждое воскресенье", "еженедель", "раз в неделю"]):
        candidate = starts_at + timedelta(days=7)
        while candidate <= now():
            candidate += timedelta(days=7)
    elif any(marker in lower for marker in ["каждый месяц", "раз в месяц", "ежемесяч"]):
        day_match = re.search(r"\b([1-9]|[12]\d|3[01])\s*(?:числа|число)?\b", lower)
        day = int(day_match.group(1)) if day_match else starts_at.day
        year = starts_at.year
        month = starts_at.month + 1
        if month == 13:
            month = 1
            year += 1
        while True:
            try:
                candidate = starts_at.replace(year=year, month=month, day=day)
            except ValueError:
                month += 1
                if month == 13:
                    month = 1
                    year += 1
                continue
            if candidate > now():
                break
            month += 1
            if month == 13:
                month = 1
                year += 1
    if candidate and allowed(candidate):
        return candidate
    return None

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

async def get_event(event_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        return await (
            await db.execute(
                "SELECT id, user_id, title, kind, starts_at, reminders_json, sent_reminders_json, seen, departed, confirmed, done FROM events WHERE id=?",
                (event_id,),
            )
        ).fetchone()

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
