"""Entry point: Telegram bot (long polling), the reminder loop and the mini-app web server in one process."""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo
from aiohttp import web

import db
import planner
from bot import router, send
from config import config
from web import build_app


async def reminders(bot: Bot) -> None:
    """Once a minute: exact-time reminders, and after MORNING_HOUR the digest and nudges."""
    while True:
        try:
            await send(bot, planner.tick(db.local_now()))
        except Exception:
            logging.exception("reminder tick failed")
        await asyncio.sleep(60)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if not config.bot_token:
        raise SystemExit("Не задан BOT_TOKEN")
    if not config.ai_enabled:
        logging.warning("YANDEX_API_KEY/YANDEX_FOLDER_ID не заданы: голос выключен, фразы разбираются простыми правилами")
    db.init()

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    runner = web.AppRunner(build_app(bot))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", config.port).start()
    if config.webapp_url:
        try:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Планер", web_app=WebAppInfo(url=f"{config.webapp_url}/tg")))
        except Exception as e:  # a mistyped WEBAPP_URL must not stop the chat bot itself
            logging.error("WEBAPP_URL %r не подошёл, кнопка мини-приложения не включена: %s", config.webapp_url, e)
    await bot.set_my_commands([
        BotCommand(command="list", description="Дела"),
        BotCommand(command="buy", description="Покупки"),
        BotCommand(command="projects", description="Проекты"),
        BotCommand(command="season", description="Счёт: семья против хаоса"),
        BotCommand(command="reminders", description="Тон напоминаний"),
        BotCommand(command="busy", description="Занятые дни"),
        BotCommand(command="invite", description="Позвать пару"),
        BotCommand(command="help", description="Как пользоваться"),
        BotCommand(command="privacy", description="Какие данные хранятся"),
        BotCommand(command="delete", description="Удалить мои данные"),
    ])
    loop = asyncio.create_task(reminders(bot))
    dp = Dispatcher()
    dp.include_router(router)
    try:
        await dp.start_polling(bot)
    finally:
        loop.cancel()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
