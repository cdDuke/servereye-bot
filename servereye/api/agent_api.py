"""HTTP API для ServerEye Agent.

Агент только присылает метрики. Никаких команд в обратную сторону —
это осознанное ограничение безопасности: скомпрометированный бот не должен
превращаться в средство управления чужими серверами.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from aiohttp import web

from ..config import get_settings
from ..database import repository as repo
from ..database.database import session_scope
from ..services import pipeline
from ..services.notifier import Notifier

log = logging.getLogger(__name__)
settings = get_settings()

MAX_BODY = 64 * 1024
MIN_INTERVAL = 5.0  # защита от флуда одним токеном
_last_seen: dict[str, float] = {}

NUMERIC_FIELDS = {
    "cpu_percent": (0, 100),
    "ram_percent": (0, 100),
    "swap_percent": (0, 100),
    "disk_percent": (0, 100),
    "cpu_cores": (0, 4096),
    "load_1": (0, 10000),
    "load_5": (0, 10000),
    "load_15": (0, 10000),
    "ram_total_mb": (0, 1e9),
    "ram_used_mb": (0, 1e9),
    "ram_free_mb": (0, 1e9),
    "ram_available_mb": (0, 1e9),
    "disk_total_bytes": (0, 1e18),
    "disk_used_bytes": (0, 1e18),
    "disk_free_bytes": (0, 1e18),
    "net_down_mbps": (0, 1e7),
    "net_up_mbps": (0, 1e7),
    "net_total_recv_bytes": (0, 1e18),
    "net_total_sent_bytes": (0, 1e18),
    "uptime_seconds": (0, 1e10),
    "process_count": (0, 1e6),
    "temperature_c": (-100, 200),
    "gpu_temp_c": (-100, 200),
    "battery_percent": (0, 100),
    "battery_secs_left": (0, 1e7),
}
BOOL_FIELDS = ("power_plugged",)
STRING_FIELDS = {"os": 128, "hostname": 128, "version": 16, "gpu_name": 64}


def _extract_token(request: web.Request) -> str | None:
    token = request.headers.get("X-Agent-Token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:]
    token = (token or "").strip()
    if not token or len(token) > 64 or not token.startswith("SE-"):
        return None
    return token


def _clean_payload(raw: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, (low, high) in NUMERIC_FIELDS.items():
        value = raw.get(key)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number != number or number < low or number > high:  # NaN / вне диапазона
            continue
        payload[key] = int(number) if key in ("cpu_cores", "process_count") else number
    for key in BOOL_FIELDS:
        value = raw.get(key)
        if isinstance(value, bool):
            payload[key] = value
    for key, limit in STRING_FIELDS.items():
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            payload[key] = value.strip()[:limit]
    return payload


async def handle_metrics(request: web.Request) -> web.Response:
    token = _extract_token(request)
    if token is None:
        return web.json_response({"ok": False, "error": "invalid token"}, status=401)

    now = time.monotonic()
    previous = _last_seen.get(token)
    if previous is not None and now - previous < MIN_INTERVAL:
        return web.json_response({"ok": False, "error": "too many requests"}, status=429)

    if request.content_length and request.content_length > MAX_BODY:
        return web.json_response({"ok": False, "error": "payload too large"}, status=413)

    try:
        raw = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)
    if not isinstance(raw, dict):
        return web.json_response({"ok": False, "error": "invalid payload"}, status=400)

    payload = _clean_payload(raw)
    bot = request.app["bot"]

    async with session_scope() as session:
        server = await repo.get_server_by_agent_token(session, token)
        if server is None:
            log.warning("Отклонён heartbeat с неизвестным токеном")
            return web.json_response({"ok": False, "error": "unknown token"}, status=401)

        _last_seen[token] = now
        metric = await pipeline.store_metric(session, server, payload)
        st = await repo.get_settings_for(session, server.user_id)
        if bot is not None:
            try:
                notifier = Notifier(bot)
                await pipeline.check_resource_alerts(session, server, metric, st, notifier)
                await pipeline.check_hardware_alerts(session, server, metric, st, notifier)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка при разборе метрик сервера %s", server.id)

    return web.json_response(
        {
            "ok": True,
            "interval": settings.agent_heartbeat_seconds,
            "server": server.name,
        }
    )


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "ServerEye Agent API", "version": "1.0"})


async def handle_agent_check(request: web.Request) -> web.Response:
    """Позволяет агенту убедиться, что токен принят, ничего не записывая."""
    token = _extract_token(request)
    if token is None:
        return web.json_response({"ok": False, "error": "invalid token"}, status=401)
    async with session_scope() as session:
        server = await repo.get_server_by_agent_token(session, token)
        if server is None:
            return web.json_response({"ok": False, "error": "unknown token"}, status=401)
        return web.json_response(
            {"ok": True, "server": server.name, "interval": settings.agent_heartbeat_seconds}
        )


def build_app(bot) -> web.Application:
    app = web.Application(client_max_size=MAX_BODY)
    app["bot"] = bot
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/v1/agent/check", handle_agent_check)
    app.router.add_post("/api/v1/metrics", handle_metrics)
    return app


class AgentAPIServer:
    """Запуск/остановка HTTP-сервера вместе с ботом."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        if not settings.agent_api_enabled:
            log.info("Agent API отключён настройкой AGENT_API_ENABLED=false")
            return
        app = build_app(self.bot)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, settings.agent_api_host, settings.agent_api_port)
        try:
            await site.start()
            log.info(
                "Agent API слушает %s:%s (публичный адрес: %s)",
                settings.agent_api_host,
                settings.agent_api_port,
                settings.agent_public_url,
            )
        except OSError as exc:
            log.error("Не удалось запустить Agent API: %s", exc)
            self._runner = None

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
