"""Фоновые задачи: регулярные проверки серверов, отчёты, обслуживание."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ..bot import texts
from ..bot.keyboards import inline as kb
from ..config import get_settings
from ..database import repository as repo
from ..database.database import session_scope
from ..database.models import (
    STATUS_OFFLINE,
    STATUS_ONLINE,
    STATUS_WARNING,
    Server,
    UserSettings,
)
from ..services import access, analyzer, pipeline
from ..services.notifier import Notifier, cooldown_passed
from ..utils import esc, fmt_ago, utcnow

log = logging.getLogger(__name__)
settings = get_settings()


# ------------------------------------------------------------ проверка серверов ---
async def job_check_servers(bot: Bot) -> None:
    notifier = Notifier(bot)
    try:
        async with session_scope() as session:
            servers = await repo.all_monitored_servers(session)
            if not servers:
                return
            settings_map: dict[int, UserSettings] = {}
            for server in servers:
                if server.user_id not in settings_map:
                    settings_map[server.user_id] = await repo.get_settings_for(
                        session, server.user_id
                    )
            results = await pipeline.check_servers(session, servers, settings_map, notifier)
            log.info("Плановая проверка: %s серверов", len(results))
    except Exception:  # noqa: BLE001
        log.exception("Плановая проверка серверов упала")


# ------------------------------------------------------ сторож мониторинга ресурсов ---
async def job_agent_watchdog(bot: Bot) -> None:
    """Отдельно следит за агентом: сервер может быть жив, а агент остановлен."""
    notifier = Notifier(bot)
    threshold = timedelta(seconds=settings.agent_offline_after_seconds)
    now = utcnow()
    try:
        async with session_scope() as session:
            servers = await repo.all_monitored_servers(session)
            for server in servers:
                if not server.agent_enabled or server.agent_last_seen is None:
                    continue
                if now - server.agent_last_seen <= threshold:
                    continue
                if not cooldown_passed(server.agent_offline_notified_at, "agent_offline"):
                    continue
                if not server.notifications_enabled:
                    continue
                await repo.log_agent_event(session, server.id, "offline")
                server_state = (
                    "🟢 Сервер отвечает на проверки"
                    if server.status in (STATUS_ONLINE, STATUS_WARNING)
                    else "🔴 Сервер также недоступен"
                )
                await notifier.send_server(
                    session,
                    server,
                    "\n".join(
                        [
                            "⚠️ <b>Мониторинг ресурсов не отвечает</b>",
                            "",
                            f"<b>{esc(server.name)}</b>",
                            f"Последние данные: {fmt_ago(server.agent_last_seen)}",
                            "",
                            server_state,
                            "",
                            "Данные о CPU, RAM и диске временно недоступны.",
                        ]
                    ),
                    type_="agent_offline",
                    severity="warning",
                    reply_markup=kb.notification_actions(server.id),
                )
                server.agent_offline_notified_at = now
    except Exception:  # noqa: BLE001
        log.exception("Сторож мониторинга ресурсов упал")


# -------------------------------------------------- отложенная доставка ---
async def job_retry_notifications(bot: Bot) -> None:
    try:
        async with session_scope() as session:
            sent = await Notifier(bot).retry_undelivered(session)
        if sent:
            log.info("Доставлено отложенных уведомлений: %s", sent)
    except Exception:  # noqa: BLE001
        log.exception("Повторная отправка уведомлений упала")


# ------------------------------------------------------------------- отчёты ---
async def _user_findings(session, user_id: int, servers: list[Server]) -> list[str]:
    """Короткий список отклонений для отчёта."""
    findings: list[str] = []
    for server in servers:
        if server.status == STATUS_OFFLINE:
            findings.append(f"• {esc(server.name)}: сервер недоступен")
            continue

        insight = await analyzer.analyze_server_ping(session, server)
        if insight is not None and insight.severity == "warning":
            findings.append(
                f"• {esc(server.name)}: Ping вырос на {abs(insight.change_pct or 0):.0f}%"
            )
            continue

        stats = await repo.server_stats(session, server.id, hours=3)
        if stats.failed:
            findings.append(
                f"• {esc(server.name)}: {stats.failed} неудачных проверок из {stats.total}"
            )
        elif stats.loss_avg and stats.loss_avg >= 5:
            findings.append(f"• {esc(server.name)}: потери пакетов {stats.loss_avg:.0f}%")
    return findings[:8]


async def build_hourly_report(session, user_id: int) -> str | None:
    """Текст ежечасного отчёта. Используется и планировщиком, и командой /report."""
    servers = await repo.list_servers(session, user_id)
    if not servers:
        return None

    online = sum(1 for s in servers if s.status == STATUS_ONLINE)
    problems = sum(1 for s in servers if s.status == STATUS_WARNING)
    offline = sum(1 for s in servers if s.status == STATUS_OFFLINE)
    pings = [s.last_ping for s in servers if s.last_ping is not None]

    checks = errors = 0
    for server in servers:
        stats = await repo.server_stats(session, server.id, hours=3)
        checks += stats.total
        errors += stats.failed

    findings = await _user_findings(session, user_id, servers)
    return texts.hourly_report(
        len(servers),
        online,
        problems,
        offline,
        sum(pings) / len(pings) if pings else None,
        checks,
        errors,
        findings,
    )


async def job_hourly_report(bot: Bot) -> None:
    notifier = Notifier(bot)
    try:
        async with session_scope() as session:
            users = await repo.all_users(session)
            for user in users:
                st = await repo.get_settings_for(session, user.id)
                if not st.hourly_report:
                    continue
                text = await build_hourly_report(session, user.id)
                if text is None:
                    continue
                await notifier.send(
                    session,
                    user.id,
                    text,
                    type_="hourly_report",
                    severity="info",
                    reply_markup=kb.back_main([("🖥 Мои серверы", "menu:servers")]),
                )
    except Exception:  # noqa: BLE001
        log.exception("Ежечасный отчёт упал")


async def job_daily_report(bot: Bot) -> None:
    notifier = Notifier(bot)
    try:
        async with session_scope() as session:
            users = await repo.all_users(session)
            for user in users:
                st = await repo.get_settings_for(session, user.id)
                if not st.daily_report:
                    continue
                servers = await repo.list_servers(session, user.id)
                if not servers:
                    continue

                uptimes: list[float] = []
                pings: list[float] = []
                mins: list[float] = []
                maxs: list[float] = []
                outages = checks = errors = 0
                per_server: list[tuple[str, float]] = []

                for server in servers:
                    stats = await repo.server_stats(session, server.id, hours=24)
                    if stats.uptime is not None:
                        uptimes.append(stats.uptime)
                        per_server.append((server.name, stats.uptime))
                    if stats.ping_avg is not None:
                        pings.append(stats.ping_avg)
                    if stats.ping_min is not None:
                        mins.append(stats.ping_min)
                    if stats.ping_max is not None:
                        maxs.append(stats.ping_max)
                    outages += stats.outages
                    checks += stats.total
                    errors += stats.failed

                per_server.sort(key=lambda item: item[1], reverse=True)
                await notifier.send(
                    session,
                    user.id,
                    texts.daily_report(
                        len(servers),
                        sum(uptimes) / len(uptimes) if uptimes else None,
                        sum(pings) / len(pings) if pings else None,
                        min(mins) if mins else None,
                        max(maxs) if maxs else None,
                        outages,
                        checks,
                        errors,
                        per_server[0] if per_server else None,
                        per_server[-1] if len(per_server) > 1 else None,
                    ),
                    type_="daily_report",
                    severity="info",
                    reply_markup=kb.back_main([("📊 Статистика", "menu:stats")]),
                )
    except Exception:  # noqa: BLE001
        log.exception("Ежедневный отчёт упал")


async def job_cleanup() -> None:
    try:
        async with session_scope() as session:
            removed = await repo.cleanup_old_data(session, settings.data_retention_days)
            await access.cleanup(session, settings.data_retention_days)
        log.info("Очистка истории: %s", removed)
    except Exception:  # noqa: BLE001
        log.exception("Очистка истории упала")


# ---------------------------------------------------------------- планировщик ---
def _check_trigger():
    """Расписание проверок.

    Если интервал укладывается в час целое число раз, ставим cron —
    так проверки идут в предсказуемое время, а ежечасный отчёт в :00
    всегда опирается на свежие данные.
    """
    minutes = max(1, settings.server_check_interval_minutes)
    if minutes >= 60 and minutes % 60 == 0:
        return CronTrigger(hour=f"*/{minutes // 60}", minute=50)
    if 60 % minutes == 0:
        return CronTrigger(minute=f"*/{minutes}")
    return IntervalTrigger(minutes=minutes)


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")

    scheduler.add_job(
        job_check_servers,
        _check_trigger(),
        args=[bot],
        id="check_servers",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        job_agent_watchdog,
        IntervalTrigger(minutes=5),
        args=[bot],
        id="agent_watchdog",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        job_retry_notifications,
        IntervalTrigger(minutes=1),
        args=[bot],
        id="retry_notifications",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        job_hourly_report,
        CronTrigger(minute=settings.hourly_report_minute),
        args=[bot],
        id="hourly_report",
        max_instances=1,
        misfire_grace_time=600,
    )
    scheduler.add_job(
        job_daily_report,
        CronTrigger(
            hour=(settings.daily_report_hour - settings.default_tz_offset) % 24, minute=5
        ),
        args=[bot],
        id="daily_report",
        max_instances=1,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        job_cleanup,
        CronTrigger(hour=4, minute=30),
        id="cleanup",
        max_instances=1,
        misfire_grace_time=3600,
    )
    return scheduler


async def run_startup_jobs(bot: Bot) -> None:
    """Первый прогон сразу после старта, чтобы данные появились без ожидания."""
    await asyncio.sleep(3)
    await job_check_servers(bot)
