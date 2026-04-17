# BotReminder

Личный Telegram-бот напоминаний, который не просто пишет "скоро событие", а дожимает до реакции: увидел, выдвинулся, на месте, готово, отложить или отменить.

## Что нужно

1. Создать бота через Telegram `@BotFather` и получить `TELEGRAM_BOT_TOKEN`.
2. Для голосовых сообщений нужен `OPENAI_API_KEY`. Подписка ChatGPT Plus за $20 не включает API-ключ, он создается отдельно в OpenAI Platform. Без ключа бот все равно работает текстом.
3. Python 3.11+.

## Быстрый старт

```bash
cp .env.example .env
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

## Примеры

```text
Стоматолог завтра в 10, напомни за час и за 30 минут
На неделе купить подарок
Каждый понедельник в 9 созвон с командой
Удали встречу с Сашей
Что у меня сегодня?
Покажи неделю
```

## Команды

- `/start` - приветствие
- `/today` - день
- `/week` - неделя
- `/month` - месяц
- `/list` - список активных событий с кнопками управления
- `/hot` - только срочное и незакрытое
- `/cost` - примерные расходы OpenAI API за месяц

## API-бюджет

По умолчанию бот использует:

- `gpt-5.4-nano` для понимания текста;
- `gpt-4o-mini-transcribe` для голосовых;
- месячный лимит `API_MONTHLY_LIMIT_USD=5`.

Если лимит исчерпан, бот перестает ходить в OpenAI API и продолжает работать локальными правилами.

## Структура проекта

```text
bot.py                  # тонкая точка входа
botreminder/config.py   # настройки, Telegram/OpenAI клиенты
botreminder/models.py   # dataclass-модели intent-ов
botreminder/db.py       # SQLite, события, логи, обучение, расходы API
botreminder/parsing.py  # локальный и OpenAI-парсинг текста/голоса
botreminder/commands.py # контекстные команды к текущему событию
botreminder/keyboards.py# inline-кнопки Telegram
botreminder/views.py    # ответы пользователю и списки событий
botreminder/handlers.py # Telegram handlers
botreminder/scheduler.py# цикл напоминаний и эскалация
botreminder/main.py     # сборка приложения и запуск polling
```
