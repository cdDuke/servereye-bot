"""Умный анализ: сравнение текущих показателей с историей.

Здесь рождаются фразы вида «Ping вырос на 359%» и «сервер стал стабильнее».
Модуль возвращает структуры Insight — как их показать, решают хендлеры и
рассыльщик уведомлений.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database.models import (
    STATUS_OFFLINE,
    STATUS_WARNING,
    Server,
    ServerCheck,
    ServerMetric,
)
from ..utils import fmt_mb, fmt_ms, pct_change, utcnow

log = logging.getLogger(__name__)

SEV_INFO = "info"
SEV_GOOD = "good"
SEV_WARNING = "warning"
SEV_CRITICAL = "critical"

# насколько изменение считается значимым
PING_DEGRADE_PCT = 60.0
PING_DEGRADE_MIN_MS = 25.0
PING_IMPROVE_PCT = -30.0
RESOURCE_GROWTH_PCT = 35.0


@dataclass
class Insight:
    kind: str
    severity: str
    title: str
    lines: list[str] = field(default_factory=list)
    server_id: int | None = None
    value: float | None = None
    previous: float | None = None
    change_pct: float | None = None

    def render(self) -> str:
        head = self.title
        body = "\n".join(self.lines)
        return f"{head}\n\n{body}" if body else head


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.0f}%"


# ------------------------------------------------------------------ серверы ---
async def _avg_ping(
    session: AsyncSession, server_id: int, hours_from: float, hours_to: float = 0.0
) -> tuple[float | None, int]:
    now = utcnow()
    start = now - timedelta(hours=hours_from)
    end = now - timedelta(hours=hours_to)
    row = (
        await session.execute(
            select(func.avg(ServerCheck.ping), func.count(ServerCheck.id)).where(
                ServerCheck.server_id == server_id,
                ServerCheck.timestamp >= start,
                ServerCheck.timestamp < end,
                ServerCheck.ping.is_not(None),
            )
        )
    ).one()
    avg, count = row
    return (float(avg) if avg is not None else None), int(count or 0)


async def analyze_server_ping(
    session: AsyncSession, server: Server, current_ping: float | None = None
) -> Insight | None:
    """Сравнивает свежий ping с базовой линией за последние сутки."""
    baseline, baseline_count = await _avg_ping(session, server.id, hours_from=24, hours_to=1)
    recent, recent_count = await _avg_ping(session, server.id, hours_from=1)
    if current_ping is not None:
        recent = current_ping if recent is None else (recent + current_ping) / 2
    if baseline is None or recent is None or baseline_count < 3 or (recent_count < 1 and current_ping is None):
        return None

    change = pct_change(baseline, recent)
    if change is None:
        return None

    if change >= PING_DEGRADE_PCT and (recent - baseline) >= PING_DEGRADE_MIN_MS:
        return Insight(
            kind="ping_degraded",
            severity=SEV_WARNING,
            title="⚠️ Обнаружено ухудшение соединения",
            lines=[
                f"<b>{server.name}</b>",
                "",
                "Ping:",
                f"{fmt_ms(baseline)} → <b>{fmt_ms(recent)}</b>",
                f"Рост: {_fmt_pct(change)}",
                "",
                "Сервер работает, но соединение стало значительно медленнее.",
            ],
            server_id=server.id,
            value=recent,
            previous=baseline,
            change_pct=change,
        )
    if change <= PING_IMPROVE_PCT and (baseline - recent) >= 10:
        return Insight(
            kind="ping_improved",
            severity=SEV_GOOD,
            title="📈 Сервер работает стабильнее",
            lines=[
                f"<b>{server.name}</b>",
                "",
                "Средний Ping:",
                f"{fmt_ms(baseline)} → <b>{fmt_ms(recent)}</b>",
                f"Улучшение: {abs(change):.0f}%",
            ],
            server_id=server.id,
            value=recent,
            previous=baseline,
            change_pct=change,
        )
    return None


async def analyze_instability(
    session: AsyncSession, server: Server, hours: int = 3, min_errors: int = 4
) -> Insight | None:
    since = utcnow() - timedelta(hours=hours)
    errors = int(
        await session.scalar(
            select(func.count(ServerCheck.id)).where(
                ServerCheck.server_id == server.id,
                ServerCheck.timestamp >= since,
                ServerCheck.status.in_((STATUS_OFFLINE, STATUS_WARNING)),
            )
        )
        or 0
    )
    total = int(
        await session.scalar(
            select(func.count(ServerCheck.id)).where(
                ServerCheck.server_id == server.id, ServerCheck.timestamp >= since
            )
        )
        or 0
    )
    if errors < min_errors or total < min_errors:
        return None
    return Insight(
        kind="unstable",
        severity=SEV_WARNING,
        title="⚠️ Сервер нестабилен",
        lines=[
            f"<b>{server.name}</b>",
            "",
            f"За последние {hours} ч зафиксировано {errors} проблемных проверок "
            f"из {total}.",
            "Стоит проверить сеть или нагрузку на сервере.",
        ],
        server_id=server.id,
        value=float(errors),
    )


async def analyze_resource_trend(
    session: AsyncSession, server: Server, hours: int = 3
) -> Insight | None:
    """Сравнивает нагрузку в начале и в конце окна — ищет постепенный рост."""
    now = utcnow()
    since = now - timedelta(hours=hours)
    metrics = list(
        (
            await session.execute(
                select(ServerMetric)
                .where(ServerMetric.server_id == server.id, ServerMetric.timestamp >= since)
                .order_by(ServerMetric.timestamp)
            )
        )
        .scalars()
        .all()
    )
    if len(metrics) < 6:
        return None
    head = metrics[: max(2, len(metrics) // 4)]
    tail = metrics[-max(2, len(metrics) // 4) :]

    def avg(items, attr):
        values = [getattr(m, attr) for m in items if getattr(m, attr) is not None]
        return sum(values) / len(values) if values else None

    ram_old, ram_new = avg(head, "ram_used_mb"), avg(tail, "ram_used_mb")
    cpu_old, cpu_new = avg(head, "cpu_percent"), avg(tail, "cpu_percent")
    ram_change = pct_change(ram_old, ram_new)
    cpu_change = pct_change(cpu_old, cpu_new)

    growing = (ram_change is not None and ram_change >= RESOURCE_GROWTH_PCT) or (
        cpu_change is not None and cpu_change >= RESOURCE_GROWTH_PCT and (cpu_new or 0) >= 50
    )
    if not growing:
        return None

    lines = [f"<b>{server.name}</b>", "", f"За последние {hours} ч:"]
    if ram_old and ram_new:
        lines += ["", "RAM:", f"{fmt_mb(ram_old)} → {fmt_mb(ram_new)} MB", f"Рост: {_fmt_pct(ram_change)}"]
    if cpu_old is not None and cpu_new is not None:
        lines += ["", "CPU:", f"{cpu_old:.0f}% → {cpu_new:.0f}%"]
    lines += ["", "⚠️ Нагрузка постепенно увеличивается."]
    return Insight(
        kind="resource_trend",
        severity=SEV_WARNING,
        title="🧠 Анализ сервера",
        lines=lines,
        server_id=server.id,
        change_pct=ram_change or cpu_change,
    )
