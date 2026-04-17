import asyncio

from aiogram.types import BotCommand

from . import handlers  # noqa: F401 - registers aiogram handlers
from .config import TELEGRAM_BOT_TOKEN, bot, dp
from .db import init_db
from .scheduler import scheduler_loop


async def set_bot_commands() -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="help", description="Что умеет бот"),
            BotCommand(command="today", description="Показать сегодня"),
            BotCommand(command="week", description="Показать неделю"),
            BotCommand(command="month", description="Показать месяц"),
            BotCommand(command="list", description="Все активные"),
            BotCommand(command="hot", description="Что горит"),
            BotCommand(command="cost", description="Расходы API"),
            BotCommand(command="logs", description="Логи бота"),
            BotCommand(command="learned", description="Кейсы обучения"),
        ]
    )


async def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Нужен TELEGRAM_BOT_TOKEN в .env. Его дает @BotFather в Telegram.")
    await init_db()
    await set_bot_commands()
    asyncio.create_task(scheduler_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
