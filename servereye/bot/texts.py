"""Все тексты интерфейса ServerEye собраны в одном месте."""
from __future__ import annotations


from ..config import get_settings
from ..database.models import (
    CHECK_TYPE_TITLES,
    STATUS_OFFLINE,
    STATUS_ONLINE,
    STATUS_UNKNOWN,
    STATUS_WARNING,
    Server,
    ServerMetric,
)
from ..database.repository import MAX_SERVERS_PER_USER as MAX_SERVERS
from ..database.repository import ServerStats
from ..services.server_monitor import PING_TARGET_NAMES, ping_target_title
from ..utils import (
    esc,
    fmt_ago,
    fmt_bytes_gb,
    fmt_dt,
    fmt_duration_ru,
    fmt_mb,
    fmt_mbps,
    fmt_ms,
    fmt_time,
    sparkline,
    utcnow,
)

settings = get_settings()

BRAND = "🛡 <b>ServerEye</b>"
SUBTITLE = "<i>Мониторинг доступности серверов</i>"

STATUS_EMOJI = {
    STATUS_ONLINE: "🟢",
    STATUS_WARNING: "🟡",
    STATUS_OFFLINE: "🔴",
    STATUS_UNKNOWN: "⚪",
}
STATUS_WORD = {
    STATUS_ONLINE: "ONLINE",
    STATUS_WARNING: "ВНИМАНИЕ",
    STATUS_OFFLINE: "OFFLINE",
    STATUS_UNKNOWN: "НЕТ ДАННЫХ",
}
STATUS_HUMAN = {
    STATUS_ONLINE: "Сервер работает нормально",
    STATUS_WARNING: "Сервер отвечает, но есть замечания",
    STATUS_OFFLINE: "Сервер недоступен",
    STATUS_UNKNOWN: "Ещё не проверялся",
}


def emoji(status: str) -> str:
    return STATUS_EMOJI.get(status, "⚪")


def agent_state(server: Server) -> tuple[bool, str]:
    """(подключён?, человекочитаемый статус агента)."""
    if not server.agent_enabled:
        return False, "не подключён"
    if server.agent_last_seen is None:
        return False, "ожидает первого подключения"
    delta = (utcnow() - server.agent_last_seen).total_seconds()
    if delta <= settings.agent_offline_after_seconds:
        return True, f"🟢 Connected · {fmt_ago(server.agent_last_seen)}"
    return False, f"🔴 Agent offline · последний heartbeat {fmt_ago(server.agent_last_seen)}"


def ram_line(metric: ServerMetric | None) -> str | None:
    if not metric or metric.ram_total_mb is None:
        return None
    used = fmt_mb(metric.ram_used_mb)
    total = fmt_mb(metric.ram_total_mb)
    percent = f"{metric.ram_percent:.2f}%" if metric.ram_percent is not None else "—"
    return f"{used} / {total} MB ({percent})"


def disk_line(metric: ServerMetric | None) -> str | None:
    if not metric or metric.disk_total_bytes is None:
        return None
    used = fmt_bytes_gb(metric.disk_used_bytes)
    total = fmt_bytes_gb(metric.disk_total_bytes)
    percent = f"{metric.disk_percent:.1f}%" if metric.disk_percent is not None else "—"
    return f"{used} / {total} ({percent})"


def load_level(value: float | None, warning: float, critical: float) -> str:
    if value is None:
        return "⚪"
    if value >= critical:
        return "🔴"
    if value >= warning:
        return "🟡"
    return "🟢"


def temp_mark(value: float | None, alarm: float) -> str:
    if value is None:
        return "⚪"
    if value >= alarm:
        return "🔴"
    if value >= alarm - 15:
        return "🟡"
    return "🟢"


def power_line(server: Server, metric: ServerMetric | None) -> str | None:
    if metric is None or metric.power_plugged is None:
        return None
    charge = f"{metric.battery_percent:.0f}%" if metric.battery_percent is not None else "—"
    if metric.power_plugged:
        return f"🔌 От сети · заряд {charge}"
    line = f"⚡ <b>ОТ БАТАРЕИ</b> · заряд {charge}"
    if metric.battery_secs_left:
        line += f" · ~{fmt_duration_ru(metric.battery_secs_left)}"
    return line


def hardware_lines(server: Server, metric: ServerMetric, tz: int) -> list[str]:
    """Блоки «Температура» и «Питание» для карточки сервера."""
    out: list[str] = []
    alarm = float(server.cpu_temp_alarm or 90)
    gpu_sleeping = metric.gpu_temp_c is None and bool(metric.gpu_name) and "сон" in metric.gpu_name
    if metric.temperature_c is not None or metric.gpu_temp_c is not None or gpu_sleeping:
        out += ["", "🌡 <b>Температура</b>"]
        if metric.temperature_c is not None:
            out.append(
                f"CPU: {temp_mark(metric.temperature_c, alarm)} {metric.temperature_c:.0f}°C"
                f" <i>(тревога от {alarm:.0f}°C)</i>"
            )
        if metric.gpu_temp_c is not None:
            name = f" · {esc(metric.gpu_name)}" if metric.gpu_name else ""
            out.append(f"GPU: {temp_mark(metric.gpu_temp_c, alarm)} {metric.gpu_temp_c:.0f}°C{name}")
        elif gpu_sleeping:
            out.append("GPU: 💤 спит — не нагружена и не греется")
        if server.temp_alarm_active and server.temp_alarm_since:
            out.append(
                f"🚨 Перегрев с {fmt_time(server.temp_alarm_since, tz)}, "
                f"пик {server.temp_alarm_peak or metric.temperature_c:.0f}°C"
            )
    line = power_line(server, metric)
    if line:
        out += ["", "🔋 <b>Питание</b>", line]
        if server.on_battery and server.power_lost_at:
            out.append(f"Без сети с {fmt_time(server.power_lost_at, tz)}")
    return out


# ------------------------------------------------------------------ главное ---
def main_menu(user_name: str | None, total: int, online: int, problems: int, offline: int) -> str:
    hello = f"Привет, {esc(user_name)}!" if user_name else "Привет!"
    if total == 0:
        state = (
            "У вас пока нет подключённых серверов.\n"
            "Нажмите «➕ Подключить сервер», чтобы начать мониторинг."
        )
    else:
        state = (
            f"🖥 Серверы: <b>{total}</b>\n"
            f"🟢 Online: {online}   🟡 Проблемы: {problems}   🔴 Offline: {offline}"
        )
    return f"{BRAND}\n{SUBTITLE}\n\n{hello}\n\n{state}\n\nВыберите раздел:"


async def main_menu_for(session, user_id: int, first_name: str | None) -> str:
    """Главное меню с актуальной сводкой по общему списку серверов."""
    from ..database import repository as repo

    servers = await repo.list_servers(session, user_id)
    return main_menu(
        first_name,
        len(servers),
        sum(1 for s in servers if s.status == STATUS_ONLINE),
        sum(1 for s in servers if s.status == STATUS_WARNING),
        sum(1 for s in servers if s.status == STATUS_OFFLINE),
    )


# ------------------------------------------------------------------ серверы ---
def servers_list(
    rows: list[tuple[Server, ServerStats | None]],
    creators: dict[int, str] | None = None,
    viewer_id: int | None = None,
) -> str:
    if not rows:
        return (
            f"{BRAND}\n\n🖥 <b>Мои серверы</b>\n\n"
            "Список пуст. Подключите первый сервер — это займёт полминуты."
        )
    out = [f"🖥 <b>МОИ СЕРВЕРЫ</b>", ""]
    for server, stats in rows:
        suffix = ""
        if creators and server.user_id != viewer_id and server.user_id in creators:
            suffix = f" <i>· {esc(creators[server.user_id])}</i>"
        out.append(f"{emoji(server.status)} <b>{esc(server.name)}</b>{suffix}")
        if server.status == STATUS_OFFLINE:
            out.append("Недоступен")
        elif server.status == STATUS_UNKNOWN:
            out.append("Ожидает первой проверки")
        else:
            ping = fmt_ms(server.last_ping) if server.last_ping is not None else "—"
            uptime = f"{stats.uptime:.2f}%" if stats and stats.uptime is not None else "—"
            out.append(f"Ping: {ping}")
            out.append(f"Uptime: {uptime}")
        out.append("")
    out.append("Нажмите на сервер, чтобы открыть подробности.")
    if creators and len(creators) > 1:
        out.append("<i>Серверы общие для всех участников бота. Курсивом — кто добавил.</i>")
    return "\n".join(out)


def server_card(
    server: Server,
    stats24: ServerStats,
    stats7: ServerStats,
    stats30: ServerStats,
    metric: ServerMetric | None,
    tz: int,
    creator=None,
    viewer_id: int | None = None,
) -> str:
    connected, agent_text = agent_state(server)
    out = [
        f"🖥 <b>{esc(server.name)}</b>",
        f"{emoji(server.status)} <b>{STATUS_WORD.get(server.status, '—')}</b> — "
        f"{STATUS_HUMAN.get(server.status, '')}",
        "",
        "🌐 <b>Сеть</b>",
    ]
    if server.is_local_host:
        host = f" · <code>{esc(server.agent_hostname)}</code>" if server.agent_hostname else ""
        targets = settings.local_ping_target_list
        backup = f", запасная — {ping_target_title(targets[1])}" if len(targets) > 1 else ""
        out += [
            f"Адрес: эта машина{host}",
            f"Проверка: интернет — пинг до {ping_target_title(targets[0] if targets else None)}{backup}",
        ]
    else:
        out += [
            f"Адрес: <code>{esc(server.host)}</code>",
            f"Тип проверки: {CHECK_TYPE_TITLES.get(server.check_type, server.check_type)}",
        ]
    if creator is not None:
        who = "вы" if creator.id == viewer_id else esc(creator.display_name)
        out.append(f"Добавил(-а): {who}")
    if server.port_list:
        out.append(f"Порты: {', '.join(str(p) for p in server.port_list)}")
    ping_label = (
        f"Ping до {ping_target_title(server.last_ping_target)} сейчас"
        if server.is_local_host and server.last_ping_target
        else "Ping сейчас"
    )
    out += [
        f"{ping_label}: {fmt_ms(server.last_ping)}",
        f"Средний Ping: {fmt_ms(stats24.ping_avg)}",
        f"Мин / Макс Ping: {fmt_ms(stats24.ping_min)} / {fmt_ms(stats24.ping_max)}",
        f"Потеря пакетов: {stats24.loss_avg:.1f}%" if stats24.loss_avg is not None else "Потеря пакетов: —",
        "",
        "⏱ <b>Uptime</b>",
        f"24 часа — {stats24.uptime:.2f}%" if stats24.uptime is not None else "24 часа — нет данных",
        f"7 дней — {stats7.uptime:.2f}%" if stats7.uptime is not None else "7 дней — нет данных",
        f"30 дней — {stats30.uptime:.2f}%" if stats30.uptime is not None else "30 дней — нет данных",
    ]

    if metric is not None:
        out += ["", "⚙️ <b>CPU</b>"]
        cpu_mark = load_level(metric.cpu_percent, server.cpu_warning, server.cpu_critical)
        out.append(f"{cpu_mark} {metric.cpu_percent:.0f}%" if metric.cpu_percent is not None else "—")
        if metric.cpu_cores:
            out.append(f"{metric.cpu_cores} ядер")
        if metric.load_1 is not None:
            out.append(
                f"Load Average: {metric.load_1:.2f} / {metric.load_5:.2f} / {metric.load_15:.2f}"
            )
        ram = ram_line(metric)
        if ram:
            ram_mark = load_level(metric.ram_percent, server.ram_warning, server.ram_critical)
            free = fmt_mb(metric.ram_available_mb if metric.ram_available_mb is not None else metric.ram_free_mb)
            out += ["", "🧠 <b>RAM</b>", f"{ram_mark} {ram}", f"Свободно: {free} MB"]
        disk = disk_line(metric)
        if disk:
            disk_mark = load_level(metric.disk_percent, server.disk_warning, server.disk_critical)
            out += ["", "💾 <b>Disk</b>", f"{disk_mark} {disk}", f"Свободно: {fmt_bytes_gb(metric.disk_free_bytes)}"]
        if metric.net_down_mbps is not None or metric.net_up_mbps is not None:
            out += [
                "",
                "📡 <b>Сеть сервера</b>",
                f"↓ {fmt_mbps(metric.net_down_mbps)}",
                f"↑ {fmt_mbps(metric.net_up_mbps)}",
            ]
        if metric.uptime_seconds:
            out += ["", "⏱ <b>Uptime системы</b>", fmt_duration_ru(metric.uptime_seconds)]
        out += hardware_lines(server, metric, tz)

    # Блок мониторинга ресурсов показываем только там, где он включён
    if server.agent_enabled:
        out += ["", "🔐 <b>Мониторинг ресурсов</b>", agent_text]
        if server.agent_os:
            out.append(f"OS: {esc(server.agent_os)}")
        if server.agent_hostname:
            out.append(f"Hostname: <code>{esc(server.agent_hostname)}</code>")
        if not connected:
            out.append(
                "⚠️ Доступность сервера и состояние агента — разные вещи: "
                "сервер может отвечать, а агент быть остановлен."
            )

    out += [
        "",
        f"Последняя проверка: {fmt_time(server.last_check_at, tz)}",
        f"Проверок за 24 ч: {stats24.total} · ошибок: {stats24.errors}",
    ]
    return "\n".join(out)


def server_stats_text(
    server: Server, stats24: ServerStats, stats7: ServerStats, stats30: ServerStats
) -> str:
    def block(title: str, s: ServerStats) -> list[str]:
        if not s.total:
            return [f"<b>{title}</b>", "нет данных", ""]
        return [
            f"<b>{title}</b>",
            f"Проверок: {s.total}",
            f"Успешных: {s.ok} ({s.success_rate:.1f}%)" if s.success_rate is not None else f"Успешных: {s.ok}",
            f"С замечаниями: {s.warning}",
            f"Ошибок: {s.failed}",
            f"Эпизодов недоступности: {s.outages}",
            f"Uptime: {s.uptime:.2f}%" if s.uptime is not None else "Uptime: —",
            f"Ping avg/min/max: {fmt_ms(s.ping_avg)} / {fmt_ms(s.ping_min)} / {fmt_ms(s.ping_max)}",
            f"Потеря пакетов: {s.loss_avg:.1f}%" if s.loss_avg is not None else "Потеря пакетов: —",
            "",
        ]

    lines = [f"📊 <b>Статистика — {esc(server.name)}</b>", ""]
    lines += block("За 24 часа", stats24)
    lines += block("За 7 дней", stats7)
    lines += block("За 30 дней", stats30)
    return "\n".join(lines)[:4000]


def server_history(server: Server, checks: list, tz: int) -> str:
    if not checks:
        return f"📊 <b>История — {esc(server.name)}</b>\n\nПока нет проверок."
    lines = [f"📊 <b>История — {esc(server.name)}</b>", "", "Последние проверки:", ""]
    for check in checks:
        parts = [f"{fmt_time(check.timestamp, tz)} {emoji(check.status)}"]
        if check.ping is not None:
            name = PING_TARGET_NAMES.get(check.ping_target or "", "")
            parts.append(f"ping {fmt_ms(check.ping)}" + (f" ({name})" if name else ""))
        if check.http_status:
            parts.append(f"HTTP {check.http_status}")
        if check.response_time is not None:
            parts.append(f"ответ {check.response_time:.0f} ms")
        if check.packet_loss:
            parts.append(f"потери {check.packet_loss:.0f}%")
        lines.append(" · ".join(parts))
        if check.error and check.status == STATUS_OFFLINE:
            lines.append(f"   ↳ <i>{esc(check.error[:90])}</i>")
    pings = [c.ping for c in checks][::-1]
    lines += ["", f"Ping: {sparkline(pings)}"]
    return "\n".join(lines)


# ---------------------------------------------------------- срочная проверка ---
def urgent_progress(names: list[str], done: dict[str, str] | None = None) -> str:
    done = done or {}
    lines = ["⏳ <b>Проверяю серверы...</b>", ""]
    for name in names:
        lines.append(f"{esc(name)} {done.get(name, '⏳')}")
    return "\n".join(lines)


def urgent_result(blocks: list[dict], online: int, problems: int, offline: int) -> str:
    lines = ["⚡ <b>Результат срочной проверки</b>", ""]
    for block in blocks:
        lines.append(f"{block['emoji']} <b>{esc(block['name'])}</b>")
        lines.extend(block["lines"])
        lines.append("")
    lines += [
        "———",
        f"✅ Доступны: {online}",
        f"⚠️ Проблемы: {problems}",
        f"🔴 Недоступны: {offline}",
    ]
    return "\n".join(lines)


def server_check_block(server: Server, result, metric: ServerMetric | None) -> dict:
    lines: list[str] = []
    if result.status == STATUS_OFFLINE:
        lines.append("Нет интернета" if server.is_local_host else "Сервер недоступен")
        if result.error:
            lines.append(f"<i>{esc(result.error[:120])}</i>")
    else:
        if result.ping is not None:
            if result.ping_target:
                name = PING_TARGET_NAMES.get(result.ping_target, result.ping_target)
                lines.append(f"Интернет: {fmt_ms(result.ping)} до {name}")
            else:
                lines.append(f"Ping: {fmt_ms(result.ping)}")
        if result.http_status:
            lines.append(f"HTTP: {result.http_status} ({result.response_time:.0f} ms)"
                         if result.response_time else f"HTTP: {result.http_status}")
        if result.tcp_detail:
            lines.append(f"Порты: {result.tcp_detail}")
        if result.packet_loss:
            lines.append(f"Потеря пакетов: {result.packet_loss:.0f}%")
        connected, _ = agent_state(server)
        if metric is not None and connected:
            if metric.cpu_percent is not None:
                lines.append(f"CPU: {metric.cpu_percent:.0f}%")
            ram = ram_line(metric)
            if ram:
                lines.append(f"RAM: {ram}")
            disk = disk_line(metric)
            if disk:
                lines.append(f"Disk: {disk}")
            alarm = float(server.cpu_temp_alarm or 90)
            if metric.temperature_c is not None:
                lines.append(f"CPU temp: {temp_mark(metric.temperature_c, alarm)} {metric.temperature_c:.0f}°C")
            if metric.gpu_temp_c is not None:
                lines.append(f"GPU temp: {temp_mark(metric.gpu_temp_c, alarm)} {metric.gpu_temp_c:.0f}°C")
            elif metric.gpu_name and "сон" in metric.gpu_name:
                lines.append("GPU: 💤 спит")
            power = power_line(server, metric)
            if power:
                lines.append(power)
        elif server.agent_enabled:
            lines.append("Agent: 🔴 не отвечает (нагрузка недоступна)")
        lines.append(f"Status: {STATUS_WORD.get(result.status, '—')}")
    return {"emoji": emoji(result.status), "name": server.name, "lines": lines}


# ------------------------------------------------------------------ отчёты ---
def hourly_report(
    total: int,
    online: int,
    problems: int,
    offline: int,
    avg_ping: float | None,
    checks: int,
    errors: int,
    findings: list[str],
) -> str:
    lines = ["📊 <b>ServerEye — отчёт за час</b>", ""]
    if total:
        lines += [
            "🖥 <b>Серверы</b>",
            f"Всего: {total}",
            f"🟢 Online: {online}",
            f"🟡 Проблемы: {problems}",
            f"🔴 Offline: {offline}",
            "",
            f"Средний Ping: {fmt_ms(avg_ping)}",
            f"Проверок за час: {checks}",
            f"Ошибок: {errors}",
        ]
    else:
        lines += [
            "🖥 Серверы не подключены",
            "",
            "Нажмите «➕ Подключить сервер», чтобы начать мониторинг.",
        ]
    if findings:
        lines += ["", "⚠️ <b>Обнаружено:</b>", *findings]
    elif total:
        lines += ["", "✅ Отклонений не обнаружено"]
    return "\n".join(lines)


def daily_report(
    total_servers: int,
    uptime: float | None,
    avg_ping: float | None,
    min_ping: float | None,
    max_ping: float | None,
    outages: int,
    checks: int,
    errors: int,
    best: tuple[str, float] | None,
    worst: tuple[str, float] | None,
) -> str:
    lines = ["📊 <b>ServerEye</b>", "<b>Ежедневный отчёт</b>", ""]
    if not total_servers:
        lines.append("🖥 Серверы не подключены")
        return "\n".join(lines)

    lines += [
        "🖥 <b>Серверы</b>",
        f"Подключено: {total_servers}",
        f"Uptime: {uptime:.2f}%" if uptime is not None else "Uptime: нет данных",
        "",
        f"Средний Ping: {fmt_ms(avg_ping)}",
        f"Лучший Ping: {fmt_ms(min_ping)}",
        f"Худший Ping: {fmt_ms(max_ping)}",
        "",
        f"Проверок за сутки: {checks}",
        f"Ошибок: {errors}",
        f"Эпизодов недоступности: {outages}",
    ]
    if best:
        lines += ["", f"🥇 Стабильнее всех: {esc(best[0])} — {best[1]:.2f}%"]
    if worst and (not best or worst[0] != best[0]):
        lines.append(f"⚠️ Хуже всех: {esc(worst[0])} — {worst[1]:.2f}%")
    if outages == 0:
        lines += ["", "✅ За сутки сбоев не зафиксировано"]
    return "\n".join(lines)


def system_state(
    total: int,
    online: int,
    problems: int,
    offline: int,
    uptime24: float | None,
    incidents: int,
    avg_ping: float | None,
    agents_ok: int,
    agents_total: int,
) -> str:
    if not total:
        return (
            "🧠 <b>СОСТОЯНИЕ СИСТЕМЫ</b>\n\n"
            "⚪ Серверы не подключены\n\n"
            "Нажмите «➕ Подключить сервер», чтобы ServerEye начал следить "
            "за доступностью."
        )
    if offline:
        head = "🔴 <b>Есть недоступные серверы</b>"
    elif problems:
        head = "🟡 <b>Есть замечания</b>"
    else:
        head = "🟢 <b>Все системы работают</b>"

    lines = [
        "🧠 <b>СОСТОЯНИЕ СИСТЕМЫ</b>",
        "",
        head,
        "",
        f"Серверов: {total}",
        f"🟢 Online: {online}",
        f"🟡 Problems: {problems}",
        f"🔴 Offline: {offline}",
        "",
        f"Средний Ping: {fmt_ms(avg_ping)}",
    ]
    if agents_total:
        lines.append(f"🔐 Мониторинг ресурсов: {agents_ok} / {agents_total} активны")
    lines += [
        "",
        "<b>За последние 24 часа</b>",
        f"Uptime: {uptime24:.2f}%" if uptime24 is not None else "Uptime: нет данных",
        f"Проблем: {incidents}",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------- уведомления ---
def notifications_settings(st) -> str:
    def mark(value: bool) -> str:
        return "☑" if value else "☐"

    return "\n".join(
        [
            "🔔 <b>Настройки уведомлений</b>",
            "",
            "Нажмите на пункт, чтобы включить или выключить:",
            "",
            f"{mark(st.notify_server_down)} Сервер отключился",
            f"{mark(st.notify_server_up)} Сервер восстановился",
            f"{mark(st.notify_high_ping)} Высокий Ping",
            f"{mark(st.notify_packet_loss)} Потеря пакетов",
            f"{mark(st.notify_cpu)} Высокая загрузка CPU",
            f"{mark(st.notify_ram)} Высокая загрузка RAM",
            f"{mark(st.notify_disk)} Заканчивается место на диске",
            f"{mark(st.notify_temp)} Перегрев процессора (повтор каждые "
            f"{settings.temp_alarm_repeat_minutes:g} мин)",
            f"{mark(st.notify_power)} Пропало питание / батарея садится",
            f"{mark(st.notify_agent_offline)} Мониторинг ресурсов не отвечает",
            f"{mark(st.notify_trend)} Аналитика и тенденции",
            f"{mark(st.hourly_report)} Ежечасный отчёт",
            f"{mark(st.daily_report)} Ежедневный отчёт",
            "",
            "<b>Пороги</b>",
            f"Ping warning: {st.ping_warning_ms} ms",
            f"Ping critical: {st.ping_critical_ms} ms",
            f"Потеря пакетов выше: {st.packet_loss_pct:.0f}%",
            "",
            "<i>Пороги можно задать отдельно для каждого сервера — "
            "в его карточке ⚙️ Настройки.</i>",
        ]
    )


def settings_page(st, servers_count: int) -> str:
    return "\n".join(
        [
            "⚙️ <b>Настройки</b>",
            "",
            f"Часовой пояс: UTC{st.tz_offset:+d}",
            f"Ежечасный отчёт: {'включён' if st.hourly_report else 'выключен'}",
            f"Ежедневный отчёт: {'включён' if st.daily_report else 'выключен'}",
            "",
            "<b>Мониторинг</b>",
            f"Добавлено вами серверов: {servers_count} из {MAX_SERVERS}",
            f"Интервал проверки: раз в {settings.server_check_interval_minutes} мин",
            f"Хранение истории: {settings.data_retention_days} дней",
            "",
            "<i>Все данные привязаны к вашему аккаунту Telegram "
            "и не видны другим пользователям.</i>",
        ]
    )


def agent_instructions(server: Server, public_url: str) -> str:
    token = server.agent_token or "—"
    return "\n".join(
        [
            f"🔐 <b>ServerEye Agent — {esc(server.name)}</b>",
            "",
            "🔑 Token:",
            f"<code>{esc(token)}</code>",
            "",
            "Установите агент на сам сервер (Linux или Windows):",
            "",
            "<b>1.</b> Скопируйте на сервер файл <code>agent/servereye_agent.py</code>",
            "<b>2.</b> Установите зависимости:",
            "<code>pip install psutil httpx</code>",
            "<b>3.</b> Запустите:",
            f"<code>python servereye_agent.py --server {esc(public_url)} --token {esc(token)}</code>",
            "",
            "Агент отправляет только метрики нагрузки (CPU, RAM, Disk, сеть, uptime).",
            "Он не выполняет команды и не требует root-прав.",
            "",
            "⚠️ Токен — это доступ к записи метрик этого сервера. "
            "Не публикуйте его; при утечке нажмите «🔑 Перевыпустить токен».",
        ]
    )


# ------------------------------------------------------------------- доступ ---
_TZ = settings.default_tz_offset
ONLINE_WINDOW = 300  # секунд — «сейчас в боте»

ACCESS_STATUS_TITLES = {
    "active": "🟢 доступ есть",
    "pending": "⏳ ждёт подтверждения",
    "denied": "❌ отказано",
    "blocked": "🚫 заблокирован",
}
ACCESS_STATUS_ICON = {"active": "🟢", "pending": "⏳", "denied": "❌", "blocked": "🚫"}


def person_lines(person) -> list[str]:
    """Кто это: имя-ссылка, username, ID. person — aiogram User или модель User."""
    name = " ".join(x for x in (person.first_name, getattr(person, "last_name", None)) if x) or "без имени"
    username = f"@{esc(person.username)}" if person.username else "нет"
    lines = [
        f'👤 <a href="tg://user?id={person.id}">{esc(name)}</a>',
        f"Username: {username}",
        f"Telegram ID: <code>{person.id}</code>",
    ]
    if getattr(person, "language_code", None):
        lines.append(f"Язык: {esc(person.language_code)}")
    return lines


def invite_line(invite) -> str:
    if invite is None:
        return "Приглашение: —"
    note = f" «{esc(invite.note)}»" if invite.note else ""
    mode = "с подтверждением" if invite.mode == "approve" else "вход сразу"
    return f"🔗 Приглашение #{invite.id}{note} · {mode}"


def is_online(user) -> bool:
    return user.last_seen_at is not None and (utcnow() - user.last_seen_at).total_seconds() <= ONLINE_WINDOW


def access_joined(user, invite) -> str:
    lines = ["🔔 <b>Новый пользователь в боте</b>", "", *person_lines(user), "", invite_line(invite)]
    if invite is not None and invite.created_at:
        lines.append(
            f"Создано {fmt_dt(invite.created_at, _TZ)} · использовано {fmt_time(invite.used_at, _TZ)}"
        )
    lines += ["", "Если это не тот человек — заберите доступ кнопкой ниже."]
    return "\n".join(lines)


def access_request(user, invite) -> str:
    return "\n".join(
        [
            "🔔 <b>Запрос доступа</b>",
            "",
            *person_lines(user),
            "",
            invite_line(invite),
            "",
            "Приглашение уже погашено — второй раз по нему не войти.",
            "Пустить этого человека в бот?",
        ]
    )


def access_reused(tg, invite, previous) -> str:
    lines = [
        "⚠️ <b>Попытка войти по чужой ссылке</b>",
        "",
        f"{invite_line(invite)} уже использовано.",
    ]
    if previous is not None:
        lines.append(f"Им вошёл(-ла): {esc(previous.display_name)} (<code>{previous.id}</code>)")
    lines += [
        "",
        "Сейчас по этой же ссылке пытается войти:",
        *person_lines(tg),
        "",
        "Похоже, ссылку переслали. Доступ <b>не выдан</b>.",
    ]
    return "\n".join(lines)


_ALERT_TITLES = {
    "stranger": ("🚫 <b>Попытка входа без приглашения</b>", "Доступ не выдан."),
    "invalid": ("❓ <b>Вход по неверной ссылке</b>", "Ссылка не существует. Доступ не выдан."),
    "expired": ("⌛️ <b>Вход по просроченной ссылке</b>", "Приглашение истекло или отозвано. Доступ не выдан."),
    "blocked": ("🚫 <b>Заблокированный пытается войти</b>", "Доступ не выдан."),
    "locked": (
        "🔒 <b>Подбор ссылок</b>",
        "Слишком много неудачных попыток — дальнейшие попытки игнорируются час.",
    ),
}


def access_alert(tg, reason: str, invite=None) -> str:
    title, tail = _ALERT_TITLES.get(reason, ("⚠️ <b>Попытка входа</b>", ""))
    lines = [title, "", *person_lines(tg)]
    if invite is not None:
        lines += ["", invite_line(invite)]
    lines += ["", tail, "<i>О повторных попытках этого человека — не чаще раза в сутки.</i>"]
    return "\n".join(lines)


def admin_panel(users: list, pending: int, invites: int, viewer_is_owner: bool = True) -> str:
    active = [u for u in users if u.access_status == "active"]
    blocked = sum(1 for u in users if u.access_status == "blocked")
    online = sum(1 for u in active if is_online(u))
    admins = sum(1 for u in active if u.role == "admin")
    return "\n".join(
        [
            "👑 <b>АДМИН-ПАНЕЛЬ</b>" if viewer_is_owner else "⭐ <b>АДМИН-ПАНЕЛЬ</b> · вы админ",
            "",
            f"👥 Пользователей с доступом: <b>{len(active)}</b>",
            f"⭐ Админов: <b>{admins}</b>",
            f"🟢 Сейчас в боте: <b>{online}</b>",
            f"⏳ Заявок: <b>{pending}</b>",
            f"🔗 Активных приглашений: <b>{invites}</b>",
            f"🚫 Заблокировано: <b>{blocked}</b>",
            "",
            "<i>Вход — только по одноразовым приглашениям. "
            "О каждом входе и каждой подозрительной попытке приходит уведомление.</i>",
        ]
    )


def admin_users(users: list, owner_ids: set[int]) -> str:
    if not users:
        return "👥 <b>Пользователи</b>\n\nПока никого."
    order = {"active": 1, "pending": 2, "denied": 3, "blocked": 4}
    users = sorted(
        users,
        key=lambda u: (0 if u.id in owner_ids else 1 if u.role == "admin" and u.access_status == "active"
                       else order.get(u.access_status, 9) + 1,
                       -(u.last_seen_at.timestamp() if u.last_seen_at else 0)),
    )
    lines = ["👥 <b>Пользователи</b>", ""]
    for user in users:
        crown = "👑 " if user.id in owner_ids else "⭐ " if user.role == "admin" else ""
        icon = ACCESS_STATUS_ICON.get(user.access_status, "⚪")
        if user.access_status == "active":
            state = "сейчас в боте" if is_online(user) else f"был {fmt_ago(user.last_seen_at)}"
        else:
            state = ACCESS_STATUS_TITLES.get(user.access_status, user.access_status)[2:]
        lines.append(f"{icon} {crown}<b>{esc(user.display_name)}</b> · {state}")
    lines += ["", "👑 владелец · ⭐ админ", "Нажмите на человека, чтобы открыть карточку."]
    return "\n".join(lines)


def admin_user_card(user, servers: int, invite, is_owner_user: bool) -> str:
    title = (
        "👑 <b>Владелец</b>" if is_owner_user
        else "⭐ <b>Админ</b>" if user.role == "admin" and user.access_status == "active"
        else "👤 <b>Пользователь</b>"
    )
    lines = [
        title,
        "",
        *person_lines(user),
        "",
        f"Статус: {ACCESS_STATUS_TITLES.get(user.access_status, user.access_status)}",
        f"Активность: {'🟢 сейчас в боте' if is_online(user) else fmt_ago(user.last_seen_at)}"
        if user.access_status == "active" else f"Последняя попытка: {fmt_ago(user.last_seen_at)}",
        f"Впервые: {fmt_dt(user.created_at, _TZ)}",
        f"Серверов: {servers}",
    ]
    if not is_owner_user:
        lines.append(invite_line(invite))
    if not user.is_active:
        lines.append("⚠️ Заблокировал бота — сообщения ему не доходят")
    return "\n".join(lines)


def invite_created(invite, link: str) -> str:
    ttl = invite.expires_at - invite.created_at
    hours = round(ttl.total_seconds() / 3600)
    ttl_text = f"{hours} ч" if hours < 48 else f"{hours // 24} дн"
    mode = (
        "с вашим подтверждением — войти можно только после «✅ Пустить»"
        if invite.mode == "approve"
        else "вход сразу — вам придёт уведомление"
    )
    lines = [
        f"🔗 <b>Приглашение #{invite.id} создано</b>",
        "",
        f"<code>{esc(link)}</code>",
        "",
        "• Одноразовое: сгорит после первого входа",
        f"• Действует до {fmt_dt(invite.expires_at, _TZ)} ({ttl_text})",
        f"• Режим: {mode}",
    ]
    if invite.note:
        lines.append(f"• Для: {esc(invite.note)}")
    lines += [
        "",
        "⚠️ Ссылка показывается <b>один раз</b> — в базе хранится только её отпечаток. "
        "Отправьте её человеку лично.",
    ]
    if invite.mode == "approve":
        lines.append("Если ссылку перешлют, вы увидите, кто пришёл на самом деле, и сможете отказать.")
    return "\n".join(lines)


def invites_list(invites: list) -> str:
    if not invites:
        return "🔗 <b>Активные приглашения</b>\n\nАктивных приглашений нет."
    lines = ["🔗 <b>Активные приглашения</b>", ""]
    for invite in invites:
        note = f" «{esc(invite.note)}»" if invite.note else ""
        mode = "✋" if invite.mode == "approve" else "⚡"
        lines.append(f"{mode} #{invite.id}{note} · до {fmt_dt(invite.expires_at, _TZ)}")
    lines += ["", "✋ — с подтверждением · ⚡ — вход сразу", "Нажмите, чтобы отозвать."]
    return "\n".join(lines)


def access_log(events: list) -> str:
    from ..services.access import EVENT_TITLES

    if not events:
        return "📜 <b>Журнал доступа</b>\n\nСобытий пока нет."
    lines = ["📜 <b>Журнал доступа</b>", ""]
    for ev in events:
        who = esc(ev.full_name or (f"@{ev.username}" if ev.username else f"id{ev.tg_id}"))
        inv = f" · #{ev.invite_id}" if ev.invite_id else ""
        lines.append(
            f"<code>{fmt_dt(ev.created_at, _TZ)}</code> {EVENT_TITLES.get(ev.event, ev.event)}{inv}\n"
            f"      {who} (<code>{ev.tg_id}</code>)"
        )
    return "\n".join(lines)[:4000]
