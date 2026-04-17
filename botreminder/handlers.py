import re
from datetime import timedelta

import aiosqlite
from aiogram import F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from .commands import ai_command_intent, apply_command_intent, local_command_intent
from .config import BOT_OWNER_ID, DB_PATH, dp
from .db import (
    cancel_event,
    find_focus_event,
    get_event,
    log_event,
    mark_event_arrived,
    mark_event_departed,
    mark_event_seen,
    mark_task_done,
    save_learning_example,
    save_pending_question,
    snooze_event_to,
)
from .keyboards import confirm_delete_keyboard, manage_keyboard, snooze_keyboard
from .models import ParsedIntent
from .parsing import (
    ai_parse,
    apply_repeat_until_to_pending,
    apply_time_to_pending,
    merge_pending_with_parsed,
    named_snooze_time,
    normalize_parsed_intent,
    transcribe_voice,
)
from .time_utils import now, parse_dt
from .views import (
    ask_delete,
    finish_create,
    send_api_stats,
    send_calendar,
    send_calendar_to_chat,
    send_event_details,
    send_learning_examples,
    send_recent_logs,
)

def owner_allowed(message: Message) -> bool:
    return not BOT_OWNER_ID or str(message.from_user.id) == BOT_OWNER_ID

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
    await log_event(message.from_user.id, "user_text", text)

    learning_match = re.match(
        r"(?is)^(?:запомни|научись|это надо было понять как|это нужно понимать как)\s*[:\-]?\s*(.+?)\s*(?:=>|->|как|значит)\s*(.+)$",
        text.strip(),
    )
    if learning_match:
        user_text, expected = learning_match.group(1).strip(), learning_match.group(2).strip()
        await save_learning_example(message.from_user.id, user_text, expected)
        await message.answer("Запомнил этот кейс. В следующих разборах буду учитывать.")
        return

    if await handle_quick_reply(message, text):
        return

    pending = await pop_pending_question(message.from_user.id)
    if pending:
        if pending.needs_repeat_until_question:
            parsed = apply_repeat_until_to_pending(pending, text)
            if not parsed:
                question = "Не понял срок повтора. Ответь, например: «навсегда», «до 1 сентября» или «на 3 месяца»."
                await save_pending_question(message.from_user.id, pending, question)
                await message.answer(question)
                return
        else:
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

    parsed = normalize_parsed_intent(parsed)

    if parsed.action != "create":
        await log_event(message.from_user.id, "unknown_intent", text, parsed.__dict__)
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

    if parsed.repeat_rule and parsed.needs_repeat_until_question and not parsed.repeat_until:
        question = (
            f"Повтор понял: {parsed.repeat_rule}. Когда перестать повторять?\n"
            "Можно ответить: «навсегда», «до 1 сентября» или «на 3 месяца»."
        )
        await save_pending_question(message.from_user.id, parsed, question)
        await message.answer(question)
        return

    await finish_create(message, parsed)
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
        "Чтобы проверить расходы API, напиши /cost.\n"
        "Чтобы посмотреть логи, напиши /logs."
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


@dp.message(Command("logs"))

async def logs(message: Message) -> None:
    await send_recent_logs(message)


@dp.message(Command("learned"))

async def learned(message: Message) -> None:
    await send_learning_examples(message)


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
    await log_event(query.from_user.id, "callback", data, {"callback": data})
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
