"""Пошаговое подключение сервера."""
from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import get_settings
from ...database import repository as repo
from ...database.database import session_scope
from ...database.models import (
    CHECK_HTTP,
    CHECK_PING,
    CHECK_PING_HTTP,
    CHECK_TCP,
    CHECK_TYPE_TITLES,
    User,
    UserSettings,
)
from ...services import pipeline
from ...utils import clean_name, esc, parse_ports, validate_host
from .. import texts
from ..keyboards import inline as kb
from .helpers import edit

log = logging.getLogger(__name__)
router = Router(name="add_server")
settings = get_settings()


class AddServer(StatesGroup):
    name = State()
    host = State()
    check_type = State()
    ports = State()
    agent = State()


ASK_NAME = (
    "➕ <b>Подключение сервера</b>\n\n"
    "<b>Шаг 1 из 4.</b> Как назвать сервер?\n"
    "Например: <code>Production Server</code>\n\n"
    "Отправьте название сообщением."
)


async def start_add(target: Message | CallbackQuery, session: AsyncSession, user: User, state: FSMContext) -> None:
    count = await repo.count_servers(session, user.id)
    if count >= repo.MAX_SERVERS_PER_USER:
        text = (
            f"⚠️ Достигнут лимит в {repo.MAX_SERVERS_PER_USER} серверов.\n"
            "Удалите ненужный сервер, чтобы добавить новый."
        )
        if isinstance(target, CallbackQuery):
            await edit(target, text, kb.back_main([("🖥 Мои серверы", "menu:servers")]))
            await target.answer()
        else:
            await target.answer(text, reply_markup=kb.back_main())
        return

    await state.set_state(AddServer.name)
    if isinstance(target, CallbackQuery):
        await edit(target, ASK_NAME, kb.cancel_only())
        await target.answer()
    else:
        await target.answer(ASK_NAME, reply_markup=kb.cancel_only())


@router.callback_query(F.data == "menu:add")
async def cb_add(call: CallbackQuery, session: AsyncSession, user: User, state: FSMContext) -> None:
    await start_add(call, session, user, state)


@router.message(Command("add"))
async def cmd_add(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    await start_add(message, session, user, state)


@router.callback_query(F.data == "add:cancel")
async def cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await edit(
        call,
        "Добавление сервера отменено.",
        kb.back_main([("🖥 Мои серверы", "menu:servers"), ("➕ Попробовать снова", "menu:add")]),
    )
    await call.answer()


@router.message(AddServer.name)
async def step_name(message: Message, state: FSMContext) -> None:
    name = clean_name(message.text or "")
    if len(name) < 2:
        await message.answer(
            "Название слишком короткое. Введите от 2 до 40 символов.",
            reply_markup=kb.cancel_only(),
        )
        return
    await state.update_data(name=name)
    await state.set_state(AddServer.host)
    await message.answer(
        f"✅ Название: <b>{esc(name)}</b>\n\n"
        "<b>Шаг 2 из 4.</b> Укажите IP-адрес или домен.\n"
        "Например: <code>93.184.216.34</code> или <code>example.com</code>",
        reply_markup=kb.cancel_only(),
    )


@router.message(AddServer.host)
async def step_host(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    ok, host, error = validate_host(message.text or "")
    if not ok:
        await message.answer(
            f"❌ {error}.\nПопробуйте ещё раз — нужен IP или домен, например <code>example.com</code>.",
            reply_markup=kb.cancel_only(),
        )
        return
    await state.update_data(host=host)
    await state.set_state(AddServer.check_type)
    duplicate = await repo.find_duplicate(session, user.id, host)
    if duplicate is not None:
        await message.answer(
            f"ℹ️ Этот адрес уже есть в общем списке: <b>{esc(duplicate.name)}</b>.\n"
            "Его видят все участники бота. Можно продолжить, если нужна ещё одна "
            "проверка того же сервера (например, другой порт), или нажать «Отмена»."
        )
    await message.answer(
        f"✅ Адрес: <code>{esc(host)}</code>\n\n"
        "<b>Шаг 3 из 4.</b> Как проверять сервер?\n\n"
        "🔹 <b>Ping</b> — быстрая проверка доступности по ICMP\n"
        "🔹 <b>HTTP / HTTPS</b> — проверка сайта или API (код ответа и время)\n"
        "🔹 <b>TCP-порт</b> — проверка конкретных портов, например 22, 80, 443\n"
        "🔹 <b>Ping + HTTP</b> — обе проверки сразу (рекомендуется для сайтов)",
        reply_markup=kb.check_types(),
    )


@router.callback_query(AddServer.check_type, F.data.startswith("add:type:"))
async def step_type(call: CallbackQuery, state: FSMContext) -> None:
    check_type = call.data.split(":")[2]
    await state.update_data(check_type=check_type)
    if check_type == CHECK_PING:
        await ask_agent(call, state)
        return
    await state.set_state(AddServer.ports)
    optional = check_type != CHECK_TCP
    hint = (
        "Укажите порты через запятую, например: <code>22, 80, 443</code>"
        if check_type == CHECK_TCP
        else "Укажите порт, если он нестандартный (например <code>8443</code>), "
        "или нажмите «Пропустить» — тогда проверим 443/80."
    )
    await edit(
        call,
        f"✅ Тип проверки: <b>{CHECK_TYPE_TITLES.get(check_type, check_type)}</b>\n\n"
        f"<b>Шаг 4 из 4.</b> {hint}",
        kb.ports_step(optional),
    )
    await call.answer()


@router.message(AddServer.ports)
async def step_ports(message: Message, state: FSMContext) -> None:
    ports, error = parse_ports(message.text or "")
    if error or not ports:
        await message.answer(
            f"❌ {error or 'Не удалось разобрать порты'}.\n"
            "Пример правильного ввода: <code>22, 80, 443</code>",
            reply_markup=kb.ports_step(True),
        )
        return
    await state.update_data(ports=ports)
    await ask_agent(message, state)


@router.callback_query(AddServer.ports, F.data == "add:skip_ports")
async def skip_ports(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(ports=[])
    await ask_agent(call, state)
    await call.answer()


async def ask_agent(target: Message | CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddServer.agent)
    text = (
        "🖥 <b>Мониторинг ресурсов</b>\n\n"
        "Ping, HTTP и TCP показывают только доступность. "
        "Чтобы видеть CPU, RAM, диск и сеть сервера, нужен небольшой агент, "
        "который устанавливается на сам сервер.\n\n"
        "Подключить мониторинг ресурсов? Токен и инструкцию выдам сразу — "
        "установить агент можно и позже."
    )
    if isinstance(target, CallbackQuery):
        await edit(target, text, kb.agent_choice())
    else:
        await target.answer(text, reply_markup=kb.agent_choice())


@router.callback_query(AddServer.agent, F.data.startswith("add:agent:"))
async def finish(
    call: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    user: User,
    user_settings: UserSettings,
) -> None:
    want_agent = call.data.split(":")[2] == "1"
    data = await state.get_data()
    await state.clear()

    name = data.get("name") or "Сервер"
    host = data.get("host")
    check_type = data.get("check_type", CHECK_PING)
    ports = data.get("ports") or []
    if not host:
        await edit(call, "Что-то пошло не так, начнём заново.", kb.back_main([("➕ Подключить", "menu:add")]))
        await call.answer()
        return

    http_url = None
    if check_type in (CHECK_HTTP, CHECK_PING_HTTP):
        from ...services.server_monitor import build_http_url

        http_url = build_http_url(host, ports[0] if ports else None)

    server = await repo.create_server(
        session,
        user_id=user.id,
        name=name,
        host=host,
        check_type=check_type,
        ports=ports,
        http_url=http_url,
        agent_enabled=want_agent,
    )
    await session.commit()

    lines = [
        "✅ <b>Сервер успешно подключен!</b>",
        "",
        f"🖥 <b>{esc(server.name)}</b>",
        f"Адрес: <code>{esc(server.host)}</code>",
        f"Проверка: {CHECK_TYPE_TITLES.get(check_type, check_type)}",
    ]
    if ports:
        lines.append(f"Порты: {', '.join(str(p) for p in ports)}")
    lines += [
        "",
        "Сервер появился в общем списке — его видят все участники бота.",
        f"ServerEye начинает мониторинг автоматически — "
        f"проверка каждые {settings.server_check_interval_minutes} мин.",
    ]
    if want_agent:
        lines += [
            "",
            "🔐 <b>Agent</b>",
            "Токен для мониторинга ресурсов:",
            f"<code>{esc(server.agent_token or '')}</code>",
            "Инструкция по установке — кнопка ниже.",
        ]
    await edit(call, "\n".join(lines), kb.after_add(server.id, want_agent))
    await call.answer("Сервер добавлен")

    asyncio.create_task(_first_check(server.id, user.id))


async def _first_check(server_id: int, user_id: int) -> None:
    """Первая проверка сразу после добавления — чтобы карточка не пустовала."""
    await asyncio.sleep(1)
    try:
        async with session_scope() as session:
            server = await repo.get_server(session, server_id, user_id)
            if server is None:
                return
            st = await repo.get_settings_for(session, user_id)
            await pipeline.check_single(session, server, st, notifier=None)
    except Exception:  # noqa: BLE001
        log.exception("Первая проверка сервера %s не удалась", server_id)
