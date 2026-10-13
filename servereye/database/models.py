"""SQLAlchemy-модели ServerEye."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from ..utils import utcnow


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------- статусы ---
STATUS_ONLINE = "online"
STATUS_WARNING = "warning"
STATUS_OFFLINE = "offline"
STATUS_UNKNOWN = "unknown"

CHECK_PING = "ping"
CHECK_HTTP = "http"
CHECK_TCP = "tcp"
CHECK_PING_HTTP = "ping_http"

# Доступ к боту
ACCESS_ACTIVE = "active"      # пользуется ботом
ACCESS_PENDING = "pending"    # пришёл по приглашению, ждёт подтверждения
ACCESS_DENIED = "denied"      # владелец отказал (новое приглашение — можно)
ACCESS_BLOCKED = "blocked"    # заблокирован (никакие приглашения не работают)

ROLE_OWNER = "owner"   # из ADMIN_IDS, права нельзя отнять из бота
ROLE_ADMIN = "admin"   # назначается владельцем
ROLE_USER = "user"

INVITE_APPROVE = "approve"    # вход только после подтверждения владельцем
INVITE_INSTANT = "instant"    # вход сразу, владельцу — уведомление

INVITE_ACTIVE = "active"
INVITE_USED = "used"
INVITE_REVOKED = "revoked"

CHECK_TYPE_TITLES = {
    CHECK_PING: "Ping",
    CHECK_HTTP: "HTTP / HTTPS",
    CHECK_TCP: "TCP-порт",
    CHECK_PING_HTTP: "Ping + HTTP",
}


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # telegram user_id
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    language_code: Mapped[str | None] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # False — пользователь заблокировал бота (некуда доставлять сообщения)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Доступ. По умолчанию «ждёт»: существующие записи без приглашения
    # после обновления не получают доступ автоматически.
    access_status: Mapped[str] = mapped_column(String(16), default=ACCESS_PENDING, index=True)
    role: Mapped[str] = mapped_column(String(16), default=ROLE_USER)
    invite_id: Mapped[int | None] = mapped_column(Integer)
    access_changed_at: Mapped[datetime | None] = mapped_column(DateTime)

    @property
    def display_name(self) -> str:
        name = " ".join(x for x in (self.first_name, self.last_name) if x)
        return name or (f"@{self.username}" if self.username else f"id{self.id}")

    settings: Mapped["UserSettings"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan", lazy="selectin"
    )
    servers: Mapped[list["Server"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserSettings(Base):
    __tablename__ = "settings"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )

    # что присылать
    notify_server_down: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_server_up: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_high_ping: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_packet_loss: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_cpu: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_ram: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_disk: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_agent_offline: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_trend: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_temp: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_power: Mapped[bool] = mapped_column(Boolean, default=True)
    hourly_report: Mapped[bool] = mapped_column(Boolean, default=True)
    daily_report: Mapped[bool] = mapped_column(Boolean, default=True)

    # пороги пользователя (глобальные значения по умолчанию)
    ping_warning_ms: Mapped[int] = mapped_column(Integer, default=100)
    ping_critical_ms: Mapped[int] = mapped_column(Integer, default=300)
    packet_loss_pct: Mapped[float] = mapped_column(Float, default=5.0)

    quiet_hours_from: Mapped[int | None] = mapped_column(Integer)  # локальный час
    quiet_hours_to: Mapped[int | None] = mapped_column(Integer)
    tz_offset: Mapped[int] = mapped_column(Integer, default=3)

    user: Mapped[User] = relationship(back_populates="settings")


class Server(Base):
    __tablename__ = "servers"
    __table_args__ = (Index("ix_servers_user", "user_id", "is_active"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(64))
    host: Mapped[str] = mapped_column(String(253))
    port: Mapped[int | None] = mapped_column(Integer)
    ports: Mapped[str | None] = mapped_column(String(128))  # «22,80,443»
    check_type: Mapped[str] = mapped_column(String(16), default=CHECK_PING)
    http_url: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # текущее состояние (машина состояний для уведомлений)
    status: Mapped[str] = mapped_column(String(16), default=STATUS_UNKNOWN)
    status_since: Mapped[datetime | None] = mapped_column(DateTime)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_ping: Mapped[float | None] = mapped_column(Float)
    last_ping_target: Mapped[str | None] = mapped_column(String(64))
    down_notified_at: Mapped[datetime | None] = mapped_column(DateTime)
    high_ping_notified_at: Mapped[datetime | None] = mapped_column(DateTime)
    loss_notified_at: Mapped[datetime | None] = mapped_column(DateTime)
    cpu_notified_at: Mapped[datetime | None] = mapped_column(DateTime)
    ram_notified_at: Mapped[datetime | None] = mapped_column(DateTime)
    disk_notified_at: Mapped[datetime | None] = mapped_column(DateTime)
    trend_notified_at: Mapped[datetime | None] = mapped_column(DateTime)
    agent_offline_notified_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Машина, на которой работает сам бот: только для неё доступно выключение
    is_local_host: Mapped[bool] = mapped_column(Boolean, default=False)

    # Тревога по температуре CPU (повторяется, пока не остынет)
    temp_alarm_active: Mapped[bool] = mapped_column(Boolean, default=False)
    temp_alarm_since: Mapped[datetime | None] = mapped_column(DateTime)
    temp_alarm_last_at: Mapped[datetime | None] = mapped_column(DateTime)
    temp_alarm_peak: Mapped[float | None] = mapped_column(Float)

    # Питание: работа от батареи = пропал свет
    on_battery: Mapped[bool] = mapped_column(Boolean, default=False)
    power_lost_at: Mapped[datetime | None] = mapped_column(DateTime)
    battery_alert_level: Mapped[int | None] = mapped_column(Integer)

    # ServerEye Agent
    agent_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    agent_token: Mapped[str | None] = mapped_column(String(32), unique=True, index=True)
    agent_last_seen: Mapped[datetime | None] = mapped_column(DateTime)
    agent_os: Mapped[str | None] = mapped_column(String(128))
    agent_hostname: Mapped[str | None] = mapped_column(String(128))
    agent_version: Mapped[str | None] = mapped_column(String(16))

    # индивидуальные пороги (NULL → берём из настроек пользователя/умолчаний)
    cpu_warning: Mapped[float] = mapped_column(Float, default=80.0)
    cpu_critical: Mapped[float] = mapped_column(Float, default=95.0)
    ram_warning: Mapped[float] = mapped_column(Float, default=80.0)
    ram_critical: Mapped[float] = mapped_column(Float, default=90.0)
    disk_warning: Mapped[float] = mapped_column(Float, default=80.0)
    disk_critical: Mapped[float] = mapped_column(Float, default=90.0)
    ping_warning: Mapped[float | None] = mapped_column(Float)
    ping_critical: Mapped[float | None] = mapped_column(Float)
    cpu_temp_alarm: Mapped[float] = mapped_column(Float, default=90.0)
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    user: Mapped[User] = relationship(back_populates="servers")
    checks: Mapped[list["ServerCheck"]] = relationship(
        back_populates="server", cascade="all, delete-orphan"
    )
    metrics: Mapped[list["ServerMetric"]] = relationship(
        back_populates="server", cascade="all, delete-orphan"
    )

    @property
    def port_list(self) -> list[int]:
        if not self.ports:
            return [self.port] if self.port else []
        return [int(p) for p in self.ports.split(",") if p.strip().isdigit()]


class ServerCheck(Base):
    __tablename__ = "server_checks"
    __table_args__ = (Index("ix_checks_server_ts", "server_id", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    server_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("servers.id", ondelete="CASCADE"), index=True
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    status: Mapped[str] = mapped_column(String(16))
    ping: Mapped[float | None] = mapped_column(Float)
    ping_target: Mapped[str | None] = mapped_column(String(64))  # если пинговали не сам host
    response_time: Mapped[float | None] = mapped_column(Float)  # ms, HTTP/TCP
    packet_loss: Mapped[float | None] = mapped_column(Float)
    http_status: Mapped[int | None] = mapped_column(Integer)
    tcp_ok: Mapped[bool | None] = mapped_column(Boolean)
    tcp_detail: Mapped[str | None] = mapped_column(String(255))
    connection_lost: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(String(255))

    server: Mapped[Server] = relationship(back_populates="checks")


class ServerMetric(Base):
    """Метрики нагрузки от ServerEye Agent."""

    __tablename__ = "server_metrics"
    __table_args__ = (Index("ix_metrics_server_ts", "server_id", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    server_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("servers.id", ondelete="CASCADE"), index=True
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    cpu_percent: Mapped[float | None] = mapped_column(Float)
    cpu_cores: Mapped[int | None] = mapped_column(Integer)
    load_1: Mapped[float | None] = mapped_column(Float)
    load_5: Mapped[float | None] = mapped_column(Float)
    load_15: Mapped[float | None] = mapped_column(Float)

    ram_total_mb: Mapped[float | None] = mapped_column(Float)
    ram_used_mb: Mapped[float | None] = mapped_column(Float)
    ram_free_mb: Mapped[float | None] = mapped_column(Float)
    ram_available_mb: Mapped[float | None] = mapped_column(Float)
    ram_percent: Mapped[float | None] = mapped_column(Float)
    swap_percent: Mapped[float | None] = mapped_column(Float)

    disk_total_bytes: Mapped[float | None] = mapped_column(Float)
    disk_used_bytes: Mapped[float | None] = mapped_column(Float)
    disk_free_bytes: Mapped[float | None] = mapped_column(Float)
    disk_percent: Mapped[float | None] = mapped_column(Float)

    net_down_mbps: Mapped[float | None] = mapped_column(Float)
    net_up_mbps: Mapped[float | None] = mapped_column(Float)
    net_total_recv_bytes: Mapped[float | None] = mapped_column(Float)
    net_total_sent_bytes: Mapped[float | None] = mapped_column(Float)

    uptime_seconds: Mapped[float | None] = mapped_column(Float)
    process_count: Mapped[int | None] = mapped_column(Integer)
    temperature_c: Mapped[float | None] = mapped_column(Float)  # CPU
    gpu_temp_c: Mapped[float | None] = mapped_column(Float)
    gpu_name: Mapped[str | None] = mapped_column(String(64))

    battery_percent: Mapped[float | None] = mapped_column(Float)
    power_plugged: Mapped[bool | None] = mapped_column(Boolean)
    battery_secs_left: Mapped[float | None] = mapped_column(Float)

    server: Mapped[Server] = relationship(back_populates="metrics")


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notif_user_ts", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    server_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("servers.id", ondelete="CASCADE")
    )
    type: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=1)


class Invite(Base):
    """Одноразовое приглашение. Сама ссылка не хранится — только её хэш."""

    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    mode: Mapped[str] = mapped_column(String(16), default=INVITE_APPROVE)
    note: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default=INVITE_ACTIVE, index=True)
    used_by: Mapped[int | None] = mapped_column(BigInteger)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)


class AccessEvent(Base):
    """Журнал доступа. Без внешнего ключа: сюда пишутся и посторонние."""

    __tablename__ = "access_events"
    __table_args__ = (Index("ix_access_user_ts", "tg_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    tg_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str | None] = mapped_column(String(128))
    event: Mapped[str] = mapped_column(String(24))
    invite_id: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[str | None] = mapped_column(String(255))


class AgentEvent(Base):
    """Служебный журнал агента — помогает отличать «сервер лёг» от «агент лёг»."""

    __tablename__ = "agent_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    server_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("servers.id", ondelete="CASCADE"), index=True
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    event: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str | None] = mapped_column(String(255))


__all__ = [
    "Base",
    "User",
    "UserSettings",
    "Server",
    "ServerCheck",
    "ServerMetric",
    "Notification",
    "AgentEvent",
    "Invite",
    "AccessEvent",
    "STATUS_ONLINE",
    "STATUS_WARNING",
    "STATUS_OFFLINE",
    "STATUS_UNKNOWN",
    "CHECK_PING",
    "CHECK_HTTP",
    "CHECK_TCP",
    "CHECK_PING_HTTP",
    "CHECK_TYPE_TITLES",
]
