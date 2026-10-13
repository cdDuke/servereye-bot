"""Выключение машины, на которой работает бот.

Сознательно узкий модуль: одна фиксированная команда, никаких аргументов
от пользователя. Права выдаются правилом polkit только системному
пользователю servereye и только на выключение (см. deploy/).
"""
from __future__ import annotations

import asyncio
import logging
import platform
import shutil

from ..config import get_settings

log = logging.getLogger(__name__)
settings = get_settings()

POWEROFF_CMD = ("systemctl", "poweroff", "--check-inhibitors=no")


def power_control_available() -> tuple[bool, str]:
    """Можно ли вообще выключать эту машину из бота."""
    if not settings.power_control_enabled:
        return False, "Управление питанием отключено в настройках (POWER_CONTROL_ENABLED)"
    if platform.system() != "Linux":
        return False, "Выключение поддерживается только на Linux"
    if shutil.which("systemctl") is None:
        return False, "systemctl не найден"
    return True, ""


def is_power_admin(user_id: int) -> bool:
    """Выключать может только администратор из ADMIN_IDS."""
    return user_id in settings.admin_ids


async def poweroff(delay: float = 5.0) -> tuple[bool, str]:
    """Планирует выключение через delay секунд.

    Задержка нужна, чтобы бот успел отправить ответ в Telegram до того,
    как система начнёт останавливать сервисы.
    """
    ok, reason = power_control_available()
    if not ok:
        return False, reason

    async def _run() -> None:
        await asyncio.sleep(delay)
        log.warning("Выключение машины по команде из Telegram")
        try:
            proc = await asyncio.create_subprocess_exec(
                *POWEROFF_CMD,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                log.error("Выключение не удалось: %s", out.decode(errors="ignore")[:300])
        except Exception:  # noqa: BLE001
            log.exception("Выключение не удалось")

    asyncio.create_task(_run())
    return True, ""
