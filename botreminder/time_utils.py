from datetime import datetime, timedelta
from typing import Optional

from .config import BOT_TIMEZONE


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

def month_start_iso() -> str:
    current = now()
    return current.replace(day=1, hour=0, minute=0).isoformat()
