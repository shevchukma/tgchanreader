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

    def supports(self, channel: Channel) -> bool:
        return True
