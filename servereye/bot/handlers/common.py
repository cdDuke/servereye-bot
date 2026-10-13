"""Главное меню, /start, состояние системы, общая статистика."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import repository as repo
from ...database.models import (
    STATUS_OFFLINE,
    STATUS_ONLINE,
    STATUS_WARNING,
    Server,
    User,
    UserSettings,
)
from ...services import access
from ...services import charts as charts_service
from ...utils import utcnow
from .. import texts
from ..keyboards import inline as kb
from .helpers import edit

log = logging.getLogger(__name__)
router = Router(name="common")


async def summary(session: AsyncSession, user_id: int) -> tuple[int, int, int, int, list[Server]]:
    servers = await repo.list_servers(session, user_id)
    online = sum(1 for s in servers if s.status == STATUS_ONLINE)
    problems = sum(1 for s in servers if s.status == STATUS_WARNING)
    offline = sum(1 for s in servers if s.status == STATUS_OFFLINE)
    return len(servers), online, problems, offline, servers


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    session: AsyncSession,
    user: User,
    state: FSMContext,
    command: CommandObject | None = None,
) -> None:
    await state.clear()
    if command is not None and command.args:
        # Сюда попадают только те, у кого доступ уже есть: ссылка не тратится
        if access.is_staff(user):
            await message.answer(
                "🔗 Это ссылка-приглашение. Вы уже в боте, поэтому она <b>не израсходована</b> "
                "и по-прежнему сработает один раз для того, кому вы её отправите."
            )
        else:
            await message.answer("Вы уже в боте — приглашение не понадобилось и не израсходовано.")
    total, online, problems, offline, _ = await summary(session, user.id)
    await message.answer(
        texts.main_menu(user.first_name, total, online, problems, offline),
        reply_markup=kb.main_menu(access.is_staff(user)),
        disable_web_page_preview=True,
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    await cmd_start(message, session, user, state, None)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "\n".join(
            [
                f"{texts.BRAND}",
                texts.SUBTITLE,
                "",
                "<b>Команды</b>",
                "/start — главное меню",
                "/servers — список серверов",
                "/add — подключить сервер",
                "/check — срочная проверка всех серверов",
                "/report — отчёт за час прямо сейчас",
                "/status — состояние системы",
                "/admin — админ-панель (владелец и админы)",
                "/help — эта справка",
                "",
                "<b>Что умеет ServerEye</b>",
                "• Проверяет серверы по Ping, HTTP/HTTPS и TCP-портам",
                "• Считает uptime, средний, минимальный и максимальный ping",
                "• Фиксирует потери пакетов, ошибки и эпизоды недоступности",
                "• Сравнивает показатели с историей и предупреждает об ухудшении",
                "• Строит графики Ping и Uptime за 24 часа, 7 и 30 дней",
                "• Присылает уведомления, когда сервер упал или восстановился",
                "",
                "Все данные привязаны к вашему Telegram-аккаунту и не видны другим.",
                "Бот закрытый: войти можно только по личному приглашению владельца.",
                "Серверы общие для всех участников: менять и удалять сервер может "
                "тот, кто его добавил, админы и владелец.",
            ]
        ),
        reply_markup=kb.back_main(),
    )


@router.message(Command("status"))
async def cmd_status(message: Message, session: AsyncSession, user: User) -> None:
    text = await build_system_state(session, user.id)
    await message.answer(text, reply_markup=kb.back_main())


@router.callback_query(F.data == "menu:main")
async def show_main(call: CallbackQuery, session: AsyncSession, user: User, state: FSMContext) -> None:
    await state.clear()
    total, online, problems, offline, _ = await summary(session, user.id)
    await edit(
        call,
        texts.main_menu(user.first_name, total, online, problems, offline),
        kb.main_menu(access.is_staff(user)),
    )
    await call.answer()


@router.callback_query(F.data == "menu:stats")
async def show_stats_menu(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    total, online, problems, offline, servers = await summary(session, user.id)
    if not servers:
        text = (
            "📊 <b>Статистика</b>\n\n"
            "Серверы пока не подключены.\n"
            "Нажмите «➕ Подключить сервер» — и здесь появятся uptime, "
            "средний ping и история проверок."
        )
    else:
        text = (
            "📊 <b>Статистика</b>\n\n"
            f"Подключено серверов: {total}\n"
            f"🟢 {online} · 🟡 {problems} · 🔴 {offline}\n\n"
            "Выберите сервер, чтобы посмотреть подробные показатели:"
        )
    await edit(call, text, kb.stats_menu(servers))
    await call.answer()


async def build_system_state(session: AsyncSession, user_id: int) -> str:
    total, online, problems, offline, servers = await summary(session, user_id)

    uptimes: list[float] = []
    pings: list[float] = []
    incidents = 0
    for server in servers:
        stats = await repo.server_stats(session, server.id, hours=24)
        if stats.uptime is not None:
            uptimes.append(stats.uptime)
        if stats.ping_avg is not None:
            pings.append(stats.ping_avg)
        incidents += stats.outages

    agents_total = sum(1 for s in servers if s.agent_enabled)
    agents_ok = sum(1 for s in servers if texts.agent_state(s)[0])

    return texts.system_state(
        total,
        online,
        problems,
        offline,
        sum(uptimes) / len(uptimes) if uptimes else None,
        incidents,
        sum(pings) / len(pings) if pings else None,
        agents_ok,
        agents_total,
    )


@router.callback_query(F.data == "menu:system")
async def show_system(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    text = await build_system_state(session, user.id)
    await edit(
        call,
        text,
        kb.back_main([("🔄 Обновить", "menu:system"), ("⚡ Срочная проверка", "menu:urgent")]),
    )
    await call.answer()


@router.callback_query(F.data == "noop")
async def noop(call: CallbackQuery) -> None:
    await call.answer()


async def _report_text(session: AsyncSession, user_id: int) -> str:
    from ...scheduler.tasks import build_hourly_report

    text = await build_hourly_report(session, user_id)
    if text is None:
        return (
            "📊 <b>Отчёт за час</b>\n\n"
            "Пока нечего показать: нет ни серверов, ни данных об интернете.\n"
            "Подключите сервер или откройте раздел 🌐 Интернет."
        )
    return text


@router.callback_query(F.data == "menu:report")
async def show_report(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    await edit(
        call,
        await _report_text(session, user.id),
        kb.back_main([("🔄 Обновить", "menu:report"), ("📊 Статистика", "menu:stats")]),
    )
    await call.answer()


@router.message(Command("report"))
async def cmd_report(message: Message, session: AsyncSession, user: User) -> None:
    await message.answer(
        await _report_text(session, user.id),
        reply_markup=kb.back_main([("📊 Статистика", "menu:stats")]),
    )

# Отдельный роутер-«ловушка»: подключается последним и ловит всё, что не разобрали
# другие хендлеры — иначе кнопка из старого сообщения оставляет вечные часики.
fallback_router = Router(name="fallback")


@fallback_router.callback_query()
async def unknown_callback(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    log.debug("Необработанный callback: %s", call.data)
    await call.answer("Кнопка устарела — открываю главное меню", show_alert=False)
    total, online, problems, offline, _ = await summary(session, user.id)
    await edit(
        call,
        texts.main_menu(user.first_name, total, online, problems, offline),
        kb.main_menu(access.is_staff(user)),
    )


@fallback_router.message()
async def unknown_message(message: Message, session: AsyncSession, user: User) -> None:
    total, online, problems, offline, _ = await summary(session, user.id)
    await message.answer(
        "Не понял команду 🤔\n\nПользуйтесь кнопками меню или отправьте /help.\n\n"
        + texts.main_menu(user.first_name, total, online, problems, offline),
        reply_markup=kb.main_menu(access.is_staff(user)),
        disable_web_page_preview=True,
    )
