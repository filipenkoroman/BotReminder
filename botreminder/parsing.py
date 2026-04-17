import json
import re
import tempfile
from datetime import datetime, timedelta
from typing import List, Optional

from aiogram.types import Message

from .config import OPENAI_MODEL, OPENAI_TRANSCRIBE_MODEL, openai_client, bot
from .db import api_budget_available, log_event, month_api_spend, recent_learning_examples, record_api_usage
from .models import ParsedIntent
from .pricing import rough_token_count, text_cost_usd, transcription_cost_usd
from .time_utils import now


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
    relative_match = re.search(r"через\s+(\d+)\s*(минут|мин|час|часа|часов)", lower)
    if relative_match:
        value = int(relative_match.group(1))
        minutes_delta = value * 60 if relative_match.group(2).startswith("час") else value
        starts_at = (base + timedelta(minutes=minutes_delta)).isoformat()
        assumptions.append(f"понял относительное время: через {minutes_delta} мин")
    if "завтра" in lower:
        base = base + timedelta(days=1)
        date_was_given = True
    elif "послезавтра" in lower:
        base = base + timedelta(days=2)
        date_was_given = True
    elif "сегодня" in lower:
        date_was_given = True

    hour, minute, vague_time = extract_time(lower)
    if starts_at is None and hour is not None and minute is not None:
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
    repeat_rule = repeat_rule_from_text(text)
    return ParsedIntent(
        action="create",
        title=title or text,
        kind=kind,
        starts_at=starts_at,
        reminders=[5, 1] if relative_match else default_reminders(text),
        repeat_rule=repeat_rule,
        repeat_until=extract_repeat_until(text),
        assumptions=assumptions,
        needs_time_question=needs_time,
        original_text=text,
    )

def repeat_rule_from_text(text: str) -> Optional[str]:
    lower = text.lower()
    if any(marker in lower for marker in ["каждый день", "ежеднев", "раз в день"]):
        return "каждый день"
    if any(marker in lower for marker in ["каждые 2 недели", "каждые две недели", "раз в 2 недели", "раз в две недели"]):
        return "каждые две недели"
    weekdays = {
        "понедельник": ["понедельник", "понедельникам"],
        "вторник": ["вторник", "вторникам"],
        "среду": ["среду", "средам"],
        "четверг": ["четверг", "четвергам"],
        "пятницу": ["пятницу", "пятницам"],
        "субботу": ["субботу", "субботам"],
        "воскресенье": ["воскресенье", "воскресеньям"],
    }
    for label, forms in weekdays.items():
        if any(f"каждый {form}" in lower or f"каждую {form}" in lower for form in forms):
            return f"каждую неделю, {label}"
    if any(marker in lower for marker in ["каждую неделю", "еженедель", "раз в неделю"]):
        return "каждую неделю"
    month_day = monthly_day_from_text(text)
    if month_day:
        return f"каждый месяц {month_day} числа"
    if any(marker in lower for marker in ["каждый месяц", "ежемесяч", "раз в месяц"]):
        return "каждый месяц"
    return None

def extract_repeat_until(text: str) -> Optional[str]:
    lower = text.lower()
    current = now()
    if any(marker in lower for marker in ["навсегда", "бессрочно", "пока не отменю", "без конца"]):
        return "never"
    if "до конца года" in lower:
        return current.replace(month=12, day=31, hour=23, minute=59).isoformat()

    relative = re.search(r"\b(?:на|до)\s+(\d+)\s*(день|дня|дней|неделю|недели|недель|месяц|месяца|месяцев|год|года|лет)\b", lower)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        if unit.startswith("д"):
            return (current + timedelta(days=amount)).replace(hour=23, minute=59).isoformat()
        if unit.startswith("н"):
            return (current + timedelta(days=amount * 7)).replace(hour=23, minute=59).isoformat()
        if unit.startswith("м"):
            return add_months(current, amount).replace(hour=23, minute=59).isoformat()
        return current.replace(year=current.year + amount, hour=23, minute=59).isoformat()

    date_match = re.search(r"\bдо\s+(\d{1,2})(?:[.\s/]+(\d{1,2}|января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря))(?:[.\s/]+(\d{2,4}))?\b", lower)
    if date_match:
        day = int(date_match.group(1))
        month_raw = date_match.group(2)
        year_raw = date_match.group(3)
        month = month_number(month_raw)
        year = int(year_raw) if year_raw else current.year
        if year < 100:
            year += 2000
        try:
            candidate = current.replace(year=year, month=month, day=day, hour=23, minute=59)
        except ValueError:
            return None
        if not year_raw and candidate < current:
            candidate = candidate.replace(year=year + 1)
        return candidate.isoformat()
    return None

def month_number(value: str) -> int:
    months = {
        "января": 1,
        "февраля": 2,
        "марта": 3,
        "апреля": 4,
        "мая": 5,
        "июня": 6,
        "июля": 7,
        "августа": 8,
        "сентября": 9,
        "октября": 10,
        "ноября": 11,
        "декабря": 12,
    }
    if value in months:
        return months[value]
    if value.isdigit():
        return int(value)
    return 1

def add_months(value: datetime, months: int) -> datetime:
    month = value.month - 1 + months
    year = value.year + month // 12
    month = month % 12 + 1
    day = min(value.day, days_in_month(year, month))
    return value.replace(year=year, month=month, day=day)

def days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    first_next = datetime(year, month + 1, 1, tzinfo=now().tzinfo)
    return (first_next - timedelta(days=1)).day

def next_monthly_occurrence(day: int, hour: int = 9, minute: int = 0) -> datetime:
    current = now()
    year = current.year
    month = current.month
    while True:
        try:
            candidate = current.replace(year=year, month=month, day=day, hour=hour, minute=minute)
        except ValueError:
            month += 1
            if month == 13:
                month = 1
                year += 1
            continue
        if candidate > current:
            return candidate
        month += 1
        if month == 13:
            month = 1
            year += 1

def monthly_day_from_text(text: str) -> Optional[int]:
    lower = text.lower()
    if not any(marker in lower for marker in ["раз в месяц", "каждый месяц", "ежемесяч"]):
        return None
    match = re.search(r"\b([1-9]|[12]\d|3[01])\s*(?:числа|число)?\b", lower)
    if not match:
        return None
    return int(match.group(1))

def normalize_parsed_intent(parsed: ParsedIntent) -> ParsedIntent:
    if parsed.action != "create":
        return parsed

    source = " ".join(filter(None, [parsed.original_text, parsed.repeat_rule or ""]))
    lower_source = source.lower()
    parsed.repeat_rule = parsed.repeat_rule or repeat_rule_from_text(source)
    parsed.repeat_until = parsed.repeat_until or extract_repeat_until(source)

    if any(marker in lower_source for marker in ["подписк", "списан", "оплат", "платеж", "платёж", "счет", "счёт"]):
        parsed.kind = "task"

    day = monthly_day_from_text(source)
    starts_at = None
    if parsed.starts_at:
        try:
            starts_at = datetime.fromisoformat(parsed.starts_at).astimezone(now().tzinfo)
        except ValueError:
            starts_at = None

    if day and (not starts_at or starts_at <= now()):
        hour = starts_at.hour if starts_at else 9
        minute = starts_at.minute if starts_at else 0
        next_start = next_monthly_occurrence(day, hour, minute)
        parsed.starts_at = next_start.isoformat()
        parsed.repeat_rule = parsed.repeat_rule or f"каждый месяц {day} числа"
        parsed.assumptions = list(parsed.assumptions or [])
        parsed.assumptions.append(f"исправил дату в прошлом: ближайшее {day} число — {next_start.strftime('%d.%m.%Y')}")
        starts_at = next_start

    if starts_at and starts_at <= now() and not day:
        shifted = starts_at + timedelta(days=1)
        while shifted <= now():
            shifted += timedelta(days=1)
        parsed.starts_at = shifted.isoformat()
        parsed.assumptions = list(parsed.assumptions or [])
        parsed.assumptions.append(f"исправил дату в прошлом на ближайшее будущее время: {shifted.strftime('%d.%m %H:%M')}")

    if parsed.repeat_rule and not parsed.repeat_until:
        parsed.needs_repeat_until_question = True

    return parsed

async def ai_parse(text: str, user_id: int = 0) -> ParsedIntent:
    if not openai_client:
        return fallback_parse(text)
    if not await api_budget_available(user_id):
        return fallback_parse(text)

    learned = await recent_learning_examples(user_id)
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
  "repeat_until": "ISO datetime with timezone, never или null",
  "assumptions": ["короткие объяснения твоих догадок"],
  "needs_time_question": true|false,
  "needs_repeat_until_question": true|false
}}
Если задача без времени, поставь kind=task, starts_at=null, needs_time_question=true.
Если есть примерное время ("около 17", "после 14", "ближе к вечеру", "после обеда"), НЕ спрашивай точное время:
создай событие на разумное рабочее время, поставь starts_at и добавь assumptions.
Для "около 17" ставь 17:00 сегодня, если время еще впереди, иначе завтра.
Для "после 14" ставь 14:00 сегодня, если время еще впереди, иначе завтра.
Для "ближе к вечеру" ставь 18:00 сегодня, если время еще впереди, иначе завтра.
Для "после обеда" ставь 14:00 сегодня, если время еще впереди, иначе завтра.
Если время примерное, reminders=[60,30,15,10].
Если пользователь пишет "через N минут/часов", поставь starts_at=N минут/часов от текущего времени, kind=event, reminders=[5,1] для коротких интервалов.
Если пользователь просит повтор ("каждый день", "каждую неделю", "каждые две недели", "раз в месяц", "каждое 15 число"), заполни repeat_rule.
Если пользователь явно говорит "навсегда", "бессрочно", "пока не отменю", поставь repeat_until="never".
Если пользователь говорит "до 1 сентября", "до конца года", "на 3 месяца", поставь repeat_until ISO.
Если repeat_rule есть, но непонятно когда прекращать повторять, поставь needs_repeat_until_question=true.
Спрашивай уточнение только если вообще нет даты/времени или невозможно сделать полезную догадку.
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
        parsed = normalize_parsed_intent(ParsedIntent(
            action=payload.get("action", "unknown"),
            title=payload.get("title"),
            kind=payload.get("kind") or "event",
            starts_at=payload.get("starts_at"),
            reminders=payload.get("reminders") or default_reminders(text),
            repeat_rule=payload.get("repeat_rule"),
            repeat_until=payload.get("repeat_until"),
            assumptions=payload.get("assumptions") or [],
            needs_time_question=bool(payload.get("needs_time_question")),
            needs_repeat_until_question=bool(payload.get("needs_repeat_until_question")),
            original_text=text,
        ))
        await log_event(user_id, "ai_parse", text, parsed.__dict__)
        return parsed
    except Exception:
        parsed = normalize_parsed_intent(fallback_parse(text))
        await log_event(user_id, "fallback_parse_after_ai_error", text, parsed.__dict__)
        return parsed

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

def apply_repeat_until_to_pending(pending: ParsedIntent, text: str) -> Optional[ParsedIntent]:
    repeat_until = extract_repeat_until(text)
    if not repeat_until:
        return None
    pending.repeat_until = repeat_until
    pending.needs_repeat_until_question = False
    pending.assumptions = list(pending.assumptions or [])
    if repeat_until == "never":
        pending.assumptions.append("повтор будет идти, пока ты сам его не отменишь")
    else:
        until_dt = datetime.fromisoformat(repeat_until)
        pending.assumptions.append(f"повтор будет идти до {until_dt.strftime('%d.%m.%Y')}")
    return pending
