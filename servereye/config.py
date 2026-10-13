"""Конфигурация ServerEye. Все секреты берутся из окружения / .env."""
from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Telegram ---
    bot_token: str = Field(..., alias="BOT_TOKEN")
    admin_ids_raw: str = Field("", alias="ADMIN_IDS")

    # --- База данных ---
    database_url: str = Field(
        f"sqlite+aiosqlite:///{BASE_DIR / 'servereye.db'}", alias="DATABASE_URL"
    )

    # --- Мониторинг ---
    server_check_interval_minutes: int = Field(60, alias="SERVER_CHECK_INTERVAL_MINUTES")
    max_concurrent_checks: int = Field(10, alias="MAX_CONCURRENT_CHECKS")
    ping_count: int = Field(4, alias="PING_COUNT")
    ping_timeout: float = Field(4.0, alias="PING_TIMEOUT")
    http_timeout: float = Field(10.0, alias="HTTP_TIMEOUT")
    tcp_timeout: float = Field(6.0, alias="TCP_TIMEOUT")

    # --- Отчёты ---
    hourly_report_minute: int = Field(0, alias="HOURLY_REPORT_MINUTE")
    daily_report_hour: int = Field(9, alias="DAILY_REPORT_HOUR")
    default_tz_offset: int = Field(3, alias="DEFAULT_TZ_OFFSET")

    # --- API для ServerEye Agent ---
    agent_api_enabled: bool = Field(True, alias="AGENT_API_ENABLED")
    agent_api_host: str = Field("0.0.0.0", alias="AGENT_API_HOST")
    agent_api_port: int = Field(8787, alias="AGENT_API_PORT")
    agent_public_url: str = Field("http://127.0.0.1:8787", alias="AGENT_PUBLIC_URL")
    agent_offline_after_seconds: int = Field(180, alias="AGENT_OFFLINE_AFTER_SECONDS")
    agent_heartbeat_seconds: int = Field(60, alias="AGENT_HEARTBEAT_SECONDS")

    # --- Машина, на которой работает бот ---
    # Пинговать себя (127.0.0.1) бессмысленно — меряем интернет: первая
    # доступная цель из списка. По умолчанию Google, запасная — Cloudflare.
    local_ping_targets: str = Field("8.8.8.8,1.1.1.1", alias="LOCAL_PING_TARGETS")

    # --- Железо и питание (для машины, на которой работает бот) ---
    power_control_enabled: bool = Field(True, alias="POWER_CONTROL_ENABLED")
    temp_alarm_repeat_minutes: float = Field(3.0, alias="TEMP_ALARM_REPEAT_MINUTES")
    temp_alarm_clear_delta: float = Field(5.0, alias="TEMP_ALARM_CLEAR_DELTA")
    battery_alert_levels: str = Field("20,10,5", alias="BATTERY_ALERT_LEVELS")

    # --- Хранение ---
    data_retention_days: int = Field(45, alias="DATA_RETENTION_DAYS")

    # --- Прочее ---
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    charts_enabled: bool = Field(True, alias="CHARTS_ENABLED")

    @field_validator("bot_token")
    @classmethod
    def _validate_token(cls, v: str) -> str:
        v = v.strip()
        if not v or ":" not in v:
            raise ValueError("BOT_TOKEN выглядит некорректно (ожидается формат 123456:ABC...)")
        return v

    @property
    def admin_ids(self) -> set[int]:
        out: set[int] = set()
        for part in self.admin_ids_raw.replace(";", ",").split(","):
            part = part.strip()
            if part.isdigit():
                out.add(int(part))
        return out

    @property
    def local_ping_target_list(self) -> list[str]:
        return [x.strip() for x in self.local_ping_targets.split(",") if x.strip()]

    @property
    def battery_levels(self) -> list[int]:
        levels = [int(x) for x in self.battery_alert_levels.split(",") if x.strip().isdigit()]
        return sorted(set(levels), reverse=True)

    @property
    def db_path(self) -> Path | None:
        if self.database_url.startswith("sqlite"):
            return Path(self.database_url.split("///")[-1])
        return None


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def new_agent_token() -> str:
    """Уникальный токен агента вида SE-xxxxxxxxxxxxxxxx."""
    return "SE-" + secrets.token_hex(8)
