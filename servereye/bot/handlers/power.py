"""⏻ Выключение сервера, на котором работает бот."""
from __future__ import annotations

import logging
import time

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import repository as repo
from ...database.models import Server, User
from ...services import power
from ...utils import esc
from ..keyboards import inline as kb
from .helpers import edit

log = logging.getLogger(__name__)
router = Router(name="power")

CONFIRM_TTL = 30  # секунд на подтверждение


async def _authorize(call: CallbackQuery, session: AsyncSession, user: User) -> Server | None:
    """Все проверки перед показом или выполнением выключения."""
    parts = (call.data or "").split(":")
    if len(parts) < 3 or not parts[2].isdigit():
        await call.answer("Некорректный запрос", show_alert=True)
        return None

    server = await repo.get_server(session, int(parts[2]), user.id)
    if server is None:
        await call.answer("Сервер не найден", show_alert=True)
        return None
    if not server.is_local_host:
        await call.answer("Выключать можно только машину, на которой работает бот", show_alert=True)
        return None
    if not power.is_power_admin(user.id):
        log.warning("Отказ в выключении: пользователь %s не администратор", user.id)
        await call.answer("⛔️ Недостаточно прав", show_alert=True)
        return None
    ok, reason = power.power_control_available()
    if not ok:
        await call.answer(reason, show_alert=True)
        return None
    return server


@router.callback_query(F.data.startswith("pwr:ask:"))
async def ask_poweroff(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    server = await _authorize(call, session, user)
    if server is None:
        return
    stamp = int(time.time())
    await edit(
        call,
        "\n".join(
            [
                "⏻ <b>Выключение сервера</b>",
                "",
                f"Выключить <b>{esc(server.name)}</b>?",
                "",
                "⚠️ Вместе с ним остановится и этот бот.",
                "Включить ноутбук обратно можно только кнопкой питания на нём самом.",
                "",
                f"<i>Подтверждение действует {CONFIRM_TTL} секунд.</i>",
            ]
        ),
        kb.power_confirm(server.id, stamp),
    )
    await call.answer()


@router.callback_query(F.data.startswith("pwr:do:"))
async def do_poweroff(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    server = await _authorize(call, session, user)
    if server is None:
        return

    parts = call.data.split(":")
    stamp = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    if time.time() - stamp > CONFIRM_TTL:
        await call.answer("Время подтверждения истекло — нажмите ещё раз", show_alert=True)
        await edit(
            call,
            "⌛️ Подтверждение устарело, выключение отменено.",
            kb.back_main([("🖥 К серверу", f"srv:view:{server.id}")]),
        )
        return

    ok, reason = await power.poweroff(delay=5)
    if not ok:
        await call.answer(reason, show_alert=True)
        return

    log.warning("Пользователь %s выключает сервер %s", user.id, server.id)
    await repo.add_notification(
        session, user.id, "poweroff", f"Выключение {server.name} по команде из Telegram",
        server_id=server.id, severity="critical",
    )
    await session.commit()
    await call.answer("Выключаю")
    await edit(
        call,
        "\n".join(
            [
                "⏻ <b>Сервер выключается</b>",
                "",
                f"<b>{esc(server.name)}</b> выключится через 5 секунд.",
                "Бот станет недоступен, пока ноутбук не включат вручную.",
                "",
                "После включения ServerEye запустится сам.",
            ]
        ),
        None,
    )
