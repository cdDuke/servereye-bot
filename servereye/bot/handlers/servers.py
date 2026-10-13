"""Раздел «Мои серверы»: карточка, графики, история, настройки, агент."""
from __future__ import annotations

import logging
from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import get_settings
from ...database import repository as repo
from ...database.models import Server, User, UserSettings
from ...services import charts as charts_service
from ...services import pipeline
from ...services.notifier import Notifier
from ...services import access
from ...services.power import is_power_admin
from ...utils import clean_name, esc, fmt_mbps, fmt_ms, sparkline, utcnow
from .. import texts
from ..keyboards import inline as kb
from .helpers import edit

log = logging.getLogger(__name__)
router = Router(name="servers")
settings = get_settings()

CHART_TITLES = {
    "ping": ("📈 Ping", "ms"),
    "uptime": ("📈 Uptime", "%"),
    "cpu": ("⚙️ CPU", "%"),
    "ram": ("🧠 RAM", "MB"),
    "disk": ("💾 Disk", "%"),
    "net": ("📡 Network", "Mbps"),
    "temp": ("🌡 Температура", "°C"),
    "battery": ("🔋 Батарея", "%"),
}


# Единственные поля сервера, которые можно менять через редактор порогов.
# Имя поля приходит из callback_data, поэтому без белого списка подделанная
# кнопка могла бы переписать любое поле модели (например, user_id).
THRESHOLDS: dict[str, tuple[float, float, str]] = {
    "cpu_warning": (1, 100, "%"),
    "cpu_critical": (1, 100, "%"),
    "ram_warning": (1, 100, "%"),
    "ram_critical": (1, 100, "%"),
    "disk_warning": (1, 100, "%"),
    "disk_critical": (1, 100, "%"),
    "ping_warning": (1, 100000, "ms"),
    "ping_critical": (1, 100000, "ms"),
    "cpu_temp_alarm": (50, 110, "°C"),
}


def can_power(server: Server, user: User) -> bool:
    return server.is_local_host and is_power_admin(user.id)


class ServerEdit(StatesGroup):
    threshold = State()
    rename = State()


async def _load_managed(session: AsyncSession, call: CallbackQuery, user: User) -> Server | None:
    """Сервер, который этот пользователь вправе менять или удалять."""
    server = await _load(session, call, user)
    if server is None:
        return None
    if not access.can_manage(server, user):
        await call.answer(
            "🔒 Менять этот сервер может тот, кто его добавил, админы и владелец бота",
            show_alert=True,
        )
        return None
    return server


async def _load(session: AsyncSession, call: CallbackQuery, user: User) -> Server | None:
    try:
        server_id = int(call.data.split(":")[2])
    except (IndexError, ValueError):
        await call.answer("Некорректный запрос", show_alert=True)
        return None
    server = await repo.get_server(session, server_id, user.id)
    if server is None:
        await call.answer("Сервер не найден", show_alert=True)
        await edit(call, "Этот сервер больше недоступен.", kb.back_main([("🖥 Мои серверы", "menu:servers")]))
        return None
    return server


# ------------------------------------------------------------------- список ---
async def render_list(session: AsyncSession, user_id: int) -> tuple[str, object]:
    servers = await repo.list_servers(session, user_id)
    rows = []
    creators: dict[int, str] = {}
    for server in servers:
        stats = await repo.server_stats(session, server.id, hours=24)
        rows.append((server, stats))
        if server.user_id not in creators:
            creator = await session.get(User, server.user_id)
            creators[server.user_id] = (creator.first_name or creator.display_name) if creator else "?"
    return texts.servers_list(rows, creators, user_id), kb.servers_list(servers)


@router.callback_query(F.data == "menu:servers")
async def show_servers(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    text, markup = await render_list(session, user.id)
    await edit(call, text, markup)
    await call.answer()


@router.message(Command("servers"))
async def cmd_servers(message: Message, session: AsyncSession, user: User) -> None:
    text, markup = await render_list(session, user.id)
    await message.answer(text, reply_markup=markup, disable_web_page_preview=True)


# ------------------------------------------------------------------ карточка ---
def card_keyboard(server: Server, user: User):
    return kb.server_card(server, can_power(server, user), access.can_manage(server, user))


async def render_card(
    session: AsyncSession, server: Server, tz: int, viewer_id: int | None = None
) -> str:
    stats24 = await repo.server_stats(session, server.id, hours=24)
    stats7 = await repo.server_stats(session, server.id, hours=24 * 7)
    stats30 = await repo.server_stats(session, server.id, hours=24 * 30)
    metric = await repo.latest_metric(session, server.id) if server.agent_enabled else None
    creator = await session.get(User, server.user_id)
    return texts.server_card(server, stats24, stats7, stats30, metric, tz, creator=creator, viewer_id=viewer_id)


@router.callback_query(F.data.startswith("srv:view:"))
async def view_server(
    call: CallbackQuery, session: AsyncSession, user: User, user_settings: UserSettings
) -> None:
    server = await _load(session, call, user)
    if server is None:
        return
    await edit(
        call,
        await render_card(session, server, user_settings.tz_offset, user.id),
        card_keyboard(server, user),
    )
    await call.answer()


@router.callback_query(F.data.startswith("srv:check:"))
async def check_server(
    call: CallbackQuery, session: AsyncSession, user: User, user_settings: UserSettings
) -> None:
    server = await _load(session, call, user)
    if server is None:
        return
    await call.answer("Проверяю...")
    await edit(call, f"⏳ Проверяю <b>{esc(server.name)}</b>...", None)
    notifier = Notifier(call.bot)
    result = await pipeline.check_single(session, server, user_settings, notifier)
    metric = await repo.latest_metric(session, server.id) if server.agent_enabled else None
    block = texts.server_check_block(server, result, metric)
    lines = [
        "⚡ <b>Результат проверки</b>",
        "",
        f"{block['emoji']} <b>{esc(block['name'])}</b>",
        *block["lines"],
        "",
        await _quick_compare(session, server, result),
    ]
    await edit(
        call,
        "\n".join(x for x in lines if x is not None),
        card_keyboard(server, user),
    )


async def _quick_compare(session: AsyncSession, server: Server, result) -> str:
    stats = await repo.server_stats(session, server.id, hours=24)
    if result.ping is None or stats.ping_avg is None or stats.total < 3:
        return "<i>Данных для сравнения пока мало.</i>"
    delta = (result.ping - stats.ping_avg) / stats.ping_avg * 100
    if delta >= 50:
        return f"⚠️ Ping выше обычного на {delta:.0f}% (средний {fmt_ms(stats.ping_avg)})."
    if delta <= -30:
        return f"📈 Ping лучше обычного на {abs(delta):.0f}% (средний {fmt_ms(stats.ping_avg)})."
    return f"✅ Показатели в норме (средний Ping {fmt_ms(stats.ping_avg)})."


# ----------------------------------------------------------------- статистика ---
@router.callback_query(F.data.startswith("srv:stats:"))
async def server_stats(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    server = await _load(session, call, user)
    if server is None:
        return
    stats24 = await repo.server_stats(session, server.id, hours=24)
    stats7 = await repo.server_stats(session, server.id, hours=24 * 7)
    stats30 = await repo.server_stats(session, server.id, hours=24 * 30)
    await edit(
        call,
        texts.server_stats_text(server, stats24, stats7, stats30),
        kb.back_main(
            [
                ("📈 Графики", f"srv:charts:{server.id}"),
                ("⬅️ К серверу", f"srv:view:{server.id}"),
            ]
        ),
    )
    await call.answer()


@router.callback_query(F.data.startswith("srv:hist:"))
async def server_history(
    call: CallbackQuery, session: AsyncSession, user: User, user_settings: UserSettings
) -> None:
    server = await _load(session, call, user)
    if server is None:
        return
    checks = await repo.last_checks(session, server.id, limit=15)
    await edit(
        call,
        texts.server_history(server, checks, user_settings.tz_offset),
        kb.back_main(
            [
                ("🔄 Обновить", f"srv:hist:{server.id}"),
                ("⬅️ К серверу", f"srv:view:{server.id}"),
            ]
        ),
    )
    await call.answer()


# -------------------------------------------------------------------- графики ---
@router.callback_query(F.data.startswith("srv:charts:"))
async def charts_menu(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    server = await _load(session, call, user)
    if server is None:
        return
    connected, agent_text = texts.agent_state(server)
    text = [
        f"📈 <b>Графики — {esc(server.name)}</b>",
        "",
        "Выберите показатель:",
    ]
    if not server.agent_enabled:
        text += [
            "",
            "<i>CPU, RAM, Disk и Network доступны после подключения "
            "ServerEye Agent на сервере.</i>",
        ]
    await edit(call, "\n".join(text), kb.server_charts(server.id, server.agent_enabled))
    await call.answer()


@router.callback_query(F.data.startswith("srv:chart:"))
async def render_chart(
    call: CallbackQuery, session: AsyncSession, user: User, user_settings: UserSettings
) -> None:
    parts = call.data.split(":")
    if len(parts) < 5:
        await call.answer("Некорректный запрос", show_alert=True)
        return
    server_id, metric, hours = int(parts[2]), parts[3], int(parts[4])
    server = await repo.get_server(session, server_id, user.id)
    if server is None:
        await call.answer("Сервер не найден", show_alert=True)
        return

    await call.answer("Строю график...")
    title, ylabel = CHART_TITLES.get(metric, ("График", ""))
    period = {24: "24 часа", 168: "7 дней", 720: "30 дней"}.get(hours, f"{hours} ч")
    caption = f"{title} — {esc(server.name)} · {period}"

    image: bytes | None = None
    fallback_lines: list[str] = []

    if metric == "uptime":
        values, labels = [], []
        for h, label in ((24, "24 часа"), (24 * 7, "7 дней"), (24 * 30, "30 дней")):
            stats = await repo.server_stats(session, server.id, hours=h)
            if stats.uptime is not None:
                values.append(stats.uptime)
                labels.append(label)
        if values:
            image = await charts_service.bar_chart(labels, values, f"Uptime — {server.name}", "%")
            fallback_lines = [f"{label}: {value:.2f}%" for label, value in zip(labels, values)]
        else:
            fallback_lines = ["Пока нет данных."]
    elif metric == "ping":
        since = utcnow() - timedelta(hours=hours)
        checks = await repo.checks_since(session, server.id, since)
        points = [(c.timestamp, c.ping) for c in checks if c.ping is not None]
        if points:
            xs = [p[0] + timedelta(hours=user_settings.tz_offset) for p in points]
            ys = [p[1] for p in points]
            image = await charts_service.line_chart(
                [("Ping", xs, ys, charts_service.ACCENT)],
                f"Ping — {server.name} ({period})",
                "ms",
                hours,
            )
            fallback_lines = [
                f"Ping: {sparkline(ys[-40:])}",
                f"Среднее: {fmt_ms(sum(ys) / len(ys))}, макс: {fmt_ms(max(ys))}",
            ]
        else:
            fallback_lines = ["Нет данных о ping за выбранный период."]
    else:
        since = utcnow() - timedelta(hours=hours)
        metrics = await repo.metrics_since(session, server.id, since)
        if not metrics:
            fallback_lines = [
                "Нет данных о нагрузке.",
                "Подключите ServerEye Agent на сервере — тогда появятся CPU, RAM, Disk и Network.",
            ]
        else:
            xs = [m.timestamp + timedelta(hours=user_settings.tz_offset) for m in metrics]
            if metric == "cpu":
                ys = [m.cpu_percent for m in metrics]
                series = [("CPU %", xs, ys, charts_service.WARN)]
                fallback_lines = [f"CPU: {sparkline(ys[-40:])}"]
            elif metric == "ram":
                ys = [m.ram_used_mb for m in metrics]
                series = [("RAM MB", xs, ys, charts_service.ACCENT)]
                fallback_lines = [f"RAM: {sparkline(ys[-40:])}"]
            elif metric == "disk":
                ys = [m.disk_percent for m in metrics]
                series = [("Disk %", xs, ys, charts_service.ACCENT2)]
                fallback_lines = [f"Disk: {sparkline(ys[-40:])}"]
            elif metric == "temp":
                cpu = [m.temperature_c for m in metrics]
                gpu = [m.gpu_temp_c for m in metrics]
                series = [("CPU °C", xs, cpu, charts_service.CRIT)]
                fallback_lines = [f"CPU: {sparkline(cpu[-40:])}"]
                if any(v is not None for v in gpu):
                    series.append(("GPU °C", xs, gpu, charts_service.WARN))
                    fallback_lines.append(f"GPU: {sparkline(gpu[-40:])}")
            elif metric == "battery":
                ys = [m.battery_percent for m in metrics]
                series = [("Заряд %", xs, ys, charts_service.ACCENT2)]
                fallback_lines = [f"Заряд: {sparkline(ys[-40:])}"]
            else:
                down = [m.net_down_mbps for m in metrics]
                up = [m.net_up_mbps for m in metrics]
                series = [
                    ("↓ Download", xs, down, charts_service.ACCENT),
                    ("↑ Upload", xs, up, charts_service.ACCENT2),
                ]
                fallback_lines = [
                    f"↓ {sparkline(down[-40:])}",
                    f"↑ {sparkline(up[-40:])}",
                ]
            # matplotlib не умеет None — отбрасываем пропуски по каждой серии
            series = [
                (
                    label,
                    [t for t, v in zip(x, y) if v is not None],
                    [v for v in y if v is not None],
                    color,
                )
                for label, x, y, color in series
            ]
            threshold = None
            if metric == "temp":
                alarm = float(server.cpu_temp_alarm or 90)
                threshold = (alarm, f"Тревога {alarm:.0f} °C")
            image = await charts_service.line_chart(
                series, f"{title} — {server.name} ({period})", ylabel, hours, threshold
            )

    markup = kb.chart_periods(server.id, metric, hours)
    if image:
        try:
            await call.message.delete()
        except Exception:  # noqa: BLE001
            pass
        await call.message.answer_photo(
            BufferedInputFile(image, filename=f"{metric}.png"),
            caption=caption,
            reply_markup=markup,
        )
    else:
        await edit(call, f"{caption}\n\n" + "\n".join(fallback_lines), markup)


# ------------------------------------------------------------------ настройки ---
@router.callback_query(F.data.startswith("srv:set:"))
async def server_settings(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    server = await _load_managed(session, call, user)
    if server is None:
        return
    text = [
        f"⚙️ <b>Настройки — {esc(server.name)}</b>",
        "",
        f"Адрес: <code>{esc(server.host)}</code>",
        "",
        "🎯 <b>Пороги предупреждений</b>",
        "Нажмите на значение, чтобы изменить его.",
        "",
        "Даже если сервер доступен, его ресурсы могут быть перегружены — "
        "именно для этого нужны пороги CPU, RAM и Disk.",
    ]
    await edit(call, "\n".join(text), kb.server_settings(server))
    await call.answer()


@router.callback_query(F.data.startswith("srv:toggle_notify:"))
async def toggle_notify(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    server = await _load_managed(session, call, user)
    if server is None:
        return
    server.notifications_enabled = not server.notifications_enabled
    await session.commit()
    await call.answer("Уведомления " + ("включены" if server.notifications_enabled else "выключены"))
    await server_settings(call, session, user)


@router.callback_query(F.data.startswith("srv:thr:"))
async def ask_threshold(call: CallbackQuery, session: AsyncSession, user: User, state: FSMContext) -> None:
    parts = call.data.split(":")
    if len(parts) < 4 or parts[3] not in THRESHOLDS or not parts[2].isdigit():
        await call.answer("Некорректный запрос", show_alert=True)
        return
    server_id, key = int(parts[2]), parts[3]
    server = await repo.get_server(session, server_id, user.id)
    if server is None:
        await call.answer("Сервер не найден", show_alert=True)
        return
    if not access.can_manage(server, user):
        await call.answer("🔒 Менять пороги может автор сервера, админ или владелец", show_alert=True)
        return
    low, high, unit = THRESHOLDS[key]
    current = getattr(server, key, None)
    await state.set_state(ServerEdit.threshold)
    await state.update_data(server_id=server_id, key=key)
    await edit(
        call,
        f"🎯 <b>{key.replace('_', ' ').upper()}</b>\n\n"
        f"Текущее значение: {current if current is not None else 'как в общих настройках'} {unit}\n\n"
        f"Отправьте новое значение числом от {low:g} до {high:g}.",
        kb.cancel_input(f"srv:set:{server_id}"),
    )
    await call.answer()


@router.message(ServerEdit.threshold)
async def save_threshold(
    message: Message, state: FSMContext, session: AsyncSession, user: User
) -> None:
    data = await state.get_data()
    server_id, key = data.get("server_id"), data.get("key")
    if key not in THRESHOLDS:
        await state.clear()
        return
    low, high, unit = THRESHOLDS[key]
    raw = (message.text or "").strip().replace(",", ".")
    for token in ("%", "ms", "°c", "°", "c", " "):
        raw = raw.lower().replace(token, "")
    try:
        value = float(raw)
    except ValueError:
        await message.answer("Нужно число, например <code>85</code>. Попробуйте ещё раз.")
        return
    if not low <= value <= high:
        await message.answer(f"Значение должно быть от {low:g} до {high:g} {unit}.")
        return
    server = await repo.get_server(session, server_id, user.id)
    if server is None or not access.can_manage(server, user):
        await state.clear()
        await message.answer("Сервер не найден.", reply_markup=kb.back_main())
        return
    setattr(server, key, value)
    await session.commit()
    await state.clear()
    await message.answer(
        f"✅ Порог обновлён: <b>{key}</b> = {value:g} {unit}",
        reply_markup=kb.back_main([("⚙️ К настройкам сервера", f"srv:set:{server_id}")]),
    )


@router.callback_query(F.data.startswith("srv:rename:"))
async def ask_rename(call: CallbackQuery, session: AsyncSession, user: User, state: FSMContext) -> None:
    server = await _load_managed(session, call, user)
    if server is None:
        return
    await state.set_state(ServerEdit.rename)
    await state.update_data(server_id=server.id)
    await edit(
        call,
        f"✏️ Введите новое название для <b>{esc(server.name)}</b>:",
        kb.cancel_input(f"srv:set:{server.id}"),
    )
    await call.answer()


@router.message(ServerEdit.rename)
async def save_rename(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    data = await state.get_data()
    server = await repo.get_server(session, data.get("server_id"), user.id)
    name = clean_name(message.text or "")
    if server is None or not access.can_manage(server, user):
        await state.clear()
        await message.answer("Сервер не найден или его нельзя менять.", reply_markup=kb.back_main())
        return
    if len(name) < 2:
        await message.answer("Название слишком короткое или сервер не найден.")
        return
    server.name = name
    await session.commit()
    await state.clear()
    await message.answer(
        f"✅ Название обновлено: <b>{esc(name)}</b>",
        reply_markup=kb.back_main([("🖥 К серверу", f"srv:view:{server.id}")]),
    )


# --------------------------------------------------------------------- агент ---
@router.callback_query(F.data.startswith("srv:agent:"))
async def agent_page(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    server = await _load_managed(session, call, user)
    if server is None:
        return
    connected, state_text = texts.agent_state(server)
    if server.agent_enabled:
        text = texts.agent_instructions(server, settings.agent_public_url)
        text += f"\n\nСтатус: {state_text}"
        if server.agent_hostname:
            text += f"\nHostname: <code>{esc(server.agent_hostname)}</code>"
        if server.agent_os:
            text += f"\nOS: {esc(server.agent_os)}"
    else:
        text = (
            f"🔐 <b>ServerEye Agent — {esc(server.name)}</b>\n\n"
            "Мониторинг ресурсов выключен.\n\n"
            "Ping, HTTP и TCP показывают только доступность сервера. "
            "Чтобы видеть CPU, RAM, диск и сетевой трафик, установите агент "
            "на сам сервер — он собирает только метрики и не выполняет команды."
        )
    await edit(call, text, kb.server_agent(server))
    await call.answer()


@router.callback_query(F.data.startswith("srv:agentnew:"))
async def agent_new_token(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    server = await _load_managed(session, call, user)
    if server is None:
        return
    await repo.rotate_agent_token(session, server)
    await session.commit()
    await call.answer("Новый токен выпущен")
    await edit(
        call,
        texts.agent_instructions(server, settings.agent_public_url)
        + "\n\n♻️ Старый токен больше не действует.",
        kb.server_agent(server),
    )


@router.callback_query(F.data.startswith("srv:agentoff:"))
async def agent_off(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    server = await _load_managed(session, call, user)
    if server is None:
        return
    await repo.disable_agent(session, server)
    await session.commit()
    await call.answer("Agent отключён")
    await edit(
        call,
        f"🗑 Agent для <b>{esc(server.name)}</b> отключён, токен отозван.\n\n"
        "Мониторинг доступности продолжает работать.",
        kb.server_agent(server),
    )


# -------------------------------------------------------------------- удаление ---
@router.callback_query(F.data.startswith("srv:del:"))
async def ask_delete(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    server = await _load_managed(session, call, user)
    if server is None:
        return
    await edit(
        call,
        f"🗑 Удалить сервер <b>{esc(server.name)}</b>?\n\n"
        "Вместе с ним будет удалена вся история проверок и метрик. "
        "Действие необратимо.",
        kb.confirm_delete(server.id),
    )
    await call.answer()


@router.callback_query(F.data.startswith("srv:delok:"))
async def do_delete(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    try:
        server_id = int(call.data.split(":")[2])
    except (IndexError, ValueError):
        await call.answer("Некорректный запрос", show_alert=True)
        return
    server = await repo.get_server(session, server_id, user.id)
    allowed = server is not None and access.can_manage(server, user)
    ok = await repo.delete_server(session, server_id, user.id, allowed=allowed)
    if server is not None and not allowed:
        await call.answer("🔒 Удалить сервер может автор, админ или владелец бота", show_alert=True)
        return
    await session.commit()
    await call.answer("Сервер удалён" if ok else "Сервер не найден")
    text, markup = await render_list(session, user.id)
    await edit(call, text, markup)
