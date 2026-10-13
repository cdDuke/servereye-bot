"""Точка входа ServerEye."""
from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat

from .api.agent_api import AgentAPIServer
from .bot.handlers import (
    add_server,
    admin,
    common,
    power,
    servers,
    settings as settings_handlers,
    urgent,
)
from .bot.keyboards.inline import main_menu as main_menu_kb
from .bot.middlewares.access import AccessMiddleware
from .bot.middlewares.db import DbSessionMiddleware, ThrottlingMiddleware
from .config import get_settings
from .database.database import close_db, init_db, session_scope
from .services import access
from .scheduler.tasks import run_startup_jobs, setup_scheduler

log = logging.getLogger("servereye")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    for noisy in ("aiogram.event", "apscheduler.executors.default", "httpx", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


COMMANDS = [
    BotCommand(command="start", description="🛡 Главное меню"),
    BotCommand(command="servers", description="🖥 Мои серверы"),
    BotCommand(command="add", description="➕ Подключить сервер"),
    BotCommand(command="check", description="⚡ Срочная проверка"),
    BotCommand(command="report", description="📊 Отчёт за час"),
    BotCommand(command="status", description="🧠 Состояние системы"),
    BotCommand(command="help", description="❓ Справка"),
]


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

    # Шлюз доступа — первым, до фильтров и любых обработчиков
    dp.message.outer_middleware(AccessMiddleware())
    dp.callback_query.outer_middleware(AccessMiddleware())

    dp.message.middleware(ThrottlingMiddleware(rate=0.3))
    dp.callback_query.middleware(ThrottlingMiddleware(rate=0.3))
    dp.message.middleware(DbSessionMiddleware())
    dp.callback_query.middleware(DbSessionMiddleware())

    dp.include_router(common.router)
    dp.include_router(add_server.router)
    dp.include_router(servers.router)
    dp.include_router(urgent.router)
    dp.include_router(power.router)
    dp.include_router(settings_handlers.router)
    dp.include_router(admin.router)
    dp.include_router(common.fallback_router)  # всегда последним
    return dp


async def notify_new_owners(bot: Bot) -> None:
    """Сообщает новым совладельцам о полных правах (один раз)."""
    async with session_scope() as session:
        for event in await access.pending_owner_notices(session):
            log.info("Новый совладелец: %s", event.tg_id)
            try:
                await bot.send_message(
                    event.tg_id,
                    "👑 <b>Вам выданы полные права</b>\n\n"
                    "Теперь вы совладелец ServerEye: админ-панель, приглашения, выдача админки, "
                    "управление всеми серверами и кнопка «⏻ Выключить сервер» у домашнего сервера.",
                    reply_markup=main_menu_kb(True),
                )
                event.detail = access.NOTICE_SENT
            except Exception:  # noqa: BLE001
                log.info("Не удалось уведомить нового совладельца %s — попробую при следующем старте", event.tg_id)


async def main() -> None:
    config = get_settings()
    setup_logging(config.log_level)
    log.info("Запуск ServerEye…")

    if not config.admin_ids:
        log.critical(
            "ADMIN_IDS не задан: бот закрытый, и без владельца в него не сможет войти никто. "
            "Укажите свой Telegram ID в .env (ADMIN_IDS=123456789)."
        )
        raise SystemExit(2)

    await init_db()
    async with session_scope() as session:
        await access.ensure_owners(session)
    log.info("Доступ: закрытый бот, владельцы %s", ", ".join(map(str, config.admin_ids)))

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    dp = build_dispatcher()

    me = await bot.get_me()
    log.info("Бот @%s (%s) авторизован", me.username, me.id)
    await bot.set_my_commands(COMMANDS)
    for owner_id in config.admin_ids:
        try:
            await bot.set_my_commands(
                COMMANDS + [BotCommand(command="admin", description="👑 Админ-панель")],
                scope=BotCommandScopeChat(chat_id=owner_id),
            )
        except Exception:  # noqa: BLE001 — владелец мог ещё не писать боту
            log.info("Команды владельца %s не установлены (он ещё не открывал бота)", owner_id)

    await notify_new_owners(bot)

    api = AgentAPIServer(bot)
    await api.start()

    scheduler = setup_scheduler(bot)
    scheduler.start()
    log.info(
        "Планировщик запущен: проверка серверов каждые %s мин",
        config.server_check_interval_minutes,
    )
    startup_task = asyncio.create_task(run_startup_jobs(bot))

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        log.info("Остановка ServerEye…")
        startup_task.cancel()
        scheduler.shutdown(wait=False)
        await api.stop()
        await bot.session.close()
        await close_db()


def run() -> None:
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("ServerEye остановлен")


if __name__ == "__main__":
    run()
