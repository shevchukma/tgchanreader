"""Конфигурация приложения из переменных окружения."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import time
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip()


def _bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on", "да"}


def _int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Переменная {name} должна быть целым числом, получено: {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    return float(raw)


def _int_list(name: str) -> list[int]:
    raw = _env(name)
    result: list[int] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            result.append(int(part))
    return result


_RANGE_RE = re.compile(
    r"^(\d{1,2})(?::(\d{2}))?\s*-\s*(\d{1,2})(?::(\d{2}))?\s*/\s*(\d+)\s*(h|ч|m|мин)?$", re.I
)


def parse_times(raw: str) -> list[time]:
    """Разбирает расписание.

    '08:00, 19:30'      -> 08:00, 19:30
    '06:00-21:00/3'     -> 06:00, 09:00, 12:00, 15:00, 18:00, 21:00 (шаг в часах)
    '09:00-10:00/30m'   -> 09:00, 09:30, 10:00 (шаг в минутах)
    """
    times: list[time] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        m = _RANGE_RE.match(part)
        if m:
            start = int(m.group(1)) * 60 + int(m.group(2) or 0)
            end = int(m.group(3)) * 60 + int(m.group(4) or 0)
            unit = (m.group(6) or "h").lower()
            step = int(m.group(5)) * (1 if unit in {"m", "мин"} else 60)
            if step <= 0 or end < start:
                raise ValueError(f"Неверный интервал в расписании: {part!r}")
            for minute in range(start, end + 1, step):
                times.append(time(minute // 60 % 24, minute % 60))
            continue
        hh, _, mm = part.partition(":")
        try:
            times.append(time(int(hh), int(mm or 0)))
        except ValueError as exc:
            raise ValueError(f"Неверное время в DIGEST_TIMES: {part!r}") from exc
    return sorted(set(times))


# Модели по умолчанию для каждого провайдера (можно переопределить LLM_MODEL).
DEFAULT_MODELS = {
    "openrouter": "google/gemini-2.5-flash",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-5-5",
    "ollama": "qwen2.5:7b",
}

DEFAULT_BASE_URLS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
    "ollama": "http://ollama:11434/v1",
}


@dataclass
class LLMConfig:
    provider: str = "openrouter"  # openrouter | openai | anthropic | ollama | none
    api_key: str = ""
    model: str = ""
    base_url: str = ""
    temperature: float = 0.2
    timeout: float = 120.0
    max_concurrency: int = 3
    batch_size: int = 8
    batch_max_chars: int = 12000

    @property
    def enabled(self) -> bool:
        if self.provider in {"", "none", "off", "disabled"}:
            return False
        if self.provider == "ollama":
            return True
        return bool(self.api_key)


@dataclass
class Config:
    # Telegram-бот (управление + отправка дайджеста)
    bot_token: str = ""
    admin_ids: list[int] = field(default_factory=list)  # полное управление
    allowed_ids: list[int] = field(default_factory=list)  # друзья: получают дайджест, без управления
    digest_chat_ids: list[int] = field(default_factory=list)

    # Источник постов
    source: str = "auto"  # auto | web | telethon
    tg_api_id: int = 0
    tg_api_hash: str = ""
    tg_session: str = ""

    # Расписание
    timezone: str = "Europe/Moscow"
    digest_times: list[time] = field(default_factory=lambda: [time(8, 0)])
    collect_interval_minutes: int = 60
    first_fetch_hours: int = 24
    max_post_age_hours: int = 48

    # Обработка
    summary_threshold: int = 600  # длиннее — сокращаем
    summary_max_chars: int = 350
    short_post_max_chars: int = 800  # короткие посты всё равно обрезаем до этой длины
    skip_ads: bool = True
    digest_overview: bool = True
    digest_language: str = "русский"
    default_priority: int = 2
    send_empty_digest: bool = False

    # Веб / RSS
    web_enabled: bool = True
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    web_base_url: str = "http://localhost:8080"
    web_token: str = ""
    rss_items: int = 50

    # Хранилище
    data_dir: str = "/data"
    channels_file: str = ""

    llm: LLMConfig = field(default_factory=LLMConfig)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "tgchanreader.db")

    @property
    def telethon_configured(self) -> bool:
        return bool(self.tg_api_id and self.tg_api_hash and self.tg_session)

    @property
    def effective_source(self) -> str:
        if self.source == "auto":
            return "telethon" if self.telethon_configured else "web"
        return self.source

    @property
    def access_ids(self) -> set[int]:
        """Все, кому разрешено пользоваться ботом."""
        return set(self.admin_ids) | set(self.allowed_ids)


def load_config() -> Config:
    provider = _env("LLM_PROVIDER", "openrouter").lower()
    llm = LLMConfig(
        provider=provider,
        api_key=_env("LLM_API_KEY") or _env("OPENROUTER_API_KEY"),
        model=_env("LLM_MODEL") or DEFAULT_MODELS.get(provider, ""),
        base_url=(_env("LLM_BASE_URL") or DEFAULT_BASE_URLS.get(provider, "")).rstrip("/"),
        temperature=_float("LLM_TEMPERATURE", 0.2),
        timeout=_float("LLM_TIMEOUT", 120.0),
        max_concurrency=max(1, _int("LLM_MAX_CONCURRENCY", 3)),
        batch_size=max(1, _int("LLM_BATCH_SIZE", 8)),
        batch_max_chars=_int("LLM_BATCH_MAX_CHARS", 12000),
    )

    data_dir = _env("DATA_DIR", "/data")
    channels_file = _env("CHANNELS_FILE")
    if not channels_file:
        candidate = os.path.join(data_dir, "channels.yml")
        if os.path.exists(candidate):
            channels_file = candidate

    cfg = Config(
        bot_token=_env("BOT_TOKEN"),
        admin_ids=_int_list("ADMIN_IDS"),
        allowed_ids=_int_list("ALLOWED_IDS"),
        digest_chat_ids=_int_list("DIGEST_CHAT_IDS"),
        source=_env("SOURCE", "auto").lower(),
        tg_api_id=_int("TG_API_ID", 0),
        tg_api_hash=_env("TG_API_HASH"),
        tg_session=_env("TG_SESSION"),
        timezone=_env("TZ", "Europe/Moscow") or "Europe/Moscow",
        digest_times=parse_times(_env("DIGEST_TIMES", _env("DIGEST_TIME", "08:00"))),
        collect_interval_minutes=max(5, _int("COLLECT_INTERVAL_MINUTES", 60)),
        first_fetch_hours=_int("FIRST_FETCH_HOURS", 24),
        max_post_age_hours=_int("MAX_POST_AGE_HOURS", 48),
        summary_threshold=_int("SUMMARY_THRESHOLD", 600),
        summary_max_chars=_int("SUMMARY_MAX_CHARS", 350),
        short_post_max_chars=_int("SHORT_POST_MAX_CHARS", 800),
        skip_ads=_bool("SKIP_ADS", True),
        digest_overview=_bool("DIGEST_OVERVIEW", True),
        digest_language=_env("DIGEST_LANGUAGE", "русский"),
        default_priority=_int("DEFAULT_PRIORITY", 2),
        send_empty_digest=_bool("SEND_EMPTY_DIGEST", False),
        web_enabled=_bool("WEB_ENABLED", True),
        web_host=_env("WEB_HOST", "0.0.0.0"),
        web_port=_int("WEB_PORT", 8080),
        web_base_url=_env("WEB_BASE_URL", "http://localhost:8080").rstrip("/"),
        web_token=_env("WEB_TOKEN"),
        rss_items=_int("RSS_ITEMS", 50),
        data_dir=data_dir,
        channels_file=channels_file,
        llm=llm,
    )

    if not cfg.digest_times:
        cfg.digest_times = [time(8, 0)]
    if cfg.source not in {"auto", "web", "telethon"}:
        raise ValueError("SOURCE должен быть auto, web или telethon")
    if cfg.source == "telethon" and not cfg.telethon_configured:
        raise ValueError("SOURCE=telethon требует TG_API_ID, TG_API_HASH и TG_SESSION")
    return cfg
