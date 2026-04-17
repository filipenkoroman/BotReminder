import json
import re
from datetime import timedelta
from typing import Optional

from aiogram.types import Message

from .config import OPENAI_MODEL, openai_client
from .db import (
    api_budget_available,
    log_event,
    mark_event_arrived,
    mark_event_departed,
    mark_event_seen,
    mark_task_done,
    recent_learning_examples,
    record_api_usage,
    snooze_event_to,
)
from .keyboards import confirm_delete_keyboard
from .models import CommandIntent
from .parsing import time_from_text
from .pricing import rough_token_count, text_cost_usd
from .time_utils import now, parse_dt


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

    learned = await recent_learning_examples(user_id)
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
{learned}
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
        intent = CommandIntent(
            action=payload.get("action", "new"),
            confidence=float(payload.get("confidence") or 0),
            minutes=payload.get("minutes"),
            starts_at=payload.get("starts_at"),
            question=payload.get("question"),
            assumptions=payload.get("assumptions") or [],
        )
        await log_event(user_id, "ai_command_intent", text, intent.__dict__)
        return intent
    except Exception:
        await log_event(user_id, "ai_command_error", text, {"focus_title": focus_title})
        return None

async def apply_command_intent(message: Message, focus, intent: CommandIntent) -> bool:
    event_id, title, kind, starts_at, _reminders, _sent, _seen, _departed, _confirmed, _done = focus
    await log_event(
        message.from_user.id,
        "command_apply",
        message.text or "",
        {"event_id": event_id, "title": title, **intent.__dict__},
    )
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
