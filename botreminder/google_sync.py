import json
from datetime import timedelta
from typing import Optional

import httpx

from .config import BOT_TIMEZONE, GOOGLE_APPS_SCRIPT_SECRET, GOOGLE_APPS_SCRIPT_URL, GOOGLE_CALENDAR_ID
from .db import fetch_google_sync_rows, get_event_for_sync, log_event, normalize_title, set_google_event_id
from .time_utils import parse_dt


def google_sync_enabled() -> bool:
    return bool(GOOGLE_CALENDAR_ID and GOOGLE_APPS_SCRIPT_URL and GOOGLE_APPS_SCRIPT_SECRET)


def _event_payload(row) -> Optional[dict]:
    _id, _user_id, title, _kind, starts_at_raw, status, reminders_raw, repeat_rule, repeat_until, raw_text, _google_id = row
    start = parse_dt(starts_at_raw)
    if not start:
        return None
    end = start + timedelta(hours=1)
    reminders = []
    for minutes in json.loads(reminders_raw or "[]")[:5]:
        try:
            reminders.append(int(minutes))
        except (TypeError, ValueError):
            continue
    description_lines = ["Создано BotReminder"]
    if raw_text:
        description_lines.append(f"Исходный текст: {raw_text}")
    if repeat_rule:
        description_lines.append(f"Повтор: {repeat_rule}")
    if repeat_until:
        description_lines.append(f"Повторять до: {repeat_until}")
    return {
        "secret": GOOGLE_APPS_SCRIPT_SECRET,
        "calendar_id": GOOGLE_CALENDAR_ID,
        "bot_event_id": str(_id),
        "google_event_id": _google_id,
        "status": status,
        "summary": normalize_title(title),
        "description": "\n".join(description_lines),
        "start_at": start.isoformat(),
        "end_at": end.isoformat(),
        "timezone": getattr(BOT_TIMEZONE, "key", "Asia/Novosibirsk"),
        "reminders": reminders,
    }


async def sync_google_event(event_id: int) -> None:
    if not google_sync_enabled():
        return
    row = await get_event_for_sync(event_id)
    if not row:
        return
    try:
        payload = _event_payload(row)
        if not payload:
            return
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.post(GOOGLE_APPS_SCRIPT_URL, json=payload)
            response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "Apps Script sync failed")
        google_event_id = result.get("google_event_id")
        await set_google_event_id(event_id, google_event_id)
        await log_event(row[1], "google_sync", row[2], {"event_id": event_id, "google_event_id": google_event_id})
    except Exception as exc:
        await log_event(row[1], "google_sync_error", row[2], {"event_id": event_id, "error": str(exc)})


async def sync_existing_google_events() -> None:
    if not google_sync_enabled():
        return
    for row in await fetch_google_sync_rows():
        await sync_google_event(row[0])
