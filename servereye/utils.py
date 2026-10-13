"""Небольшие общие утилиты: время, форматирование, валидация."""
from __future__ import annotations

import ipaddress
import re
from datetime import datetime, timedelta, timezone

HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-_]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-_]{1,63}(?<!-))*\.?$"
)
PRIVATE_OK = True  # локальные адреса разрешены — люди мониторят домашние сервера


def utcnow() -> datetime:
    """Наивный UTC — единый формат хранения времени в БД."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_local(dt: datetime | None, tz_offset: int = 3) -> datetime | None:
    if dt is None:
        return None
    return dt + timedelta(hours=tz_offset)


def fmt_time(dt: datetime | None, tz_offset: int = 3) -> str:
    local = to_local(dt, tz_offset)
    return local.strftime("%H:%M:%S") if local else "—"


def fmt_dt(dt: datetime | None, tz_offset: int = 3) -> str:
    local = to_local(dt, tz_offset)
    return local.strftime("%d.%m %H:%M") if local else "—"


def fmt_ago(dt: datetime | None) -> str:
    if dt is None:
        return "никогда"
    delta = (utcnow() - dt).total_seconds()
    if delta < 0:
        delta = 0
    if delta < 60:
        return f"{int(delta)} сек назад"
    if delta < 3600:
        return f"{int(delta // 60)} мин назад"
    if delta < 86400:
        return f"{int(delta // 3600)} ч назад"
    return f"{int(delta // 86400)} дн назад"


def fmt_duration_ru(seconds: float | int | None) -> str:
    """Человеческая длительность: «8 минут 13 секунд»."""
    if seconds is None:
        return "—"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    out = []
    if days:
        out.append(f"{days} дн")
    if hours:
        out.append(f"{hours} ч")
    if minutes:
        out.append(f"{minutes} мин")
    if secs and not days:
        out.append(f"{secs} сек")
    return " ".join(out) or "0 сек"


def fmt_mb(mb: float | None) -> str:
    if mb is None:
        return "—"
    return f"{mb:,.0f}".replace(",", " ")


def fmt_bytes_gb(value_bytes: float | None) -> str:
    """Байты → человекочитаемо, но без потери точности внутри."""
    if value_bytes is None:
        return "—"
    tb = value_bytes / 1024**4
    gb = value_bytes / 1024**3
    mb = value_bytes / 1024**2
    if tb >= 1:
        return f"{tb:.2f} TB"
    if gb >= 1:
        return f"{gb:.1f} GB"
    return f"{mb:.0f} MB"


def fmt_mbps(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 100:
        return f"{value:.0f} Mbps"
    if value >= 10:
        return f"{value:.1f} Mbps"
    return f"{value:.2f} Mbps"


def fmt_ms(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.0f} ms"


def pct_change(old: float | None, new: float | None) -> float | None:
    if old is None or new is None or old == 0:
        return None
    return (new - old) / old * 100.0


def validate_host(raw: str) -> tuple[bool, str, str]:
    """Проверяет IP/домен/URL. Возвращает (ok, нормализованный_host, ошибка)."""
    value = (raw or "").strip()
    if not value:
        return False, "", "Пустое значение"
    if len(value) > 253:
        return False, "", "Слишком длинный адрес"

    # Разрешаем ввод в виде URL
    if "://" in value:
        m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^/\s:]+)(?::(\d+))?", value)
        if not m:
            return False, "", "Не удалось разобрать URL"
        value = m.group(1)

    value = value.strip("/").split("/")[0]
    if value.startswith("[") and "]" in value:  # [IPv6]
        value = value[1 : value.index("]")]

    try:
        ipaddress.ip_address(value)
        return True, value, ""
    except ValueError:
        pass

    if HOSTNAME_RE.match(value):
        if "." not in value and value.lower() not in {"localhost"}:
            return False, "", "Похоже, это не домен и не IP"
        return True, value.lower().rstrip("."), ""
    return False, "", "Некорректный IP или домен"


def parse_ports(raw: str) -> tuple[list[int], str]:
    """«22, 80,443» → [22, 80, 443]."""
    ports: list[int] = []
    for chunk in re.split(r"[,\s;]+", (raw or "").strip()):
        if not chunk:
            continue
        if not chunk.isdigit():
            return [], f"«{chunk}» — не число"
        port = int(chunk)
        if not 1 <= port <= 65535:
            return [], f"Порт {port} вне диапазона 1–65535"
        if port not in ports:
            ports.append(port)
    if len(ports) > 10:
        return [], "Максимум 10 портов"
    return ports, ""


def clean_name(raw: str, limit: int = 40) -> str:
    name = re.sub(r"\s+", " ", (raw or "").strip())
    name = re.sub(r"[<>&]", "", name)
    return name[:limit]


def sparkline(values: list[float | None]) -> str:
    """Текстовый мини-график для случаев, когда картинка не нужна."""
    blocks = "▁▂▃▄▅▆▇█"
    nums = [v for v in values if v is not None]
    if not nums:
        return "—"
    lo, hi = min(nums), max(nums)
    span = hi - lo or 1
    out = []
    for v in values:
        if v is None:
            out.append("·")
        else:
            idx = int((v - lo) / span * (len(blocks) - 1))
            out.append(blocks[idx])
    return "".join(out)


def esc(text: str | None) -> str:
    if text is None:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
