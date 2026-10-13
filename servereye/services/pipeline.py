"""Связующее звено: проверка → запись в БД → анализ → уведомление.

Сетевые проверки выполняются параллельно и БЕЗ обращения к БД, а запись
результатов идёт последовательно в одной сессии — так асинхронная сессия
SQLAlchemy остаётся в безопасности.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from ..bot.keyboards import inline as kb
from ..database import repository as repo
from ..database.models import (
    STATUS_OFFLINE,
    STATUS_ONLINE,
    STATUS_UNKNOWN,
    STATUS_WARNING,
    Server,
    ServerMetric,
    UserSettings,
)
from ..config import get_settings
from ..utils import esc, fmt_duration_ru, fmt_mb, fmt_ms, fmt_time, utcnow
from . import analyzer
from .notifier import Notifier, cooldown_passed
from .power import is_power_admin
from .server_monitor import CheckResult, ping_target_title, run_check, run_checks_parallel

log = logging.getLogger(__name__)
settings = get_settings()


def ping_thresholds(server: Server, st: UserSettings | None) -> tuple[float, float]:
    warning = server.ping_warning or (st.ping_warning_ms if st else 100)
    critical = server.ping_critical or (st.ping_critical_ms if st else 300)
    return float(warning), float(critical)


def ping_targets_for(server: Server) -> list[str] | None:
    """Для машины, на которой работает бот, пингуем интернет, а не себя."""
    return settings.local_ping_target_list if server.is_local_host else None


def _to_item(server: Server, st: UserSettings | None) -> dict:
    warning, critical = ping_thresholds(server, st)
    return {
        "id": server.id,
        "host": server.host,
        "check_type": server.check_type,
        "ports": server.port_list,
        "http_url": server.http_url,
        "ping_warning": warning,
        "ping_critical": critical,
        "loss_warning": float(st.packet_loss_pct) if st else 5.0,
        "ping_targets": ping_targets_for(server),
    }


async def check_servers(
    session: AsyncSession,
    servers: list[Server],
    settings_map: dict[int, UserSettings],
    notifier: Notifier | None = None,
    notify: bool = True,
    sink: dict[int, CheckResult] | None = None,
) -> dict[int, CheckResult]:
    """Проверяет список серверов параллельно и сохраняет результаты."""
    if not servers:
        return {}
    items = [_to_item(s, settings_map.get(s.user_id)) for s in servers]
    results = await run_checks_parallel(items, sink=sink)

    for server in servers:
        result = results.get(server.id)
        if result is None:
            continue
        st = settings_map.get(server.user_id)
        try:
            await apply_result(session, server, result, st, notifier if notify else None)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось обработать результат проверки сервера %s", server.id)
    await session.commit()
    return results


async def check_single(
    session: AsyncSession,
    server: Server,
    st: UserSettings | None,
    notifier: Notifier | None = None,
) -> CheckResult:
    warning, critical = ping_thresholds(server, st)
    result = await run_check(
        host=server.host,
        check_type=server.check_type,
        ports=server.port_list,
        http_url=server.http_url,
        ping_warning=warning,
        ping_critical=critical,
        loss_warning=float(st.packet_loss_pct) if st else 5.0,
        ping_targets=ping_targets_for(server),
    )
    await apply_result(session, server, result, st, notifier)
    await session.commit()
    return result


async def apply_result(
    session: AsyncSession,
    server: Server,
    result: CheckResult,
    st: UserSettings | None,
    notifier: Notifier | None,
) -> None:
    """Сохраняет проверку, двигает машину состояний и шлёт уведомления."""
    now = utcnow()
    previous_status = server.status
    previous_success = server.last_success_at
    status_since = server.status_since

    await repo.add_check(
        session,
        server.id,
        timestamp=now,
        status=result.status,
        ping=result.ping,
        ping_target=result.ping_target,
        response_time=result.response_time,
        packet_loss=result.packet_loss,
        http_status=result.http_status,
        tcp_ok=result.tcp_ok,
        tcp_detail=result.tcp_detail,
        connection_lost=result.connection_lost,
        error=result.error,
    )

    server.last_check_at = now
    server.last_ping = result.ping if result.ping is not None else server.last_ping
    if result.ping_target:
        server.last_ping_target = result.ping_target
    if result.is_up:
        server.last_success_at = now
    if result.status != previous_status:
        server.status = result.status
        server.status_since = now
    await session.flush()

    if notifier is None or st is None:
        return

    tz = st.tz_offset

    # --- сервер упал ---
    if result.status == STATUS_OFFLINE and previous_status != STATUS_OFFLINE:
        if server.notifications_enabled:
            downtime = (now - previous_success).total_seconds() if previous_success else None
            if server.is_local_host:
                targets = ", ".join(ping_target_title(t) for t in ping_targets_for(server) or [])
                lines = [
                    "🌐 <b>ПРОПАЛ ИНТЕРНЕТ</b>",
                    "",
                    f"<b>{esc(server.name)}</b> работает, но не может достучаться до {targets}.",
                ]
            else:
                lines = [
                    "🔴 <b>SERVER DOWN</b>",
                    "",
                    f"<b>{esc(server.name)}</b>",
                    "Сервер перестал отвечать.",
                    "",
                    f"Адрес: <code>{esc(server.host)}</code>",
                ]
            if server.is_local_host:
                if previous_success:
                    lines += [
                        "",
                        f"Последний удачный пинг: {fmt_time(previous_success, tz)}",
                        f"Без интернета уже: {fmt_duration_ru(downtime)}",
                    ]
                lines += ["", "Когда связь вернётся, пришлю, сколько её не было."]
            elif previous_success:
                lines += [
                    "Последний успешный ответ:",
                    fmt_time(previous_success, tz),
                    "",
                    f"Время недоступности: {fmt_duration_ru(downtime)}",
                ]
            else:
                lines.append("Успешных ответов ещё не было.")
            if result.error and not server.is_local_host:
                lines += ["", f"<i>{esc(result.error[:150])}</i>"]
            await notifier.send_server(
                session,
                server,
                "\n".join(lines),
                type_="server_down",
                severity="critical",
                reply_markup=kb.notification_actions(server.id),
            )
        server.down_notified_at = now
        await session.flush()
        return

    # --- сервер поднялся ---
    if previous_status == STATUS_OFFLINE and result.is_up:
        if server.notifications_enabled:
            downtime = (now - status_since).total_seconds() if status_since else None
            if server.is_local_host:
                lines = [
                    "🌐 <b>ИНТЕРНЕТ ВОССТАНОВЛЕН</b>",
                    "",
                    f"<b>{esc(server.name)}</b> снова в сети.",
                    "",
                    f"Без интернета: {fmt_duration_ru(downtime)}",
                    f"Ping до {ping_target_title(result.ping_target)}: {fmt_ms(result.ping)}",
                ]
            else:
                lines = [
                    "🟢 <b>SERVER ONLINE</b>",
                    "",
                    f"<b>{esc(server.name)}</b> снова доступен.",
                    "",
                    f"Недоступность: {fmt_duration_ru(downtime)}",
                    f"Ping: {fmt_ms(result.ping)}",
                ]
            await notifier.send_server(
                session,
                server,
                "\n".join(lines),
                type_="server_up",
                severity="good",
                reply_markup=kb.notification_actions(server.id),
            )
        server.down_notified_at = None
        await session.flush()

    if not result.is_up:
        return

    warning, critical = ping_thresholds(server, st)

    # --- высокий ping ---
    if (
        result.ping is not None
        and result.ping >= warning
        and server.notifications_enabled
        and cooldown_passed(server.high_ping_notified_at, "high_ping")
    ):
        stats = await repo.server_stats(session, server.id, hours=24)
        baseline = stats.ping_avg
        lines = [
            "⚠️ <b>HIGH PING</b>",
            "",
            f"<b>{esc(server.name)}</b>",
            f"Ping до {ping_target_title(result.ping_target)}: <b>{fmt_ms(result.ping)}</b>"
            if result.ping_target else f"Ping: <b>{fmt_ms(result.ping)}</b>",
        ]
        if baseline:
            growth = (result.ping - baseline) / baseline * 100
            lines += [
                "",
                "Обычный средний Ping:",
                fmt_ms(baseline),
                f"Рост: +{growth:.0f}%",
            ]
        lines += ["", f"Порог уведомления: {warning:.0f} ms"]
        await notifier.send_server(
            session,
            server,
            "\n".join(lines),
            type_="high_ping",
            severity="warning",
            reply_markup=kb.notification_actions(server.id),
        )
        server.high_ping_notified_at = now
        await session.flush()

    # --- потеря пакетов ---
    if (
        result.packet_loss is not None
        and result.packet_loss >= float(st.packet_loss_pct)
        and result.packet_loss > 0
        and server.notifications_enabled
        and cooldown_passed(server.loss_notified_at, "packet_loss")
    ):
        await notifier.send_server(
            session,
            server,
            "\n".join(
                [
                    "⚠️ <b>ПОТЕРЯ ПАКЕТОВ</b>",
                    "",
                    f"<b>{esc(server.name)}</b>",
                    f"Потеряно пакетов: <b>{result.packet_loss:.0f}%</b>",
                    "",
                    "Соединение с сервером нестабильно.",
                ]
            ),
            type_="packet_loss",
            severity="warning",
            reply_markup=kb.notification_actions(server.id),
        )
        server.loss_notified_at = now
        await session.flush()

    # --- аналитика тенденций ---
    if server.notifications_enabled and cooldown_passed(server.trend_notified_at, "trend"):
        insight = await analyzer.analyze_server_ping(session, server, result.ping)
        if insight is None:
            insight = await analyzer.analyze_instability(session, server)
        if insight is not None:
            await notifier.send_server(
                session,
                server,
                insight.render(),
                type_=insight.kind,
                severity=insight.severity,
                reply_markup=kb.notification_actions(server.id),
            )
            server.trend_notified_at = now
            await session.flush()


# --------------------------------------------------------------- метрики агента ---
async def store_metric(session: AsyncSession, server: Server, payload: dict) -> ServerMetric:
    """Сохраняет метрики от агента и обновляет heartbeat."""
    now = utcnow()
    metric = await repo.add_metric(
        session,
        server.id,
        timestamp=now,
        cpu_percent=payload.get("cpu_percent"),
        cpu_cores=payload.get("cpu_cores"),
        load_1=payload.get("load_1"),
        load_5=payload.get("load_5"),
        load_15=payload.get("load_15"),
        ram_total_mb=payload.get("ram_total_mb"),
        ram_used_mb=payload.get("ram_used_mb"),
        ram_free_mb=payload.get("ram_free_mb"),
        ram_available_mb=payload.get("ram_available_mb"),
        ram_percent=payload.get("ram_percent"),
        swap_percent=payload.get("swap_percent"),
        disk_total_bytes=payload.get("disk_total_bytes"),
        disk_used_bytes=payload.get("disk_used_bytes"),
        disk_free_bytes=payload.get("disk_free_bytes"),
        disk_percent=payload.get("disk_percent"),
        net_down_mbps=payload.get("net_down_mbps"),
        net_up_mbps=payload.get("net_up_mbps"),
        net_total_recv_bytes=payload.get("net_total_recv_bytes"),
        net_total_sent_bytes=payload.get("net_total_sent_bytes"),
        uptime_seconds=payload.get("uptime_seconds"),
        process_count=payload.get("process_count"),
        temperature_c=payload.get("temperature_c"),
        gpu_temp_c=payload.get("gpu_temp_c"),
        gpu_name=payload.get("gpu_name"),
        battery_percent=payload.get("battery_percent"),
        power_plugged=payload.get("power_plugged"),
        battery_secs_left=payload.get("battery_secs_left"),
    )
    was_offline = (
        server.agent_last_seen is not None
        and (now - server.agent_last_seen).total_seconds() > 600
    )
    server.agent_last_seen = now
    server.agent_os = payload.get("os") or server.agent_os
    server.agent_hostname = payload.get("hostname") or server.agent_hostname
    server.agent_version = payload.get("version") or server.agent_version
    if was_offline:
        await repo.log_agent_event(session, server.id, "reconnected")
        server.agent_offline_notified_at = None
    await session.flush()
    return metric


async def check_resource_alerts(
    session: AsyncSession,
    server: Server,
    metric: ServerMetric,
    st: UserSettings,
    notifier: Notifier,
) -> None:
    """Предупреждения о нагрузке. Доступность сервера тут не участвует."""
    now = utcnow()

    if (
        metric.cpu_percent is not None
        and metric.cpu_percent >= server.cpu_warning
        and server.notifications_enabled
        and cooldown_passed(server.cpu_notified_at, "cpu")
    ):
        critical = metric.cpu_percent >= server.cpu_critical
        lines = [
            "🔥 <b>Высокая загрузка CPU</b>" if critical else "⚠️ <b>Повышенная загрузка CPU</b>",
            "",
            f"<b>{esc(server.name)}</b>",
            f"CPU: <b>{metric.cpu_percent:.0f}%</b>",
        ]
        if metric.load_1 is not None:
            lines.append(f"Load Average: {metric.load_1:.2f} / {metric.load_5:.2f} / {metric.load_15:.2f}")
        lines += ["", f"Порог: {server.cpu_critical if critical else server.cpu_warning:.0f}%"]
        await notifier.send_server(
            session,
            server,
            "\n".join(lines),
            type_="cpu",
            severity="critical" if critical else "warning",
            reply_markup=kb.notification_actions(server.id),
        )
        server.cpu_notified_at = now

    if (
        metric.ram_percent is not None
        and metric.ram_percent >= server.ram_warning
        and server.notifications_enabled
        and cooldown_passed(server.ram_notified_at, "ram")
    ):
        critical = metric.ram_percent >= server.ram_critical
        free = metric.ram_available_mb if metric.ram_available_mb is not None else metric.ram_free_mb
        await notifier.send_server(
            session,
            server,
            "\n".join(
                [
                    "⚠️ <b>ВЫСОКАЯ ЗАГРУЗКА RAM</b>",
                    "",
                    f"<b>{esc(server.name)}</b>",
                    "",
                    "RAM:",
                    f"{fmt_mb(metric.ram_used_mb)} / {fmt_mb(metric.ram_total_mb)} MB",
                    f"<b>{metric.ram_percent:.2f}%</b>",
                    "",
                    f"Свободно: {fmt_mb(free)} MB",
                    "",
                    "Рекомендуется проверить процессы, потребляющие память.",
                ]
            ),
            type_="ram",
            severity="critical" if critical else "warning",
            reply_markup=kb.notification_actions(server.id),
        )
        server.ram_notified_at = now

    if (
        metric.disk_percent is not None
        and metric.disk_percent >= server.disk_warning
        and server.notifications_enabled
        and cooldown_passed(server.disk_notified_at, "disk")
    ):
        from ..utils import fmt_bytes_gb

        critical = metric.disk_percent >= server.disk_critical
        await notifier.send_server(
            session,
            server,
            "\n".join(
                [
                    "💾 <b>Заканчивается место</b>",
                    "",
                    f"<b>{esc(server.name)}</b>",
                    f"Использовано: <b>{metric.disk_percent:.0f}%</b>",
                    f"Свободно: {fmt_bytes_gb(metric.disk_free_bytes)}",
                    f"Всего: {fmt_bytes_gb(metric.disk_total_bytes)}",
                ]
            ),
            type_="disk",
            severity="critical" if critical else "warning",
            reply_markup=kb.notification_actions(server.id),
        )
        server.disk_notified_at = now

    await session.flush()


# ------------------------------------------------------- температура и питание ---
def _hardware_markup(server: Server):
    """Кнопки под тревогой: «Выключить» видит только владелец."""
    return lambda user_id: kb.hardware_alert_actions(server, is_power_admin(user_id))


async def check_hardware_alerts(
    session: AsyncSession,
    server: Server,
    metric: ServerMetric,
    st: UserSettings,
    notifier: Notifier,
) -> None:
    """Перегрев CPU (повторяющаяся тревога) и отключение электричества."""
    await _check_temperature(session, server, metric, st, notifier)
    await _check_power(session, server, metric, st, notifier)
    await session.flush()


async def _check_temperature(session, server, metric, st, notifier) -> None:
    temp = metric.temperature_c
    if temp is None:
        return
    now = utcnow()
    alarm_at = float(server.cpu_temp_alarm or 90.0)
    clear_at = alarm_at - settings.temp_alarm_clear_delta
    repeat = timedelta(minutes=settings.temp_alarm_repeat_minutes)
    allowed = server.notifications_enabled
    markup = _hardware_markup(server)

    if temp >= alarm_at:
        if not server.temp_alarm_active:
            server.temp_alarm_active = True
            server.temp_alarm_since = now
            server.temp_alarm_last_at = None
            server.temp_alarm_peak = temp
        server.temp_alarm_peak = max(server.temp_alarm_peak or temp, temp)

        due = server.temp_alarm_last_at is None or now - server.temp_alarm_last_at >= repeat
        if not (due and allowed):
            return
        elapsed = (now - server.temp_alarm_since).total_seconds()
        number = int(elapsed // repeat.total_seconds()) + 1
        lines = [
            "🚨🔥 <b>ПЕРЕГРЕВ ПРОЦЕССОРА</b>",
            "",
            f"<b>{esc(server.name)}</b>",
            f"CPU: <b>{temp:.0f}°C</b> (порог {alarm_at:.0f}°C)",
            f"Пик: {server.temp_alarm_peak:.0f}°C",
            "Перегрев только начался" if elapsed < 60 else f"Перегрев длится: {fmt_duration_ru(elapsed)}",
        ]
        if metric.cpu_percent is not None:
            lines.append(f"Нагрузка CPU: {metric.cpu_percent:.0f}%")
        if metric.gpu_temp_c is not None:
            lines.append(f"GPU: {metric.gpu_temp_c:.0f}°C")
        lines += [
            "",
            "Проверьте вентиляцию: ноутбук не должен стоять на мягкой поверхности, "
            "решётки должны быть открыты.",
            "",
            f"<i>Напоминание №{number} · повтор каждые "
            f"{settings.temp_alarm_repeat_minutes:g} мин, пока выше {alarm_at:.0f}°C</i>",
        ]
        await notifier.send_server(
            session, server, "\n".join(lines),
            type_="temp_alarm", severity="critical",
            reply_markup=markup,
        )
        server.temp_alarm_last_at = now
        return

    if server.temp_alarm_active and temp < clear_at:
        duration = (now - server.temp_alarm_since).total_seconds() if server.temp_alarm_since else None
        peak = server.temp_alarm_peak
        server.temp_alarm_active = False
        server.temp_alarm_since = None
        server.temp_alarm_last_at = None
        server.temp_alarm_peak = None
        if allowed:
            await notifier.send_server(
                session, server,
                "\n".join([
                    "✅ <b>Температура в норме</b>",
                    "",
                    f"<b>{esc(server.name)}</b>",
                    f"CPU: {temp:.0f}°C",
                    f"Перегрев длился: {fmt_duration_ru(duration)}",
                    f"Пик: {peak:.0f}°C" if peak is not None else "",
                ]).rstrip(),
                type_="temp_ok", severity="good",
                reply_markup=kb.notification_actions(server.id),
            )


def _battery_line(metric: ServerMetric) -> str:
    if metric.battery_percent is None:
        return "Заряд: нет данных"
    line = f"Заряд: <b>{metric.battery_percent:.0f}%</b>"
    if metric.battery_secs_left:
        line += f" · осталось ~{fmt_duration_ru(metric.battery_secs_left)}"
    return line


async def _check_power(session, server, metric, st, notifier) -> None:
    plugged = metric.power_plugged
    if plugged is None:
        return  # у машины нет батареи — детектор неприменим
    now = utcnow()
    allowed = server.notifications_enabled
    markup = _hardware_markup(server)

    if plugged is False and not server.on_battery:
        server.on_battery = True
        server.power_lost_at = now
        server.battery_alert_level = None
        if allowed:
            await notifier.send_server(
                session, server,
                "\n".join([
                    "⚡🔋 <b>ПРОПАЛО ПИТАНИЕ</b>",
                    "",
                    f"<b>{esc(server.name)}</b> работает от батареи.",
                    _battery_line(metric),
                    "",
                    "Возможно, отключили свет или выдернули зарядку.",
                ]),
                type_="power_lost", severity="critical",
                reply_markup=markup,
            )
    elif plugged is True and server.on_battery:
        duration = (now - server.power_lost_at).total_seconds() if server.power_lost_at else None
        server.on_battery = False
        server.power_lost_at = None
        server.battery_alert_level = None
        if allowed:
            await notifier.send_server(
                session, server,
                "\n".join([
                    "🔌 <b>ПИТАНИЕ ВОССТАНОВЛЕНО</b>",
                    "",
                    f"<b>{esc(server.name)}</b> снова работает от сети.",
                    f"Без питания: {fmt_duration_ru(duration)}",
                    _battery_line(metric),
                ]),
                type_="power_restored", severity="good",
                reply_markup=kb.notification_actions(server.id),
            )

    if server.on_battery and metric.battery_percent is not None:
        crossed = [lvl for lvl in settings.battery_levels if metric.battery_percent <= lvl]
        if not crossed:
            return
        level = min(crossed)
        if server.battery_alert_level is not None and level >= server.battery_alert_level:
            return
        server.battery_alert_level = level
        if allowed:
            await notifier.send_server(
                session, server,
                "\n".join([
                    f"🪫 <b>БАТАРЕЯ НИЖЕ {level}%</b>",
                    "",
                    f"<b>{esc(server.name)}</b>",
                    _battery_line(metric),
                    "",
                    "Питание так и не вернулось. Когда батарея сядет, "
                    "ноутбук выключится сам.",
                ]),
                type_="battery_low", severity="critical",
                reply_markup=markup,
            )
