"""Чтение каналов от имени вашего аккаунта Telegram (Telethon, «юзербот»).

Позволяет читать приватные каналы и импортировать все подписки.
Сессия передаётся строкой (TG_SESSION), получить её можно командой
`python -m tgchanreader.login`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..models import Channel, RawPost
from .base import ChannelInfo, Source, SourceError

log = logging.getLogger(__name__)


def _media_kind(msg) -> str | None:  # type: ignore[no-untyped-def]
    from telethon.tl.types import (
        MessageMediaDocument,
        MessageMediaGeo,
        MessageMediaGeoLive,
        MessageMediaPhoto,
        MessageMediaPoll,
        MessageMediaVenue,
        MessageMediaWebPage,
    )

    media = msg.media
    if media is None:
        return None
    if isinstance(media, MessageMediaPhoto):
        return "photo"
    if isinstance(media, MessageMediaPoll):
        return "poll"
    if isinstance(media, (MessageMediaGeo, MessageMediaGeoLive, MessageMediaVenue)):
        return "location"
    if isinstance(media, MessageMediaWebPage):
        return "link"
    if isinstance(media, MessageMediaDocument):
        if getattr(msg, "video_note", None):
            return "round"
        if getattr(msg, "voice", None):
            return "voice"
        if getattr(msg, "gif", None):
            return "gif"
        if getattr(msg, "video", None):
            return "video"
        if getattr(msg, "audio", None):
            return "audio"
        if getattr(msg, "sticker", None):
            return "sticker"
        return "document"
    return "document"


class TelethonSource(Source):
    name = "telethon"

    def __init__(self, api_id: int, api_hash: str, session: str):
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        self._client = TelegramClient(StringSession(session), api_id, api_hash)

    async def start(self) -> None:
        await self._client.connect()
        if not await self._client.is_user_authorized():
            raise SourceError(
                "TG_SESSION недействительна. Сгенерируйте новую: python -m tgchanreader.login"
            )
        me = await self._client.get_me()
        log.info("Telethon: вошли как %s", getattr(me, "username", None) or me.id)

    async def close(self) -> None:
        await self._client.disconnect()

    async def _entity(self, ref: str):  # type: ignore[no-untyped-def]
        try:
            if ref.lstrip("-").isdigit():
                return await self._client.get_entity(int(ref))
            return await self._client.get_entity(ref)
        except Exception as exc:  # noqa: BLE001
            raise SourceError(f"Канал {ref} недоступен: {exc}") from exc

    async def resolve(self, ref: str) -> ChannelInfo:
        entity = await self._entity(ref)
        if not getattr(entity, "broadcast", False) and not getattr(entity, "megagroup", False):
            raise SourceError(f"{ref} — это не канал")
        return ChannelInfo(ref=ref, title=getattr(entity, "title", "") or ref)

    async def resolve_invite(self, invite_hash: str) -> ChannelInfo:
        from telethon.errors import InviteHashExpiredError, InviteHashInvalidError
        from telethon.tl.functions.messages import CheckChatInviteRequest
        from telethon.tl.types import ChatInviteAlready, ChatInvitePeek

        try:
            result = await self._client(CheckChatInviteRequest(invite_hash))
        except (InviteHashExpiredError, InviteHashInvalidError) as exc:
            raise SourceError("Ссылка-приглашение недействительна или устарела") from exc
        except Exception as exc:  # noqa: BLE001
            raise SourceError(f"Не удалось проверить приглашение: {exc}") from exc
        if isinstance(result, (ChatInviteAlready, ChatInvitePeek)):
            chat = result.chat
            username = getattr(chat, "username", None)
            ref = username.lower() if username else f"-100{chat.id}"
            return ChannelInfo(ref=ref, title=getattr(chat, "title", "") or ref)
        title = getattr(result, "title", "") or "канал"
        raise SourceError(
            f"Аккаунт, через который бот читает каналы, не состоит в «{title}». "
            "Вступите в канал по этой ссылке с этого аккаунта и повторите /add."
        )

    async def fetch(self, channel: Channel, since: datetime) -> list[RawPost]:
        entity = await self._entity(channel.ref)
        username = getattr(entity, "username", None)
        groups: dict[int, RawPost] = {}
        result: list[RawPost] = []

        async for msg in self._client.iter_messages(entity, min_id=channel.last_post_id, limit=500):
            posted_at = msg.date.astimezone(timezone.utc) if msg.date else datetime.now(timezone.utc)
            if posted_at < since:
                break
            if getattr(msg, "action", None):  # служебные сообщения
                continue
            text = msg.message or ""
            kind = _media_kind(msg)
            media = [kind] if kind else []
            if not text and not media:
                continue

            if msg.grouped_id:
                # альбом: объединяем в один пост с минимальным id
                existing = groups.get(msg.grouped_id)
                if existing:
                    if text and not existing.text:
                        existing.text = text
                    for m in media:
                        if m not in existing.media:
                            existing.media.append(m)
                    if msg.id < existing.msg_id:
                        existing.msg_id = msg.id
                        existing.posted_at = posted_at
                    continue
                post = RawPost(msg_id=msg.id, posted_at=posted_at, text=text, media=media)
                groups[msg.grouped_id] = post
                result.append(post)
                continue

            result.append(RawPost(msg_id=msg.id, posted_at=posted_at, text=text, media=media))

        for post in result:
            if username:
                post.url = f"https://t.me/{username}/{post.msg_id}"
            else:
                post.url = channel.post_url(post.msg_id)
        return sorted(result, key=lambda p: p.msg_id)

    async def subscribed_channels(self) -> list[ChannelInfo]:
        """Все каналы (broadcast), на которые подписан аккаунт."""
        result: list[ChannelInfo] = []
        async for dialog in self._client.iter_dialogs():
            entity = dialog.entity
            if not getattr(entity, "broadcast", False):
                continue
            username = getattr(entity, "username", None)
            ref = username.lower() if username else f"-100{entity.id}"
            result.append(ChannelInfo(ref=ref, title=getattr(entity, "title", "") or ref))
        return result


class CombinedSource(Source):
    """Telethon для всего, что он умеет; веб-превью как запасной вариант."""

    name = "combined"

    def __init__(self, primary: Source, fallback: Source | None = None):
        self.primary = primary
        self.fallback = fallback

    async def start(self) -> None:
        await self.primary.start()
        if self.fallback:
            await self.fallback.start()

    async def close(self) -> None:
        await self.primary.close()
        if self.fallback:
            await self.fallback.close()

    async def resolve(self, ref: str) -> ChannelInfo:
        try:
            return await self.primary.resolve(ref)
        except SourceError:
            if self.fallback and not ref.lstrip("-").isdigit():
                return await self.fallback.resolve(ref)
            raise

    async def resolve_invite(self, invite_hash: str) -> ChannelInfo:
        return await self.primary.resolve_invite(invite_hash)

    async def fetch(self, channel: Channel, since: datetime) -> list[RawPost]:
        try:
            return await self.primary.fetch(channel, since)
        except SourceError:
            if self.fallback and self.fallback.supports(channel):
                log.warning("Telethon не смог прочитать %s, пробуем веб-превью", channel.ref)
                return await self.fallback.fetch(channel, since)
            raise
