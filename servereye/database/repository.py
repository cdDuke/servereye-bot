"""Репозиторий: весь доступ к данным собран здесь.

Правило изоляции: любая выборка серверов/проверок обязательно ограничена user_id.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Sequence

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings, new_agent_token
from ..utils import utcnow
from .models import (
    ACCESS_ACTIVE,
    STATUS_OFFLINE,
    STATUS_ONLINE,
    STATUS_UNKNOWN,
    STATUS_WARNING,
    AgentEvent,
    Notification,
    Server,
    ServerCheck,
    ServerMetric,
    User,
    UserSettings,
)

MAX_SERVERS_PER_USER = 25


# ------------------------------------------------------------- пользователи ---
async def get_or_create_user(
    session: AsyncSession,
    user_id: int,
    username: str | None = None,
    first_name: str | None = None,
    language_code: str | None = None,
) -> User:
    user = await session.get(User, user_id)
    if user is None:
        user = User(
            id=user_id,
            username=username,
            first_name=first_name,
            language_code=language_code,
        )
        session.add(user)
        await session.flush()
        session.add(UserSettings(user_id=user_id))
        await session.flush()
    else:
        user.username = username or user.username
        user.first_name = first_name or user.first_name
        user.last_seen_at = utcnow()
        user.is_active = True
        await session.flush()
    return user


async def get_settings_for(session: AsyncSession, user_id: int) -> UserSettings:
    st = await session.get(UserSettings, user_id)
    if st is None:
        st = UserSettings(user_id=user_id)
        session.add(st)
        await session.flush()
    return st


async def update_settings(session: AsyncSession, user_id: int, **values: Any) -> UserSettings:
    st = await get_settings_for(session, user_id)
    for key, value in values.items():
        if hasattr(st, key):
            setattr(st, key, value)
    await session.flush()
    return st


async def toggle_setting(session: AsyncSession, user_id: int, key: str) -> bool:
    st = await get_settings_for(session, user_id)
    current = bool(getattr(st, key, False))
    setattr(st, key, not current)
    await session.flush()
    return not current


async def all_users(session: AsyncSession, only_active: bool = True) -> Sequence[User]:
    stmt = select(User).where(_user_has_access())
    if only_active:
        stmt = stmt.where(User.is_active.is_(True))
    return (await session.execute(stmt)).scalars().all()


async def deactivate_user(session: AsyncSession, user_id: int) -> None:
    await session.execute(update(User).where(User.id == user_id).values(is_active=False))


# ------------------------------------------------------------------ серверы ---
def _user_has_access():
    """SQL-условие «у пользователя есть доступ»: активен или владелец из .env."""
    owners = get_settings().admin_ids
    if owners:
        return or_(User.access_status == ACCESS_ACTIVE, User.id.in_(owners))
    return User.access_status == ACCESS_ACTIVE


async def _viewer_active(session: AsyncSession, user_id: int) -> bool:
    if user_id in get_settings().admin_ids:
        return True  # владелец из .env видит список всегда
    viewer = await session.get(User, user_id)
    return viewer is not None and viewer.access_status == ACCESS_ACTIVE


async def list_servers(
    session: AsyncSession, user_id: int, only_active: bool = True
) -> list[Server]:
    """Общий список серверов, который видит пользователь с доступом.

    Серверы общие для всех участников бота: каждый видит и свои, и те,
    что добавили владелец и другие приглашённые. Серверы заблокированных
    участников из общего списка пропадают.
    """
    if not await _viewer_active(session, user_id):
        return []
    stmt = (
        select(Server)
        .join(User, User.id == Server.user_id)
        .where(_user_has_access())
    )
    if only_active:
        stmt = stmt.where(Server.is_active.is_(True))
    stmt = stmt.order_by(Server.created_at)
    return list((await session.execute(stmt)).scalars().all())


async def list_own_servers(session: AsyncSession, user_id: int) -> list[Server]:
    """Серверы, которые добавил сам пользователь."""
    stmt = (
        select(Server)
        .where(Server.user_id == user_id, Server.is_active.is_(True))
        .order_by(Server.created_at)
    )
    return list((await session.execute(stmt)).scalars().all())


async def count_servers(session: AsyncSession, user_id: int) -> int:
    """Сколько серверов добавил сам пользователь (для лимита)."""
    return int(
        await session.scalar(
            select(func.count(Server.id)).where(
                Server.user_id == user_id, Server.is_active.is_(True)
            )
        )
        or 0
    )


async def get_server(session: AsyncSession, server_id: int, user_id: int | None = None) -> Server | None:
    """Сервер, если он виден этому пользователю (общий список)."""
    if server_id is None:
        return None
    server = await session.get(Server, server_id)
    if server is None or not server.is_active:
        return None
    if user_id is None:
        return server
    creator = await session.get(User, server.user_id)
    if creator is None or not (
        creator.access_status == ACCESS_ACTIVE or creator.id in get_settings().admin_ids
    ):
        return None
    if not await _viewer_active(session, user_id):
        return None
    return server


async def find_duplicate(session: AsyncSession, user_id: int, host: str) -> Server | None:
    for server in await list_servers(session, user_id):
        if server.host.lower() == host.lower():
            return server
    return None


async def get_server_by_agent_token(session: AsyncSession, token: str) -> Server | None:
    # агент заблокированного пользователя тоже перестаёт приниматься
    stmt = (
        select(Server)
        .join(User, User.id == Server.user_id)
        .where(
            Server.agent_token == token,
            Server.is_active.is_(True),
            _user_has_access(),
        )
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_server(
    session: AsyncSession,
    user_id: int,
    name: str,
    host: str,
    check_type: str,
    ports: list[int] | None = None,
    http_url: str | None = None,
    agent_enabled: bool = False,
) -> Server:
    server = Server(
        user_id=user_id,
        name=name,
        host=host,
        check_type=check_type,
        ports=",".join(str(p) for p in ports) if ports else None,
        port=ports[0] if ports else None,
        http_url=http_url,
        agent_enabled=agent_enabled,
        agent_token=new_agent_token() if agent_enabled else None,
        status=STATUS_UNKNOWN,
    )
    session.add(server)
    await session.flush()
    return server


async def delete_server(session: AsyncSession, server_id: int, user_id: int, allowed: bool) -> bool:
    """Удаление. Право (`allowed`) проверяет вызывающий: автор сервера или владелец."""
    server = await get_server(session, server_id, user_id)
    if server is None or not allowed:
        return False
    await session.delete(server)
    return True


async def rotate_agent_token(session: AsyncSession, server: Server) -> str:
    server.agent_token = new_agent_token()
    server.agent_enabled = True
    server.agent_last_seen = None
    await session.flush()
    return server.agent_token


async def disable_agent(session: AsyncSession, server: Server) -> None:
    server.agent_enabled = False
    server.agent_token = None
    server.agent_last_seen = None
    await session.flush()


async def all_monitored_servers(session: AsyncSession) -> list[Server]:
    stmt = (
        select(Server)
        .join(User, User.id == Server.user_id)
        .where(
            Server.is_active.is_(True),
            User.is_active.is_(True),
            _user_has_access(),
        )
    )
    return list((await session.execute(stmt)).scalars().all())


# ------------------------------------------------------------------ проверки ---
async def add_check(session: AsyncSession, server_id: int, **values: Any) -> ServerCheck:
    check = ServerCheck(server_id=server_id, **values)
    session.add(check)
    await session.flush()
    return check


async def last_checks(
    session: AsyncSession, server_id: int, limit: int = 20
) -> list[ServerCheck]:
    stmt = (
        select(ServerCheck)
        .where(ServerCheck.server_id == server_id)
        .order_by(ServerCheck.timestamp.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def checks_since(
    session: AsyncSession, server_id: int, since: datetime, limit: int = 5000
) -> list[ServerCheck]:
    stmt = (
        select(ServerCheck)
        .where(ServerCheck.server_id == server_id, ServerCheck.timestamp >= since)
        .order_by(ServerCheck.timestamp)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


@dataclass
class ServerStats:
    total: int = 0
    ok: int = 0
    warning: int = 0
    failed: int = 0
    uptime: float | None = None
    ping_avg: float | None = None
    ping_min: float | None = None
    ping_max: float | None = None
    loss_avg: float | None = None
    success_rate: float | None = None
    errors: int = 0
    outages: int = 0


async def server_stats(session: AsyncSession, server_id: int, hours: int) -> ServerStats:
    since = utcnow() - timedelta(hours=hours)
    row = (
        await session.execute(
            select(
                func.count(ServerCheck.id),
                func.avg(ServerCheck.ping),
                func.min(ServerCheck.ping),
                func.max(ServerCheck.ping),
                func.avg(ServerCheck.packet_loss),
            ).where(ServerCheck.server_id == server_id, ServerCheck.timestamp >= since)
        )
    ).one()
    total, ping_avg, ping_min, ping_max, loss_avg = row
    stats = ServerStats(
        total=int(total or 0),
        ping_avg=float(ping_avg) if ping_avg is not None else None,
        ping_min=float(ping_min) if ping_min is not None else None,
        ping_max=float(ping_max) if ping_max is not None else None,
        loss_avg=float(loss_avg) if loss_avg is not None else None,
    )
    if not stats.total:
        return stats

    by_status = (
        await session.execute(
            select(ServerCheck.status, func.count(ServerCheck.id))
            .where(ServerCheck.server_id == server_id, ServerCheck.timestamp >= since)
            .group_by(ServerCheck.status)
        )
    ).all()
    counters = {status: int(count) for status, count in by_status}
    stats.ok = counters.get(STATUS_ONLINE, 0)
    stats.warning = counters.get(STATUS_WARNING, 0)
    stats.failed = counters.get(STATUS_OFFLINE, 0)
    stats.errors = stats.failed
    stats.uptime = round((stats.ok + stats.warning) / stats.total * 100, 2)
    stats.success_rate = round(stats.ok / stats.total * 100, 2)

    # эпизоды недоступности = переходы online → offline
    statuses = list(
        (
            await session.execute(
                select(ServerCheck.status)
                .where(ServerCheck.server_id == server_id, ServerCheck.timestamp >= since)
                .order_by(ServerCheck.timestamp)
            )
        )
        .scalars()
        .all()
    )
    prev = None
    for status in statuses:
        if status == STATUS_OFFLINE and prev != STATUS_OFFLINE:
            stats.outages += 1
        prev = status
    return stats


async def ping_series(
    session: AsyncSession, server_id: int, hours: int, buckets: int = 48
) -> list[tuple[datetime, float | None, str]]:
    since = utcnow() - timedelta(hours=hours)
    rows = list(
        (
            await session.execute(
                select(ServerCheck.timestamp, ServerCheck.ping, ServerCheck.status)
                .where(ServerCheck.server_id == server_id, ServerCheck.timestamp >= since)
                .order_by(ServerCheck.timestamp)
            )
        ).all()
    )
    return [(ts, ping, status) for ts, ping, status in rows]


# -------------------------------------------------------- метрики от агента ---
async def add_metric(session: AsyncSession, server_id: int, **values: Any) -> ServerMetric:
    metric = ServerMetric(server_id=server_id, **values)
    session.add(metric)
    await session.flush()
    return metric


async def latest_metric(session: AsyncSession, server_id: int) -> ServerMetric | None:
    stmt = (
        select(ServerMetric)
        .where(ServerMetric.server_id == server_id)
        .order_by(ServerMetric.timestamp.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def metrics_since(
    session: AsyncSession, server_id: int, since: datetime
) -> list[ServerMetric]:
    stmt = (
        select(ServerMetric)
        .where(ServerMetric.server_id == server_id, ServerMetric.timestamp >= since)
        .order_by(ServerMetric.timestamp)
    )
    return list((await session.execute(stmt)).scalars().all())


async def metric_avg(
    session: AsyncSession, server_id: int, since: datetime, until: datetime | None = None
) -> dict[str, float | None]:
    stmt = select(
        func.avg(ServerMetric.cpu_percent),
        func.avg(ServerMetric.ram_percent),
        func.avg(ServerMetric.ram_used_mb),
        func.avg(ServerMetric.disk_percent),
    ).where(ServerMetric.server_id == server_id, ServerMetric.timestamp >= since)
    if until is not None:
        stmt = stmt.where(ServerMetric.timestamp < until)
    cpu, ram, ram_mb, disk = (await session.execute(stmt)).one()
    return {
        "cpu": float(cpu) if cpu is not None else None,
        "ram": float(ram) if ram is not None else None,
        "ram_mb": float(ram_mb) if ram_mb is not None else None,
        "disk": float(disk) if disk is not None else None,
    }


async def log_agent_event(
    session: AsyncSession, server_id: int, event: str, detail: str | None = None
) -> None:
    session.add(AgentEvent(server_id=server_id, event=event, detail=detail))
    await session.flush()


# ------------------------------------------------------------ уведомления ---
async def add_notification(
    session: AsyncSession,
    user_id: int,
    type_: str,
    message: str,
    server_id: int | None = None,
    severity: str = "info",
    delivered: bool = True,
) -> Notification:
    notification = Notification(
        user_id=user_id,
        server_id=server_id,
        type=type_,
        message=message,
        severity=severity,
        delivered=delivered,
    )
    session.add(notification)
    await session.flush()
    return notification


async def undelivered_notifications(
    session: AsyncSession, types: set[str], max_age_hours: int, max_attempts: int
) -> list[Notification]:
    since = utcnow() - timedelta(hours=max_age_hours)
    stmt = (
        select(Notification)
        .where(
            Notification.delivered.is_(False),
            Notification.type.in_(types),
            Notification.created_at >= since,
            Notification.attempts < max_attempts,
        )
        .order_by(Notification.created_at)
        .limit(50)
    )
    return list((await session.execute(stmt)).scalars().all())


async def recent_notifications(
    session: AsyncSession, user_id: int, limit: int = 10
) -> list[Notification]:
    stmt = (
        select(Notification)
        .where(Notification.user_id == user_id)
        .order_by(Notification.created_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def count_notifications(session: AsyncSession, user_id: int, hours: int = 24) -> int:
    since = utcnow() - timedelta(hours=hours)
    return int(
        await session.scalar(
            select(func.count(Notification.id)).where(
                Notification.user_id == user_id, Notification.created_at >= since
            )
        )
        or 0
    )


# ---------------------------------------------------------------- обслуживание ---
async def cleanup_old_data(session: AsyncSession, days: int) -> dict[str, int]:
    cutoff = utcnow() - timedelta(days=days)
    removed: dict[str, int] = {}
    for model, name in (
        (ServerCheck, "server_checks"),
        (ServerMetric, "server_metrics"),
        (Notification, "notifications"),
        (AgentEvent, "agent_events"),
    ):
        column = getattr(model, "timestamp", None) or getattr(model, "created_at")
        result = await session.execute(delete(model).where(column < cutoff))
        removed[name] = result.rowcount or 0
    return removed
