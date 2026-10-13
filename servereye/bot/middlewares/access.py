"""Шлюз доступа: пропускает к обработчикам только владельца и одобренных.

Стоит outer-middleware — до фильтров и роутеров, поэтому посторонний не
может вызвать ни одну команду или кнопку, даже подделав callback_data.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.types import CallbackQuery, Chat, Message, TelegramObject, User as TgUser

from ...database.database import session_scope
from ...database.models import (
    ACCESS_ACTIVE,
    ACCESS_BLOCKED,
    ACCESS_DENIED,
    ACCESS_PENDING,
    User,
)
from ...services import access
from ...services.notifier import Notifier
from .. import texts
from ..keyboards import inline as kb

log = logging.getLogger(__name__)

CLOSED_TEXT = (
    "🔒 <b>Закрытый бот</b>\n\n"
    "ServerEye доступен только по личному приглашению владельца."
)


def _start_payload(message: Message) -> str | None:
    text = (message.text or "").strip()
    if not text.startswith("/start"):
        return None
    parts = text.split(maxsplit=1)
    if parts[0] not in ("/start",) and not parts[0].startswith("/start@"):
        return None
    return parts[1].strip() if len(parts) > 1 else None


class AccessMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg: TgUser | None = data.get("event_from_user")
        chat: Chat | None = data.get("event_chat")
        if tg is None or tg.is_bot:
            return None

        # Только личные сообщения: в группе бот молчит
        if chat is not None and chat.type != "private":
            if isinstance(event, CallbackQuery):
                await event.answer()
            return None

        if access.is_owner(tg.id):
            return await handler(event, data)

        async with session_scope() as session:
            user = await session.get(User, tg.id)
            if user is not None and user.access_status == ACCESS_ACTIVE:
                allowed = True
            else:
                allowed = False
                await self._gate(event, tg, user, session, data["bot"])

        if allowed:
            return await handler(event, data)
        return None

    # ----------------------------------------------------------------------
    async def _gate(self, event, tg: TgUser, user: User | None, session, bot: Bot) -> None:
        status = user.access_status if user else None

        if isinstance(event, CallbackQuery):
            text = "⏳ Ожидает подтверждения владельца" if status == ACCESS_PENDING else "🔒 Нет доступа"
            await event.answer(text, show_alert=True)
            return
        if not isinstance(event, Message):
            return

        payload = _start_payload(event)

        if status == ACCESS_PENDING:
            await event.answer(
                "⏳ <b>Заявка на рассмотрении</b>\n\n"
                "Владелец ещё не подтвердил доступ. Как только подтвердит — я напишу."
            )
            return

        if payload is None:
            if status == ACCESS_BLOCKED:
                await event.answer("🚫 Доступ к боту закрыт.")
                if not await access.recently_logged(session, tg.id, access.EV_BLOCKED_TRY):
                    await access.log_tg(session, tg, access.EV_BLOCKED_TRY)
                    await self._alert(session, bot, texts.access_alert(tg, "blocked"),
                                      kb.access_alert_actions(tg.id, blocked=True))
                return
            if status == ACCESS_DENIED:
                await event.answer(
                    "❌ Владелец не подтвердил ваш доступ.\n"
                    "Если это ошибка — попросите новое приглашение."
                )
                return
            await event.answer(CLOSED_TEXT)
            if not access.is_locked(tg.id) and not await access.recently_logged(
                session, tg.id, access.EV_STRANGER
            ):
                await access.log_tg(session, tg, access.EV_STRANGER,
                                    detail=(event.text or "")[:60])
                await self._alert(session, bot, texts.access_alert(tg, "stranger"),
                                  kb.access_alert_actions(tg.id))
            return

        result = await access.redeem(session, tg, payload)
        await session.commit()
        await self._answer_redeem(event, tg, result, session, bot)

    async def _answer_redeem(self, message: Message, tg, result, session, bot) -> None:
        kind = result.kind
        if kind == access.R_JOINED:
            await message.answer(
                "👋 <b>Добро пожаловать в ServerEye!</b>\n\n"
                "Владелец выдал вам доступ. Серверы в боте общие: вам уже видны все, "
                "что добавил владелец, а те, что добавите вы, увидит и он.",
            )
            await message.answer(
                await texts.main_menu_for(session, tg.id, tg.first_name), reply_markup=kb.main_menu()
            )
            await self._alert(session, bot, texts.access_joined(result.user, result.invite),
                              kb.access_joined_actions(tg.id), type_="access_join")
        elif kind == access.R_REQUESTED:
            await message.answer(
                "📨 <b>Заявка отправлена</b>\n\n"
                "Приглашение принято. Владелец получил запрос и должен подтвердить доступ — "
                "как только он это сделает, я напишу."
            )
            await self._alert(session, bot, texts.access_request(result.user, result.invite),
                              kb.access_request_actions(tg.id), type_="access_request")
        elif kind == access.R_ALREADY:
            await message.answer("⏳ Это приглашение уже принято. Ожидайте решения владельца.")
        elif kind == access.R_REUSED:
            await message.answer(
                "⛔️ Это приглашение уже использовано.\n"
                "Приглашения одноразовые — попросите владельца выдать вам личное."
            )
            await self._alert(session, bot,
                              texts.access_reused(tg, result.invite, result.previous_user),
                              kb.access_alert_actions(tg.id))
        elif kind == access.R_EXPIRED:
            await message.answer("⌛️ Срок действия приглашения истёк или оно отозвано.")
            if result.notify_owner:
                await self._alert(session, bot, texts.access_alert(tg, "expired", result.invite),
                                  kb.access_alert_actions(tg.id))
        elif kind == access.R_BLOCKED:
            await message.answer("🚫 Доступ к боту закрыт.")
            if result.notify_owner:
                await self._alert(session, bot, texts.access_alert(tg, "blocked", result.invite),
                                  kb.access_alert_actions(tg.id, blocked=True))
        elif kind == access.R_LOCKED:
            if result.notify_owner:
                await message.answer(CLOSED_TEXT)
                await self._alert(session, bot, texts.access_alert(tg, "locked"),
                                  kb.access_alert_actions(tg.id))
            # после блокировки попыток молчим, чтобы не подсказывать перебор
        else:  # R_INVALID
            await message.answer(CLOSED_TEXT)
            if result.notify_owner:
                await self._alert(session, bot, texts.access_alert(tg, "invalid"),
                                  kb.access_alert_actions(tg.id))

    @staticmethod
    async def _alert(session, bot: Bot, text: str, markup, type_: str = "access_alert") -> None:
        notifier = Notifier(bot)
        for owner_id in await access.staff_ids(session):
            try:
                await notifier.send(
                    session, owner_id, text, type_=type_, severity="critical",
                    reply_markup=markup, force=True,
                )
            except Exception:  # noqa: BLE001
                log.exception("Не удалось уведомить владельца %s", owner_id)
        await session.commit()
