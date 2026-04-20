from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .db import normalize_title
from .time_utils import fmt_dt


def event_keyboard(event_id: int, phase: str, kind: str, starts_at: str = None, title: str = None) -> InlineKeyboardMarkup:
    if phase == "started":
        rows = [
            [
                InlineKeyboardButton(text="📍 На месте", callback_data=f"arrived:{event_id}"),
                InlineKeyboardButton(text="🚶 Еду", callback_data=f"departed:{event_id}"),
            ],
            [
                InlineKeyboardButton(text="✅ Сделано", callback_data=f"done:{event_id}"),
                InlineKeyboardButton(text="⏰ Позже", callback_data=f"snooze:{event_id}"),
            ],
        ]
    else:
        rows = [
            [
                InlineKeyboardButton(text="👀 Вижу", callback_data=f"seen:{event_id}"),
                InlineKeyboardButton(text="🚶 Еду", callback_data=f"departed:{event_id}"),
            ],
            [
                InlineKeyboardButton(text="✅ Сделано", callback_data=f"done:{event_id}"),
                InlineKeyboardButton(text="⏰ Позже", callback_data=f"snooze:{event_id}"),
            ],
        ]
    rows.append([InlineKeyboardButton(text="✏️ Править", callback_data=f"edit:{event_id}")])
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
                InlineKeyboardButton(text="Вечером", callback_data=f"snooze_at:{event_id}:evening"),
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
    for event_id, title, _kind, starts_at, _status in rows:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{fmt_dt(starts_at)} · {normalize_title(title)}"[:64],
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

def manage_keyboard(event_id: int, kind: str, phase: str = "before", starts_at: str = None, title: str = None) -> InlineKeyboardMarkup:
    if phase == "started":
        rows = [
            [
                InlineKeyboardButton(text="📍 На месте", callback_data=f"arrived:{event_id}"),
                InlineKeyboardButton(text="🚶 Еду", callback_data=f"departed:{event_id}"),
            ],
            [
                InlineKeyboardButton(text="✅ Сделано", callback_data=f"done:{event_id}"),
                InlineKeyboardButton(text="⏰ Позже", callback_data=f"snooze:{event_id}"),
            ],
        ]
    else:
        rows = [
            [
                InlineKeyboardButton(text="👀 Вижу", callback_data=f"seen:{event_id}"),
                InlineKeyboardButton(text="🚶 Еду", callback_data=f"departed:{event_id}"),
            ],
            [
                InlineKeyboardButton(text="✅ Сделано", callback_data=f"done:{event_id}"),
                InlineKeyboardButton(text="⏰ Позже", callback_data=f"snooze:{event_id}"),
            ],
        ]
    rows.append([InlineKeyboardButton(text="✏️ Править", callback_data=f"edit:{event_id}")])
    rows.append([InlineKeyboardButton(text="← Список", callback_data="list:all")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
