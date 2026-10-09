"""Yapılandırma: tek kaynak ortam değişkenleri.

Yerelde proje kökündeki .env de okunur; gerçek ortam değişkenleri her zaman önceliklidir.
Docker'da compose `env_file` ile aynı .env'i verir, böylece iki ortam birebir aynı çalışır.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

STUDENT_URL = "https://student.ekampus.ticaret.edu.tr"
IDP_URL = "https://ekampus.ticaret.edu.tr"

LLM_BASE_URLS = {
    "deepseek": "https://api.deepseek.com",
    "xai": "https://api.x.ai/v1",
}

# Özellik grubu → o grubun çalışması için dolu olması gereken alanlar
GROUPS = {
    "ekampus": ("EKAMPUS_USERNAME", "EKAMPUS_PASSWORD"),
    "telegram": ("TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_CHAT_ID"),
    "llm": ("LLM_API_KEY",),
    "pushover": ("PUSHOVER_APP_TOKEN", "PUSHOVER_USER_KEY"),  # isteğe bağlı: sistem uyarıları kanalı
}


class ConfigError(Exception):
    pass


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} bir tam sayı olmalı, gelen: {raw!r}") from None


def _bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on", "evet"):
        return True
    if raw in ("0", "false", "no", "off", "hayir", "hayır"):
        return False
    raise ConfigError(f"{name} true/false olmalı, gelen: {raw!r}")


def _clock(name: str, raw: str) -> time:
    try:
        hour, minute = raw.strip().split(":")
        return time(int(hour), int(minute))
    except ValueError:
        raise ConfigError(f"{name} SS:DD biçiminde olmalı, gelen: {raw!r}") from None


def _default_data_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ekampus"
    return Path.home() / ".local" / "share" / "ekampus"


@dataclass(frozen=True)
class Settings:
    username: str
    password: str = field(repr=False)
    telegram_token: str = field(repr=False)
    telegram_owner_chat_id: int | None
    llm_provider: str
    llm_api_key: str = field(repr=False)
    llm_model: str
    llm_base_url: str
    llm_daily_token_budget: int
    tz: ZoneInfo
    poll_interval_min: int
    night_poll_interval_min: int
    night_start: time
    night_end: time
    daily_digest_time: time
    reminder_hours: tuple[int, ...]
    live_lesson_reminder_min: int
    data_dir: Path
    headless: bool
    llm_history_messages: int = 20
    pushover_app_token: str = field(default="", repr=False)
    pushover_user_key: str = field(default="", repr=False)
    error_spike_per_hour: int = 5
    llm_alerts_per_day: int = 3

    @property
    def pushover_enabled(self) -> bool:
        return bool(self.pushover_app_token and self.pushover_user_key)

    @property
    def watchdog_marker_path(self) -> Path:
        return self.data_dir / "watchdog_exit"

    @property
    def session_path(self) -> Path:
        return self.data_dir / "session.json"

    @property
    def auth_guard_path(self) -> Path:
        return self.data_dir / "auth_guard.json"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def heartbeat_path(self) -> Path:
        return self.data_dir / "heartbeat"

    @property
    def explore_dir(self) -> Path:
        return self.data_dir / "explore"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    def missing(self, group: str) -> list[str]:
        values = {
            "EKAMPUS_USERNAME": self.username,
            "EKAMPUS_PASSWORD": self.password,
            "TELEGRAM_BOT_TOKEN": self.telegram_token,
            "TELEGRAM_OWNER_CHAT_ID": self.telegram_owner_chat_id,
            "LLM_API_KEY": self.llm_api_key,
            "PUSHOVER_APP_TOKEN": self.pushover_app_token,
            "PUSHOVER_USER_KEY": self.pushover_user_key,
        }
        return [name for name in GROUPS[group] if not values[name]]

    def require(self, group: str) -> None:
        missing = self.missing(group)
        if missing:
            raise ConfigError(f".env içinde eksik alan(lar): {', '.join(missing)}")


def load_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env", override=False)

    tz_name = _env("TIMEZONE", "Europe/Istanbul")
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        raise ConfigError(f"TIMEZONE geçersiz: {tz_name!r}") from None

    owner_raw = _env("TELEGRAM_OWNER_CHAT_ID")
    try:
        owner_id = int(owner_raw) if owner_raw else None
    except ValueError:
        raise ConfigError(f"TELEGRAM_OWNER_CHAT_ID sayı olmalı, gelen: {owner_raw!r}") from None

    provider = _env("LLM_PROVIDER", "deepseek").lower()
    base_url = _env("LLM_BASE_URL") or LLM_BASE_URLS.get(provider, "")
    if not base_url:
        raise ConfigError(
            f"LLM_PROVIDER {provider!r} tanınmıyor; {', '.join(LLM_BASE_URLS)} kullan ya da LLM_BASE_URL ver"
        )

    night = _env("NIGHT_HOURS", "01:00-07:00")
    try:
        night_start_raw, night_end_raw = night.split("-")
    except ValueError:
        raise ConfigError(f"NIGHT_HOURS SS:DD-SS:DD biçiminde olmalı, gelen: {night!r}") from None

    reminder_raw = _env("REMINDER_HOURS", "24,3")
    try:
        reminder_hours = tuple(sorted({int(h) for h in reminder_raw.split(",") if h.strip()}, reverse=True))
    except ValueError:
        raise ConfigError(f"REMINDER_HOURS virgülle ayrılmış saatler olmalı, gelen: {reminder_raw!r}") from None

    poll = _int("POLL_INTERVAL_MIN", 15)
    if poll < 5:
        raise ConfigError("POLL_INTERVAL_MIN en az 5 olmalı (siteyi yormamak için)")

    data_dir = Path(_env("DATA_DIR")) if _env("DATA_DIR") else _default_data_dir()

    return Settings(
        username=_env("EKAMPUS_USERNAME"),
        password=os.environ.get("EKAMPUS_PASSWORD", ""),  # şifrede baş/son boşluk olabilir, kırpma
        telegram_token=_env("TELEGRAM_BOT_TOKEN"),
        telegram_owner_chat_id=owner_id,
        llm_provider=provider,
        llm_api_key=_env("LLM_API_KEY"),
        llm_model=_env("LLM_MODEL"),
        llm_base_url=base_url,
        llm_daily_token_budget=_int("LLM_DAILY_TOKEN_BUDGET", 200_000),
        tz=tz,
        poll_interval_min=poll,
        night_poll_interval_min=_int("NIGHT_POLL_INTERVAL_MIN", 60),
        night_start=_clock("NIGHT_HOURS", night_start_raw),
        night_end=_clock("NIGHT_HOURS", night_end_raw),
        daily_digest_time=_clock("DAILY_DIGEST_TIME", _env("DAILY_DIGEST_TIME", "08:00")),
        reminder_hours=reminder_hours,
        live_lesson_reminder_min=_int("LIVE_LESSON_REMINDER_MIN", 15),
        data_dir=data_dir,
        headless=_bool("HEADLESS", True),
        llm_history_messages=max(0, _int("LLM_HISTORY_MESSAGES", 20)),
        pushover_app_token=_env("PUSHOVER_APP_TOKEN"),
        pushover_user_key=_env("PUSHOVER_USER_KEY"),
        error_spike_per_hour=max(1, _int("ERROR_SPIKE_PER_HOUR", 5)),
        llm_alerts_per_day=max(0, _int("LLM_ALERTS_PER_DAY", 3)),
    )
