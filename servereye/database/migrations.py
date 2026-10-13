"""Лёгкие миграции схемы.

ServerEye живёт на SQLite и обновляется простым выкатыванием кода, поэтому
вместо Alembic здесь набор идемпотентных шагов: они безопасно применяются
к уже работающей базе и ничего не делают, если всё уже приведено в порядок.
"""
from __future__ import annotations

import logging

from sqlalchemy import Boolean, text
from sqlalchemy.ext.asyncio import AsyncConnection

from .models import Base

log = logging.getLogger(__name__)

# Таблицы и колонки, оставшиеся от раздела «Интернет» (убран из бота)
OBSOLETE_TABLES = ("internet_checks",)
OBSOLETE_COLUMNS = {
    "settings": (
        "notify_speed_drop",
        "notify_vpn",
        "internet_monitor",
        "auto_speedtest",
        "store_ip",
        "mask_ip",
        "download_min_mbps",
    ),
}


async def _table_exists(conn: AsyncConnection, table: str) -> bool:
    result = await conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name"),
        {"name": table},
    )
    return result.first() is not None


async def _columns(conn: AsyncConnection, table: str) -> set[str]:
    result = await conn.execute(text(f"PRAGMA table_info({table})"))
    return {row[1] for row in result.fetchall()}


def _column_ddl(column, dialect) -> str:
    """DDL для ALTER TABLE ADD COLUMN с безопасным значением по умолчанию.

    SQLite не добавит NOT NULL-колонку без DEFAULT, поэтому для колонок
    со скалярным default подставляем его прямо в DDL.
    """
    ddl = f"{column.name} {column.type.compile(dialect=dialect)}"
    default = getattr(column.default, "arg", None)
    if default is None or callable(default):
        return ddl
    if isinstance(column.type, Boolean):
        value = "1" if default else "0"
    elif isinstance(default, (int, float)):
        value = str(default)
    else:
        value = "'" + str(default).replace("'", "''") + "'"
    return f"{ddl} NOT NULL DEFAULT {value}"


async def _add_missing_columns(conn: AsyncConnection) -> None:
    for table in Base.metadata.sorted_tables:
        if not await _table_exists(conn, table.name):
            continue  # новые таблицы создаёт create_all
        existing = await _columns(conn, table.name)
        for column in table.columns:
            if column.name in existing:
                continue
            ddl = _column_ddl(column, conn.dialect)
            await conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {ddl}"))
            log.info("Миграция: добавлена колонка %s.%s", table.name, column.name)


async def run_migrations(conn: AsyncConnection) -> None:
    """Приводит существующую базу к текущей схеме. Только для SQLite."""
    dialect = conn.dialect.name
    if dialect != "sqlite":
        return

    await _add_missing_columns(conn)

    for table in OBSOLETE_TABLES:
        if await _table_exists(conn, table):
            await conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
            log.info("Миграция: удалена устаревшая таблица %s", table)

    for table, columns in OBSOLETE_COLUMNS.items():
        if not await _table_exists(conn, table):
            continue
        existing = await _columns(conn, table)
        for column in columns:
            if column not in existing:
                continue
            try:
                await conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
                log.info("Миграция: удалена колонка %s.%s", table, column)
            except Exception as exc:  # noqa: BLE001 — старые SQLite не умеют DROP COLUMN
                log.warning(
                    "Миграция: не удалось удалить %s.%s (%s). "
                    "Колонка останется, на работу это не влияет.",
                    table,
                    column,
                    type(exc).__name__,
                )
