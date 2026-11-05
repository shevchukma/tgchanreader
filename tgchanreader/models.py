from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

MEDIA_LABELS = {
    "photo": "фото",
    "video": "видео",
    "gif": "GIF",
    "round": "видеосообщение",
    "voice": "голосовое",
    "audio": "аудио",
    "document": "файл",
    "poll": "опрос",
    "sticker": "стикер",
    "location": "геолокация",
    "link": "ссылка",
}


@dataclass
class Channel:
    id: int
    ref: str  # username без @ или числовой id (-100...) для приватных каналов
    title: str = ""
    tags: list[str] = field(default_factory=list)
    priority: int = 2
    active: bool = True
    last_post_id: int = 0

    @property
    def display_name(self) -> str:
        return self.title or (f"@{self.ref}" if not self.is_private else self.ref)

    @property
    def is_private(self) -> bool:
        return self.ref.lstrip("-").isdigit()

    @property
    def link(self) -> str:
        if self.is_private:
            internal = self.ref[4:] if self.ref.startswith("-100") else self.ref.lstrip("-")
            return f"https://t.me/c/{internal}"
        return f"https://t.me/{self.ref}"

    def post_url(self, msg_id: int) -> str:
        return f"{self.link}/{msg_id}"

    @property
    def primary_tag(self) -> str:
        return self.tags[0] if self.tags else ""


@dataclass
class RawPost:
    """Пост, полученный из источника (ещё не сохранён)."""

    msg_id: int
    posted_at: datetime  # aware, UTC
    text: str
    media: list[str] = field(default_factory=list)
    url: str = ""


@dataclass
class Post:
    id: int
    channel_id: int
    msg_id: int
    url: str
    posted_at: datetime
    text: str
    media: list[str]
    status: str = "new"  # new | processed
    is_ad: bool = False
    summary: str = ""
    summarized: bool = False
    digest_id: int | None = None

    @property
    def media_only(self) -> bool:
        return not self.text.strip() and bool(self.media)


_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,}$")


def normalize_ref(raw: str) -> str | None:
    """Приводит @name / t.me/name / https://t.me/s/name / -100123 к ref."""
    value = raw.strip()
    if not value:
        return None
    value = re.sub(r"^(https?://)?(www\.)?(t\.me|telegram\.me|telegram\.dog)/", "", value)
    if value.startswith("s/"):
        value = value[2:]
    value = value.split("?")[0].strip("/")
    if "/" in value:
        first, *_ = value.split("/")
        if first == "c":
            # t.me/c/123456/789 -> приватный канал
            parts = value.split("/")
            if len(parts) > 1 and parts[1].isdigit():
                return f"-100{parts[1]}"
            return None
        value = first
    value = value.lstrip("@")
    if value.lstrip("-").isdigit():
        if not value.startswith("-"):
            value = f"-100{value}"
        return value
    if _USERNAME_RE.match(value):
        return value.lower()
    return None


def parse_tags(raw: str) -> list[str]:
    tags: list[str] = []
    for part in re.split(r"[,;\s]+", raw):
        tag = part.strip().lstrip("#").lower()
        if tag and tag not in tags:
            tags.append(tag)
    return tags
