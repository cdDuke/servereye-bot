"""Разделы «🔔 Уведомления» и «⚙️ Настройки»."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import repository as repo
from ...database.models import Notification, Server, User, UserSettings
from .. import texts
from ..keyboards import inline as kb
from ..keyboards.inline import NOTIFY_TOGGLES
from .helpers import edit

log = logging.getLogger(__name__)
router = Router(name="settings")

VALID_TOGGLES = {key for key, _ in NOTIFY_TOGGLES}
THRESHOLD_LIMITS = {
    "ping_warning_ms": (1, 100000, "ms", "Уведомлять, если Ping выше"),
    "ping_critical_ms": (1, 100000, "ms", "Критический порог Ping"),
    "packet_loss_pct": (1, 100, "%", "Уведомлять, если потери пакетов выше"),
}
SETTINGS_TOGGLES = {"hourly_report", "daily_report"}


class SettingsEdit(StatesGroup):
    threshold = State()


@router.callback_query(F.data == "menu:notif")
async def notifications_page(call: CallbackQuery, user_settings: UserSettings) -> None:
    await edit(call, texts.notifications_settings(user_settings), kb.notifications(user_settings))
    await call.answer()


@router.callback_query(F.data.startswith("nt:toggle:"))
async def toggle(
    call: CallbackQuery, session: AsyncSession, user: User, user_settings: UserSettings
) -> None:
    key = call.data.split(":")[2]
    if key not in VALID_TOGGLES:
        await call.answer("Неизвестная настройка", show_alert=True)
        return
    value = await repo.toggle_setting(session, user.id, key)
    await session.commit()
    await call.answer("Включено ✅" if value else "Выключено ⛔️")
    await edit(call, texts.notifications_settings(user_settings), kb.notifications(user_settings))


@router.callback_query(F.data.startswith("nt:thr:"))
async def ask_threshold(call: CallbackQuery, state: FSMContext, user_settings: UserSettings) -> None:
    key = call.data.split(":")[2]
    if key not in THRESHOLD_LIMITS:
        await call.answer("Неизвестный порог", show_alert=True)
        return
    low, high, unit, title = THRESHOLD_LIMITS[key]
    current = getattr(user_settings, key)
    await state.set_state(SettingsEdit.threshold)
    await state.update_data(key=key)
    await edit(
        call,
        f"🎯 <b>{title}</b>\n\n"
        f"Текущее значение: <b>{current:g} {unit}</b>\n\n"
        f"Отправьте новое значение числом (от {low} до {high}).",
        kb.cancel_input("menu:notif"),
    )
    await call.answer()


@router.message(SettingsEdit.threshold)
async def save_threshold(
    message: Message, state: FSMContext, session: AsyncSession, user: User
) -> None:
    data = await state.get_data()
    key = data.get("key")
    if key not in THRESHOLD_LIMITS:
        await state.clear()
        return
    low, high, unit, title = THRESHOLD_LIMITS[key]
    raw = (message.text or "").strip().replace(",", ".")
    for token in ("ms", "%", " "):
        raw = raw.lower().replace(token, "")
    try:
        value = float(raw)
    except ValueError:
        await message.answer(f"Нужно число от {low} до {high}. Попробуйте ещё раз.")
        return
    if not low <= value <= high:
        await message.answer(f"Значение должно быть от {low} до {high}.")
        return
    st = await repo.update_settings(session, user.id, **{key: value})
    await session.commit()
    await state.clear()
    await message.answer(
        f"✅ {title}: <b>{value:g} {unit}</b>",
        reply_markup=kb.notifications(st),
    )


@router.callback_query(F.data == "menu:settings")
async def settings_page(
    call: CallbackQuery, session: AsyncSession, user: User, user_settings: UserSettings
) -> None:
    count = await repo.count_servers(session, user.id)
    await edit(call, texts.settings_page(user_settings, count), kb.settings_menu(user_settings))
    await call.answer()


@router.callback_query(F.data.startswith("st:toggle:"))
async def toggle_setting(
    call: CallbackQuery, session: AsyncSession, user: User, user_settings: UserSettings
) -> None:
    key = call.data.split(":")[2]
    if key not in SETTINGS_TOGGLES:
        await call.answer("Неизвестная настройка", show_alert=True)
        return
    value = await repo.toggle_setting(session, user.id, key)
    await session.commit()
    await call.answer("Включено ✅" if value else "Выключено ⛔️")
    count = await repo.count_servers(session, user.id)
    await edit(call, texts.settings_page(user_settings, count), kb.settings_menu(user_settings))


@router.callback_query(F.data == "st:tz")
async def timezone_menu(call: CallbackQuery) -> None:
    await edit(
        call,
        "🕒 <b>Часовой пояс</b>\n\n"
        "Выберите смещение относительно UTC — время в отчётах и истории "
        "будет показано в нём.",
        kb.tz_menu(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("st:tzset:"))
async def timezone_set(
    call: CallbackQuery, session: AsyncSession, user: User, user_settings: UserSettings
) -> None:
    offset = int(call.data.split(":")[2])
    await repo.update_settings(session, user.id, tz_offset=offset)
    await session.commit()
    await call.answer(f"Часовой пояс: UTC{offset:+d}")
    count = await repo.count_servers(session, user.id)
    await edit(call, texts.settings_page(user_settings, count), kb.settings_menu(user_settings))


@router.callback_query(F.data == "st:wipe")
async def ask_wipe(call: CallbackQuery) -> None:
    await edit(
        call,
        "🧹 <b>Удаление данных</b>\n\n"
        "Будут удалены все ваши серверы, история проверок и уведомления. "
        "Настройки останутся.\n\n"
        "Действие необратимо. Продолжить?",
        kb.confirm_wipe(),
    )
    await call.answer()


@router.callback_query(F.data == "st:wipeok")
async def do_wipe(
    call: CallbackQuery, session: AsyncSession, user: User, user_settings: UserSettings
) -> None:
    await session.execute(delete(Server).where(Server.user_id == user.id))
    await session.execute(delete(Notification).where(Notification.user_id == user.id))
    await session.commit()
    await call.answer("Данные удалены")
    await edit(
        call,
        "🧹 Все данные удалены.\n\nМожно начать с чистого листа.",
        kb.back_main([("➕ Подключить сервер", "menu:add")]),
    )
