"""⚡ Срочная проверка — немедленный опрос серверов без ожидания планировщика."""
from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import repository as repo
from ...database.models import STATUS_OFFLINE, STATUS_ONLINE, STATUS_WARNING, User, UserSettings
from ...services import pipeline
from ...services.notifier import Notifier
from ...services.server_monitor import CheckResult
from ...utils import esc
from .. import texts
from ..keyboards import inline as kb
from .helpers import edit

log = logging.getLogger(__name__)
router = Router(name="urgent")

_running: set[int] = set()


async def run_urgent(
    session: AsyncSession,
    user: User,
    user_settings: UserSettings,
    bot,
    render,
) -> str:
    """Проверяет все серверы пользователя, обновляя прогресс через render()."""
    servers = await repo.list_servers(session, user.id)
    if not servers:
        return (
            "⚡ <b>Срочная проверка</b>\n\n"
            "Нет подключённых серверов.\n"
            "Нажмите «➕ Подключить сервер», чтобы начать."
        )

    names = [s.name for s in servers]
    await render(texts.urgent_progress(names))

    sink: dict[int, CheckResult] = {}
    task = asyncio.create_task(
        pipeline.check_servers(
            session,
            servers,
            {user.id: user_settings},
            notifier=Notifier(bot),
            sink=sink,
        )
    )

    last_snapshot: dict[str, str] = {}
    while not task.done():
        await asyncio.sleep(1.5)
        progress = {
            s.name: (
                "✅"
                if sink.get(s.id) and sink[s.id].status == STATUS_ONLINE
                else "⚠️"
                if sink.get(s.id) and sink[s.id].status == STATUS_WARNING
                else "❌"
                if sink.get(s.id)
                else "⏳"
            )
            for s in servers
        }
        if progress != last_snapshot:
            last_snapshot = progress
            try:
                await render(texts.urgent_progress(names, progress))
            except Exception:  # noqa: BLE001
                pass

    try:
        results = await task
    except Exception:  # noqa: BLE001
        log.exception("Срочная проверка упала")
        return "❌ Не удалось выполнить проверку. Попробуйте ещё раз через минуту."

    blocks = []
    online = problems = offline = 0
    for server in servers:
        result = results.get(server.id)
        if result is None:
            continue
        metric = await repo.latest_metric(session, server.id) if server.agent_enabled else None
        blocks.append(texts.server_check_block(server, result, metric))
        if result.status == STATUS_ONLINE:
            online += 1
        elif result.status == STATUS_WARNING:
            problems += 1
        else:
            offline += 1

    return texts.urgent_result(blocks, online, problems, offline)


@router.callback_query(F.data == "menu:urgent")
async def urgent_callback(
    call: CallbackQuery, session: AsyncSession, user: User, user_settings: UserSettings
) -> None:
    if user.id in _running:
        await call.answer("Проверка уже выполняется", show_alert=True)
        return
    _running.add(user.id)
    await call.answer("Запускаю проверку")

    async def render(text: str) -> None:
        await edit(call, text, None)

    try:
        text = await run_urgent(session, user, user_settings, call.bot, render)
        await edit(
            call,
            text,
            kb.back_main(
                [("⚡ Проверить снова", "menu:urgent"), ("🖥 Мои серверы", "menu:servers")]
            ),
        )
    finally:
        _running.discard(user.id)


@router.message(Command("check"))
async def urgent_command(
    message: Message, session: AsyncSession, user: User, user_settings: UserSettings
) -> None:
    if user.id in _running:
        await message.answer("Проверка уже выполняется, подождите немного.")
        return
    _running.add(user.id)
    sent = await message.answer("⏳ Проверяю серверы...")

    async def render(text: str) -> None:
        try:
            await sent.edit_text(text)
        except Exception:  # noqa: BLE001
            pass

    try:
        text = await run_urgent(session, user, user_settings, message.bot, render)
        await sent.edit_text(
            text,
            reply_markup=kb.back_main(
                [("⚡ Проверить снова", "menu:urgent"), ("🖥 Мои серверы", "menu:servers")]
            ),
        )
    finally:
        _running.discard(user.id)
