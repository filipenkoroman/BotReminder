import asyncio

from . import handlers  # noqa: F401 - registers aiogram handlers
from .config import TELEGRAM_BOT_TOKEN, bot, dp
from .db import init_db
from .scheduler import scheduler_loop


async def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Нужен TELEGRAM_BOT_TOKEN в .env. Его дает @BotFather в Telegram.")
    await init_db()
    asyncio.create_task(scheduler_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
