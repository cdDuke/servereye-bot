"""Inline-клавиатуры ServerEye."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ...database.models import (
    CHECK_HTTP,
    CHECK_PING,
    CHECK_PING_HTTP,
    CHECK_TCP,
    STATUS_OFFLINE,
    STATUS_ONLINE,
    STATUS_UNKNOWN,
    STATUS_WARNING,
    Server,
)

BACK_MAIN = "menu:main"

_STATUS_EMOJI = {
    STATUS_ONLINE: "🟢",
    STATUS_WARNING: "🟡",
    STATUS_OFFLINE: "🔴",
    STATUS_UNKNOWN: "⚪",
}


def main_menu(is_owner: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="🖥 Мои серверы", callback_data="menu:servers"),
        InlineKeyboardButton(text="➕ Подключить сервер", callback_data="menu:add"),
    )
    kb.row(InlineKeyboardButton(text="⚡ СРОЧНАЯ ПРОВЕРКА", callback_data="menu:urgent"))
    kb.row(
        InlineKeyboardButton(text="📊 Статистика", callback_data="menu:stats"),
        InlineKeyboardButton(text="🧠 Состояние", callback_data="menu:system"),
    )
    kb.row(
        InlineKeyboardButton(text="🔔 Уведомления", callback_data="menu:notif"),
        InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu:settings"),
    )
    kb.row(InlineKeyboardButton(text="📊 Отчёт за час", callback_data="menu:report"))
    if is_owner:
        kb.row(InlineKeyboardButton(text="👑 Админ-панель", callback_data="adm:main"))
    return kb.as_markup()


def back_main(extra: list[tuple[str, str]] | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for text, data in extra or []:
        kb.row(InlineKeyboardButton(text=text, callback_data=data))
    kb.row(InlineKeyboardButton(text="⬅️ Главное меню", callback_data=BACK_MAIN))
    return kb.as_markup()


def servers_list(servers: list[Server]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for server in servers:
        mark = _STATUS_EMOJI.get(server.status, "⚪")
        kb.row(
            InlineKeyboardButton(
                text=f"{mark} {server.name}", callback_data=f"srv:view:{server.id}"
            )
        )
    kb.row(
        InlineKeyboardButton(text="➕ Подключить", callback_data="menu:add"),
        InlineKeyboardButton(text="⚡ Проверить все", callback_data="menu:urgent"),
    )
    kb.row(InlineKeyboardButton(text="⬅️ Главное меню", callback_data=BACK_MAIN))
    return kb.as_markup()


def server_card(
    server: Server, can_power: bool = False, can_manage: bool = True
) -> InlineKeyboardMarkup:
    sid = server.id
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="⚡ Проверить сейчас", callback_data=f"srv:check:{sid}"),
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"srv:view:{sid}"),
    )
    kb.row(
        InlineKeyboardButton(text="📈 Графики", callback_data=f"srv:charts:{sid}"),
        InlineKeyboardButton(text="📊 Статистика", callback_data=f"srv:stats:{sid}"),
    )
    if can_manage:
        kb.row(
            InlineKeyboardButton(text="📊 История", callback_data=f"srv:hist:{sid}"),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data=f"srv:set:{sid}"),
        )
    else:
        kb.row(InlineKeyboardButton(text="📊 История", callback_data=f"srv:hist:{sid}"))
    if can_power:
        kb.row(InlineKeyboardButton(text="⏻ Выключить сервер", callback_data=f"pwr:ask:{sid}"))
    if can_manage:
        kb.row(
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"srv:del:{sid}"),
            InlineKeyboardButton(text="⬅️ К списку", callback_data="menu:servers"),
        )
    else:
        kb.row(InlineKeyboardButton(text="⬅️ К списку", callback_data="menu:servers"))
    return kb.as_markup()


def power_confirm(server_id: int, stamp: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="⏻ Да, выключить", callback_data=f"pwr:do:{server_id}:{stamp}"
        )
    )
    kb.row(InlineKeyboardButton(text="✖️ Отмена", callback_data=f"srv:view:{server_id}"))
    return kb.as_markup()


def hardware_alert_actions(server: Server, can_power: bool = False) -> InlineKeyboardMarkup:
    """Кнопки под тревогой о перегреве или питании."""
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="🖥 Открыть сервер", callback_data=f"srv:view:{server.id}"),
        InlineKeyboardButton(text="🌡 График", callback_data=f"srv:chart:{server.id}:temp:24"),
    )
    if server.is_local_host and can_power:
        kb.row(InlineKeyboardButton(text="⏻ Выключить сервер", callback_data=f"pwr:ask:{server.id}"))
    return kb.as_markup()


def server_charts(server_id: int, with_agent: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="📈 Ping", callback_data=f"srv:chart:{server_id}:ping:24"),
        InlineKeyboardButton(text="📈 Uptime", callback_data=f"srv:chart:{server_id}:uptime:24"),
    )
    if with_agent:
        kb.row(
            InlineKeyboardButton(text="⚙️ CPU", callback_data=f"srv:chart:{server_id}:cpu:24"),
            InlineKeyboardButton(text="🧠 RAM", callback_data=f"srv:chart:{server_id}:ram:24"),
        )
        kb.row(
            InlineKeyboardButton(text="💾 Disk", callback_data=f"srv:chart:{server_id}:disk:24"),
            InlineKeyboardButton(text="📡 Network", callback_data=f"srv:chart:{server_id}:net:24"),
        )
        kb.row(
            InlineKeyboardButton(text="🌡 Температура", callback_data=f"srv:chart:{server_id}:temp:24"),
            InlineKeyboardButton(text="🔋 Батарея", callback_data=f"srv:chart:{server_id}:battery:24"),
        )
    kb.row(InlineKeyboardButton(text="⬅️ К серверу", callback_data=f"srv:view:{server_id}"))
    return kb.as_markup()


def chart_periods(server_id: int, metric: str, current: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    buttons = []
    for hours, label in ((24, "24 часа"), (24 * 7, "7 дней"), (24 * 30, "30 дней")):
        text = ("• " if hours == current else "") + label
        buttons.append(
            InlineKeyboardButton(
                text=text, callback_data=f"srv:chart:{server_id}:{metric}:{hours}"
            )
        )
    kb.row(*buttons)
    kb.row(
        InlineKeyboardButton(text="📈 Другие графики", callback_data=f"srv:charts:{server_id}"),
        InlineKeyboardButton(text="⬅️ К серверу", callback_data=f"srv:view:{server_id}"),
    )
    return kb.as_markup()


def server_settings(server: Server) -> InlineKeyboardMarkup:
    sid = server.id
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text=f"🔔 Уведомления: {'вкл' if server.notifications_enabled else 'выкл'}",
            callback_data=f"srv:toggle_notify:{sid}",
        )
    )
    kb.row(
        InlineKeyboardButton(text=f"CPU warning: {server.cpu_warning:.0f}%", callback_data=f"srv:thr:{sid}:cpu_warning"),
        InlineKeyboardButton(text=f"CPU critical: {server.cpu_critical:.0f}%", callback_data=f"srv:thr:{sid}:cpu_critical"),
    )
    kb.row(
        InlineKeyboardButton(text=f"RAM warning: {server.ram_warning:.0f}%", callback_data=f"srv:thr:{sid}:ram_warning"),
        InlineKeyboardButton(text=f"RAM critical: {server.ram_critical:.0f}%", callback_data=f"srv:thr:{sid}:ram_critical"),
    )
    kb.row(
        InlineKeyboardButton(text=f"Disk warning: {server.disk_warning:.0f}%", callback_data=f"srv:thr:{sid}:disk_warning"),
        InlineKeyboardButton(text=f"Disk critical: {server.disk_critical:.0f}%", callback_data=f"srv:thr:{sid}:disk_critical"),
    )
    kb.row(
        InlineKeyboardButton(
            text=f"Ping warning: {server.ping_warning:.0f} ms" if server.ping_warning else "Ping warning: как в общих",
            callback_data=f"srv:thr:{sid}:ping_warning",
        ),
        InlineKeyboardButton(
            text=f"Ping critical: {server.ping_critical:.0f} ms" if server.ping_critical else "Ping critical: как в общих",
            callback_data=f"srv:thr:{sid}:ping_critical",
        ),
    )
    if server.agent_enabled:
        kb.row(
            InlineKeyboardButton(
                text=f"🌡 Тревога по CPU от: {server.cpu_temp_alarm:.0f}°C",
                callback_data=f"srv:thr:{sid}:cpu_temp_alarm",
            )
        )
    kb.row(InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"srv:rename:{sid}"))
    kb.row(InlineKeyboardButton(text="⬅️ К серверу", callback_data=f"srv:view:{sid}"))
    return kb.as_markup()


def server_agent(server: Server) -> InlineKeyboardMarkup:
    sid = server.id
    kb = InlineKeyboardBuilder()
    if server.agent_enabled:
        kb.row(InlineKeyboardButton(text="🔑 Перевыпустить токен", callback_data=f"srv:agentnew:{sid}"))
        kb.row(InlineKeyboardButton(text="🗑 Отключить Agent", callback_data=f"srv:agentoff:{sid}"))
    else:
        kb.row(InlineKeyboardButton(text="🖥 Включить мониторинг ресурсов", callback_data=f"srv:agentnew:{sid}"))
    kb.row(InlineKeyboardButton(text="⬅️ К серверу", callback_data=f"srv:view:{sid}"))
    return kb.as_markup()


def confirm_delete(server_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"srv:delok:{server_id}"),
        InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"srv:view:{server_id}"),
    )
    return kb.as_markup()


# --------------------------------------------------------- добавление сервера ---
def check_types() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🔹 Ping", callback_data=f"add:type:{CHECK_PING}"))
    kb.row(InlineKeyboardButton(text="🔹 HTTP / HTTPS", callback_data=f"add:type:{CHECK_HTTP}"))
    kb.row(InlineKeyboardButton(text="🔹 TCP-порт", callback_data=f"add:type:{CHECK_TCP}"))
    kb.row(InlineKeyboardButton(text="🔹 Ping + HTTP", callback_data=f"add:type:{CHECK_PING_HTTP}"))
    kb.row(InlineKeyboardButton(text="❌ Отмена", callback_data="add:cancel"))
    return kb.as_markup()


def ports_step(optional: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if optional:
        kb.row(InlineKeyboardButton(text="⏭ Пропустить", callback_data="add:skip_ports"))
    kb.row(InlineKeyboardButton(text="❌ Отмена", callback_data="add:cancel"))
    return kb.as_markup()


def agent_choice() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🖥 Да, добавить мониторинг ресурсов", callback_data="add:agent:1"))
    kb.row(InlineKeyboardButton(text="Пока нет", callback_data="add:agent:0"))
    return kb.as_markup()


def cancel_only() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="❌ Отмена", callback_data="add:cancel"))
    return kb.as_markup()


def after_add(server_id: int, agent_enabled: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="⚡ Проверить сейчас", callback_data=f"srv:check:{server_id}"))
    if agent_enabled:
        kb.row(InlineKeyboardButton(text="🔐 Инструкция по Agent", callback_data=f"srv:agent:{server_id}"))
    kb.row(
        InlineKeyboardButton(text="🖥 К серверу", callback_data=f"srv:view:{server_id}"),
        InlineKeyboardButton(text="⬅️ Меню", callback_data=BACK_MAIN),
    )
    return kb.as_markup()


# --------------------------------------------------------------- уведомления ---
NOTIFY_TOGGLES = (
    ("notify_server_down", "Сервер отключился"),
    ("notify_server_up", "Сервер восстановился"),
    ("notify_high_ping", "Высокий Ping"),
    ("notify_packet_loss", "Потеря пакетов"),
    ("notify_cpu", "Высокая загрузка CPU"),
    ("notify_ram", "Высокая загрузка RAM"),
    ("notify_disk", "Мало места на диске"),
    ("notify_temp", "Перегрев процессора"),
    ("notify_power", "Пропало питание"),
    ("notify_agent_offline", "Мониторинг ресурсов молчит"),
    ("notify_trend", "Аналитика и тенденции"),
    ("hourly_report", "Ежечасный отчёт"),
    ("daily_report", "Ежедневный отчёт"),
)


def notifications(st) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key, label in NOTIFY_TOGGLES:
        mark = "☑" if getattr(st, key, False) else "☐"
        kb.row(InlineKeyboardButton(text=f"{mark} {label}", callback_data=f"nt:toggle:{key}"))
    kb.row(
        InlineKeyboardButton(text=f"⚙️ Ping warning: {st.ping_warning_ms} ms", callback_data="nt:thr:ping_warning_ms"),
        InlineKeyboardButton(text=f"Ping critical: {st.ping_critical_ms} ms", callback_data="nt:thr:ping_critical_ms"),
    )
    kb.row(
        InlineKeyboardButton(
            text=f"⚙️ Потери пакетов выше: {st.packet_loss_pct:.0f}%",
            callback_data="nt:thr:packet_loss_pct",
        )
    )
    kb.row(InlineKeyboardButton(text="⬅️ Главное меню", callback_data=BACK_MAIN))
    return kb.as_markup()


def settings_menu(st) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text=f"📊 Ежечасный отчёт: {'вкл' if st.hourly_report else 'выкл'}",
            callback_data="st:toggle:hourly_report",
        )
    )
    kb.row(
        InlineKeyboardButton(
            text=f"📅 Ежедневный отчёт: {'вкл' if st.daily_report else 'выкл'}",
            callback_data="st:toggle:daily_report",
        )
    )
    kb.row(InlineKeyboardButton(text=f"🕒 Часовой пояс: UTC{st.tz_offset:+d}", callback_data="st:tz"))
    kb.row(InlineKeyboardButton(text="🔔 Уведомления", callback_data="menu:notif"))
    kb.row(InlineKeyboardButton(text="🧹 Удалить мои данные", callback_data="st:wipe"))
    kb.row(InlineKeyboardButton(text="⬅️ Главное меню", callback_data=BACK_MAIN))
    return kb.as_markup()


def tz_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    row: list[InlineKeyboardButton] = []
    for offset in range(-2, 13):
        row.append(InlineKeyboardButton(text=f"UTC{offset:+d}", callback_data=f"st:tzset:{offset}"))
        if len(row) == 5:
            kb.row(*row)
            row = []
    if row:
        kb.row(*row)
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:settings"))
    return kb.as_markup()


def confirm_wipe() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🧹 Да, удалить всё", callback_data="st:wipeok"))
    kb.row(InlineKeyboardButton(text="⬅️ Отмена", callback_data="menu:settings"))
    return kb.as_markup()


def cancel_input(back: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="❌ Отмена", callback_data=back))
    return kb.as_markup()


def stats_menu(servers: list[Server]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for server in servers[:10]:
        mark = _STATUS_EMOJI.get(server.status, "⚪")
        kb.row(
            InlineKeyboardButton(
                text=f"{mark} {server.name}", callback_data=f"srv:stats:{server.id}"
            )
        )
    kb.row(
        InlineKeyboardButton(text="🧠 Состояние системы", callback_data="menu:system"),
        InlineKeyboardButton(text="📊 Отчёт за час", callback_data="menu:report"),
    )
    kb.row(InlineKeyboardButton(text="⬅️ Главное меню", callback_data=BACK_MAIN))
    return kb.as_markup()


def notification_actions(server_id: int | None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if server_id:
        kb.row(
            InlineKeyboardButton(text="🖥 Открыть сервер", callback_data=f"srv:view:{server_id}"),
            InlineKeyboardButton(text="⚡ Проверить", callback_data=f"srv:check:{server_id}"),
        )
    kb.row(InlineKeyboardButton(text="🏠 Меню", callback_data=BACK_MAIN))
    return kb.as_markup()


# ------------------------------------------------------------ доступ и админка ---
ADMIN_BACK = "adm:main"


def _admin_back(kb: InlineKeyboardBuilder, back: str = ADMIN_BACK) -> None:
    kb.row(
        InlineKeyboardButton(text="⬅️ Назад", callback_data=back),
        InlineKeyboardButton(text="🏠 Меню", callback_data=BACK_MAIN),
    )


def access_alert_actions(tg_id: int, blocked: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if blocked:
        kb.row(InlineKeyboardButton(text="♻️ Разблокировать", callback_data=f"adm:unblock:{tg_id}"))
    else:
        kb.row(InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"adm:block:{tg_id}"))
    kb.row(InlineKeyboardButton(text="👑 Админ-панель", callback_data=ADMIN_BACK))
    return kb.as_markup()


def access_joined_actions(tg_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="🚫 Забрать доступ", callback_data=f"adm:block:{tg_id}"),
        InlineKeyboardButton(text="👤 Карточка", callback_data=f"adm:user:{tg_id}"),
    )
    return kb.as_markup()


def access_request_actions(tg_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ Пустить", callback_data=f"adm:approve:{tg_id}"),
        InlineKeyboardButton(text="❌ Отказать", callback_data=f"adm:deny:{tg_id}"),
    )
    kb.row(InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"adm:block:{tg_id}"))
    return kb.as_markup()


def admin_panel(pending: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="👥 Пользователи", callback_data="adm:users"),
        InlineKeyboardButton(
            text=f"⏳ Заявки ({pending})" if pending else "⏳ Заявки", callback_data="adm:requests"
        ),
    )
    kb.row(
        InlineKeyboardButton(text="➕ Пригласить", callback_data="adm:inv:new"),
        InlineKeyboardButton(text="🔗 Приглашения", callback_data="adm:inv:list"),
    )
    kb.row(
        InlineKeyboardButton(text="📜 Журнал доступа", callback_data="adm:log"),
        InlineKeyboardButton(text="🔄 Обновить", callback_data=ADMIN_BACK),
    )
    kb.row(InlineKeyboardButton(text="🏠 Главное меню", callback_data=BACK_MAIN))
    return kb.as_markup()


def admin_users(users: list, back: str = ADMIN_BACK) -> InlineKeyboardMarkup:
    icons = {"active": "🟢", "pending": "⏳", "denied": "❌", "blocked": "🚫"}
    order = {"active": 2, "pending": 3, "denied": 4, "blocked": 5}
    roles = {"owner": "👑 ", "admin": "⭐ "}
    users = sorted(
        users,
        key=lambda u: (0 if u.role == "owner" else 1 if u.role == "admin" and u.access_status == "active"
                       else order.get(u.access_status, 9)),
    )
    kb = InlineKeyboardBuilder()
    for user in users[:40]:
        kb.row(
            InlineKeyboardButton(
                text=f"{icons.get(user.access_status, '⚪')} {roles.get(user.role, '')}{user.display_name}"[:60],
                callback_data=f"adm:user:{user.id}",
            )
        )
    _admin_back(kb, back)
    return kb.as_markup()


def admin_user(
    user, target_is_owner: bool, can_moderate: bool, can_set_admin: bool
) -> InlineKeyboardMarkup:
    uid = user.id
    kb = InlineKeyboardBuilder()
    status = user.access_status
    if not target_is_owner:
        if can_set_admin:
            if user.role == "admin":
                kb.row(InlineKeyboardButton(text="⭐ Снять админку", callback_data=f"adm:rmadm:{uid}"))
            else:
                kb.row(InlineKeyboardButton(text="⭐ Сделать админом", callback_data=f"adm:mkadm:{uid}"))
        if can_moderate:
            if status == "pending":
                kb.row(
                    InlineKeyboardButton(text="✅ Пустить", callback_data=f"adm:approve:{uid}"),
                    InlineKeyboardButton(text="❌ Отказать", callback_data=f"adm:deny:{uid}"),
                )
            elif status == "denied":
                kb.row(
                    InlineKeyboardButton(text="✅ Пустить", callback_data=f"adm:approve:{uid}"),
                    InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"adm:block:{uid}"),
                )
            elif status == "active":
                kb.row(InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"adm:block:{uid}"))
            elif status == "blocked":
                kb.row(InlineKeyboardButton(text="♻️ Разблокировать", callback_data=f"adm:unblock:{uid}"))
            kb.row(InlineKeyboardButton(text="🗑 Удалить вместе с данными", callback_data=f"adm:rm:{uid}"))
    kb.row(InlineKeyboardButton(text="🔄 Обновить", callback_data=f"adm:user:{uid}"))
    _admin_back(kb, "adm:users")
    return kb.as_markup()


def admin_confirm_grant(uid: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="⭐ Да, сделать админом", callback_data=f"adm:mkadmok:{uid}"),
        InlineKeyboardButton(text="✖️ Отмена", callback_data=f"adm:user:{uid}"),
    )
    return kb.as_markup()


def admin_confirm_remove(uid: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"adm:rmok:{uid}"),
        InlineKeyboardButton(text="✖️ Отмена", callback_data=f"adm:user:{uid}"),
    )
    return kb.as_markup()


def invite_mode() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="✋ С моим подтверждением (рекомендую)", callback_data="adm:inv:mode:approve"))
    kb.row(InlineKeyboardButton(text="⚡ Пускать сразу", callback_data="adm:inv:mode:instant"))
    kb.row(InlineKeyboardButton(text="✖️ Отмена", callback_data=ADMIN_BACK))
    return kb.as_markup()


def invite_ttl() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="1 час", callback_data="adm:inv:ttl:1"),
        InlineKeyboardButton(text="24 часа", callback_data="adm:inv:ttl:24"),
        InlineKeyboardButton(text="7 дней", callback_data="adm:inv:ttl:168"),
    )
    kb.row(InlineKeyboardButton(text="✖️ Отмена", callback_data=ADMIN_BACK))
    return kb.as_markup()


def invite_note() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="⏭ Без подписи", callback_data="adm:inv:nonote"))
    kb.row(InlineKeyboardButton(text="✖️ Отмена", callback_data=ADMIN_BACK))
    return kb.as_markup()


def invite_created(invite_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="🗑 Отозвать", callback_data=f"adm:inv:rv:{invite_id}"),
        InlineKeyboardButton(text="➕ Ещё одно", callback_data="adm:inv:new"),
    )
    kb.row(InlineKeyboardButton(text="👑 Админ-панель", callback_data=ADMIN_BACK))
    return kb.as_markup()


def invites_list(invites: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for invite in invites:
        note = f" {invite.note}" if invite.note else ""
        kb.row(
            InlineKeyboardButton(
                text=f"🗑 Отозвать #{invite.id}{note}"[:60], callback_data=f"adm:inv:rv:{invite.id}"
            )
        )
    kb.row(InlineKeyboardButton(text="➕ Создать приглашение", callback_data="adm:inv:new"))
    _admin_back(kb)
    return kb.as_markup()


def admin_log() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🔄 Обновить", callback_data="adm:log"))
    _admin_back(kb)
    return kb.as_markup()
