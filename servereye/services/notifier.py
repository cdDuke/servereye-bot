"""Отправка уведомлений в Telegram с учётом настроек пользователя."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import repository as repo
from ..database.models import Server, UserSettings
from ..utils import utcnow

log = logging.getLogger(__name__)

# как часто можно повторять один и тот же тип уведомления по серверу
COOLDOWN = {
    "high_ping": timedelta(minutes=45),
    "packet_loss": timedelta(minutes=45),
    "cpu": timedelta(minutes=30),
    "ram": timedelta(minutes=30),
    "disk": timedelta(hours=6),
    "trend": timedelta(hours=3),
    "agent_offline": timedelta(hours=1),
    "speed_drop": timedelta(hours=2),
}

CRITICAL_TYPES = {
    "server_down", "server_up", "cpu", "ram", "disk",
    "temp_alarm", "temp_ok", "power_lost", "power_restored", "battery_low",
    "access_join", "access_request", "access_alert",
}
# Эти события стоит доставить даже с опозданием: при отключении света
# роутер часто тоже гаснет, и первое сообщение не уходит
RETRY_TYPES = {
    "server_down", "power_lost", "power_restored", "battery_low", "temp_alarm",
    "access_join", "access_request", "access_alert",
}


# Какая настройка пользователя отвечает за тип события
SETTING_FOR_TYPE = {
    "server_down": "notify_server_down",
    "server_up": "notify_server_up",
    "high_ping": "notify_high_ping",
    "packet_loss": "notify_packet_loss",
    "cpu": "notify_cpu",
    "ram": "notify_ram",
    "disk": "notify_disk",
    "agent_offline": "notify_agent_offline",
    "temp_alarm": "notify_temp",
    "temp_ok": "notify_temp",
    "power_lost": "notify_power",
    "power_restored": "notify_power",
    "battery_low": "notify_power",
    "ping_degraded": "notify_trend",
    "ping_improved": "notify_trend",
    "unstable": "notify_trend",
    "resource_trend": "notify_trend",
}


class Notifier:
    """Тонкая обёртка над Bot: ретраи, тихие часы, запись в журнал."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    @staticmethod
    def _in_quiet_hours(st: UserSettings) -> bool:
        if st.quiet_hours_from is None or st.quiet_hours_to is None:
            return False
        hour = (utcnow() + timedelta(hours=st.tz_offset)).hour
        start, end = st.quiet_hours_from, st.quiet_hours_to
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    async def send(
        self,
        session: AsyncSession,
        user_id: int,
        text: str,
        *,
        type_: str = "info",
        server_id: int | None = None,
        severity: str = "info",
        settings_obj: UserSettings | None = None,
        reply_markup=None,
        force: bool = False,
    ) -> bool:
        st = settings_obj or await repo.get_settings_for(session, user_id)
        if not force and self._in_quiet_hours(st) and type_ not in CRITICAL_TYPES:
            log.debug("Тихие часы: уведомление %s для %s отложено", type_, user_id)
            await repo.add_notification(
                session, user_id, type_, text, server_id, severity, delivered=False
            )
            return False

        delivered = await self._deliver(user_id, text, reply_markup)
        await repo.add_notification(
            session, user_id, type_, text, server_id, severity, delivered=delivered
        )
        if not delivered:
            log.info("Уведомление %s пользователю %s не доставлено", type_, user_id)
        return delivered

    async def send_server(
        self,
        session: AsyncSession,
        server: Server,
        text: str,
        *,
        type_: str,
        severity: str = "info",
        reply_markup=None,
    ) -> int:
        """Тревога о сервере — всем участникам бота.

        Серверы общие, поэтому о каждом сообщаем каждому, у кого есть доступ.
        Каждый получатель фильтруется по своим настройкам и тихим часам.
        reply_markup может быть функцией от user_id — например, чтобы кнопку
        выключения видел только владелец.
        """
        if not server.notifications_enabled:
            return 0
        key = SETTING_FOR_TYPE.get(type_)
        sent = 0
        for user in await repo.all_users(session):
            st = await repo.get_settings_for(session, user.id)
            if key and not getattr(st, key, True):
                continue
            markup = reply_markup(user.id) if callable(reply_markup) else reply_markup
            if await self.send(
                session, user.id, text,
                type_=type_, server_id=server.id, severity=severity,
                settings_obj=st, reply_markup=markup,
            ):
                sent += 1
        return sent

    async def _deliver(self, user_id: int, text: str, reply_markup=None, attempt: int = 0) -> bool:
        try:
            await self.bot.send_message(
                user_id,
                text,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            return True
        except TelegramRetryAfter as exc:
            if attempt >= 2:
                return False
            await asyncio.sleep(exc.retry_after + 1)
            return await self._deliver(user_id, text, reply_markup, attempt + 1)
        except TelegramForbiddenError:
            log.info("Пользователь %s заблокировал бота — отключаю рассылку", user_id)
            return False
        except TelegramBadRequest as exc:
            log.warning("Не удалось отправить уведомление %s: %s", user_id, exc.message)
            return False
        except TelegramNetworkError:
            log.warning("Нет связи с Telegram — уведомление будет доставлено позже")
            return False
        except Exception:  # noqa: BLE001
            log.exception("Сбой отправки уведомления пользователю %s", user_id)
            return False

    async def retry_undelivered(self, session: AsyncSession) -> int:
        """Повторная отправка важных событий, которые не ушли из-за сети."""
        from ..utils import fmt_time

        pending = await repo.undelivered_notifications(
            session, RETRY_TYPES, max_age_hours=12, max_attempts=240
        )
        sent = 0
        for item in pending:
            st = await repo.get_settings_for(session, item.user_id)
            text = (
                f"⏳ <i>Доставлено с задержкой · событие в "
                f"{fmt_time(item.created_at, st.tz_offset)}</i>\n\n{item.message}"
            )
            item.attempts += 1
            if await self._deliver(item.user_id, text):
                item.delivered = True
                sent += 1
            else:
                break  # сети всё ещё нет — не тратим попытки на остальные
        await session.flush()
        return sent

    async def send_photo(
        self, user_id: int, photo, caption: str | None = None, reply_markup=None
    ) -> bool:
        try:
            await self.bot.send_photo(
                user_id, photo, caption=caption, reply_markup=reply_markup
            )
            return True
        except Exception:  # noqa: BLE001
            log.exception("Не удалось отправить график пользователю %s", user_id)
            return False


def cooldown_passed(last: object, key: str) -> bool:
    if last is None:
        return True
    window = COOLDOWN.get(key, timedelta(hours=1))
    return (utcnow() - last) >= window  # type: ignore[operator]


def server_notifications_allowed(server: Server, st: UserSettings, key: str) -> bool:
    if not server.notifications_enabled:
        return False
    return bool(getattr(st, key, True))
