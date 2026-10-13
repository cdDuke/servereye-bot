"""Middleware: сессия БД, пользователь и его настройки в каждом апдейте."""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User as TgUser

from ...database import repository as repo
from ...database.database import session_scope

log = logging.getLogger(__name__)


class DbSessionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is None or tg_user.is_bot:
            return await handler(event, data)

        async with session_scope() as session:
            user = await repo.get_or_create_user(
                session,
                tg_user.id,
                username=tg_user.username,
                first_name=tg_user.first_name,
                language_code=tg_user.language_code,
            )
            settings_obj = await repo.get_settings_for(session, user.id)
            data["session"] = session
            data["user"] = user
            data["user_settings"] = settings_obj
            return await handler(event, data)


class ThrottlingMiddleware(BaseMiddleware):
    """Простейшая защита от «залипшей» кнопки."""

    def __init__(self, rate: float = 0.4) -> None:
        self.rate = rate
        self._last: dict[int, float] = {}

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is not None:
            now = time.monotonic()
            previous = self._last.get(tg_user.id, 0.0)
            if now - previous < self.rate:
                return None
            self._last[tg_user.id] = now
            if len(self._last) > 10_000:
                self._last.clear()
        return await handler(event, data)
