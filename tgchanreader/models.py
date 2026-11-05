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
    # поля подписки конкретного пользователя (если канал получен через подписку)
    cursor: int = 0
    subscribed_at: datetime | None = None

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


_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_INVITE_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")
# пути t.me, которые не являются каналами
_RESERVED = {
    "share", "addstickers", "addemoji", "addtheme", "addlist", "proxy", "socks", "iv",
    "login", "setlanguage", "confirmphone", "invoice", "bg", "m", "contact", "giftcode",
}
_TG_HOSTS = r"(?:t\.me|telegram\.me|telegram\.dog)"

# ссылки и @упоминания каналов внутри произвольного текста
LINK_RE = re.compile(
    rf"(?:https?://)?(?:www\.)?(?:[A-Za-z0-9_]+\.)?{_TG_HOSTS}/[^\s<>\"'«»()]+"
    rf"|(?:https?://)?[A-Za-z][A-Za-z0-9_]{{3,31}}\.{_TG_HOSTS}\b"
    r"|tg://resolve\?[^\s<>\"']+"
    r"|(?<![\w@])@[A-Za-z][A-Za-z0-9_]{3,31}\b",
    re.I,
)


def is_invite(ref: str) -> bool:
    """normalize_ref() возвращает '+hash' для ссылок-приглашений."""
    return ref.startswith("+")


def normalize_ref(raw: str) -> str | None:
    """Приводит к ref любой вид ссылки на канал.

    @name · name · t.me/name · https://t.me/s/name · t.me/name/123 · name.t.me ·
    tg://resolve?domain=name · t.me/c/123456/78 и -100123456 (приватный канал по id) →
    'name' или '-100123456'; t.me/+hash и t.me/joinchat/hash (приглашение) → '+hash'.
    """
    value = raw.strip().strip("<>()[]{}\"'«»").rstrip(".,;:!?…")
    if not value:
        return None

    m = re.match(r"^tg://resolve\?(.*)$", value, re.I)
    if m:
        params = dict(p.partition("=")[::2] for p in m.group(1).split("&"))
        value = params.get("domain", "")
    m = re.match(r"^tg://join\?invite=([A-Za-z0-9_-]+)", value, re.I)
    if m:
        return f"+{m.group(1)}"

    # name.t.me
    m = re.match(rf"^(?:https?://)?([A-Za-z][A-Za-z0-9_]{{3,31}})\.{_TG_HOSTS}/?$", value, re.I)
    if m and m.group(1).lower() != "www":
        value = m.group(1)

    value = re.sub(rf"^(?:https?://)?(?:www\.)?{_TG_HOSTS}/", "", value, flags=re.I)
    value = value.split("?")[0].split("#")[0].strip("/")
    parts = [p for p in value.split("/") if p]
    if not parts:
        return None
    first = parts[0]

    if first.startswith("+"):
        return f"+{first[1:]}" if _INVITE_RE.match(first[1:]) else None
    if first.lower() == "joinchat":
        return f"+{parts[1]}" if len(parts) > 1 and _INVITE_RE.match(parts[1]) else None
    if first.lower() == "c":
        # t.me/c/123456/789 -> приватный канал
        return f"-100{parts[1]}" if len(parts) > 1 and parts[1].isdigit() else None
    if first.lower() in {"s", "boost"} and len(parts) > 1:
        first = parts[1]
    if first.lower() in _RESERVED:
        return None

    value = first.lstrip("@")
    if value.lstrip("-").isdigit():
        if not value.startswith("-"):
            value = f"-100{value}"
        return value
    if _USERNAME_RE.match(value):
        return value.lower()
    return None


def find_refs(text: str) -> list[str]:
    """Все каналы, упомянутые в тексте ссылками или @именами (без повторов)."""
    refs: list[str] = []
    for m in LINK_RE.finditer(text or ""):
        ref = normalize_ref(m.group(0))
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def parse_tags(raw: str) -> list[str]:
    tags: list[str] = []
    for part in re.split(r"[,;\s]+", raw):
        tag = part.strip().lstrip("#").lower()
        if tag and tag not in tags:
            tags.append(tag)
    return tags
