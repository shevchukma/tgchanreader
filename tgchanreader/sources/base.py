from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from ..models import Channel, RawPost


class SourceError(Exception):
    pass


@dataclass
class ChannelInfo:
    ref: str
    title: str


class Source(ABC):
    name = "base"

    async def start(self) -> None:  # pragma: no cover - default no-op
        pass

    async def close(self) -> None:  # pragma: no cover - default no-op
        pass

    @abstractmethod
    async def resolve(self, ref: str) -> ChannelInfo:
        """Проверяет, что канал существует и доступен; возвращает его название."""

    @abstractmethod
    async def fetch(self, channel: Channel, since: datetime) -> list[RawPost]:
        """Возвращает посты новее channel.last_post_id и не старше since."""

    async def resolve_invite(self, invite_hash: str) -> ChannelInfo:
        """Канал по ссылке-приглашению t.me/+hash (только Telethon)."""
        raise SourceError(
            "Ссылки-приглашения в приватные каналы (t.me/+…) работают только в режиме Telethon "
            "(нужны TG_API_ID, TG_API_HASH, TG_SESSION). Для публичного канала пришлите ссылку "
            "вида t.me/имя_канала или @имя_канала."
        )

    def supports(self, channel: Channel) -> bool:
        return True
