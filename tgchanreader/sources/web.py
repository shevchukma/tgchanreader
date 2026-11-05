"""Чтение публичных каналов через веб-превью https://t.me/s/<channel>.

Не требует аккаунта Telegram, но работает только для публичных каналов,
у которых включён предпросмотр.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup, Tag

from ..models import Channel, RawPost
from .base import ChannelInfo, Source, SourceError

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

MEDIA_SELECTORS = [
    (".tgme_widget_message_photo_wrap", "photo"),
    (".tgme_widget_message_video_player", "video"),
    (".tgme_widget_message_roundvideo_player", "round"),
    (".tgme_widget_message_voice_player", "voice"),
    (".tgme_widget_message_audio_player", "audio"),
    (".tgme_widget_message_document_wrap", "document"),
    (".tgme_widget_message_poll", "poll"),
    (".tgme_widget_message_sticker_wrap", "sticker"),
    (".tgme_widget_message_location_wrap", "location"),
]


def _html_to_text(node: Tag) -> str:
    for br in node.find_all("br"):
        br.replace_with("\n")
    text = node.get_text()
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_channel_page(html: str, ref: str) -> tuple[str, list[RawPost]]:
    """Разбирает страницу t.me/s/<ref>. Возвращает (название, посты)."""
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    title_node = soup.select_one(".tgme_channel_info_header_title")
    if title_node:
        title = title_node.get_text(strip=True)
    if not title:
        meta = soup.find("meta", attrs={"property": "og:title"})
        if meta and meta.get("content"):
            title = str(meta["content"]).strip()

    posts: list[RawPost] = []
    for msg in soup.select(".tgme_widget_message[data-post]"):
        if "service_message" in (msg.get("class") or []):
            continue
        data_post = str(msg.get("data-post", ""))
        _, _, msg_id_raw = data_post.rpartition("/")
        if not msg_id_raw.isdigit():
            continue
        msg_id = int(msg_id_raw)

        time_node = msg.select_one(".tgme_widget_message_date time[datetime]") or msg.select_one(
            "time[datetime]"
        )
        if not time_node:
            continue
        try:
            posted_at = datetime.fromisoformat(str(time_node["datetime"]))
        except ValueError:
            continue
        if posted_at.tzinfo is None:
            posted_at = posted_at.replace(tzinfo=timezone.utc)

        text = ""
        for node in msg.select(".tgme_widget_message_text"):
            # пропускаем цитату из поста, на который отвечают
            if node.find_parent(class_="tgme_widget_message_reply"):
                continue
            text = _html_to_text(node)
            break

        media: list[str] = []
        for selector, kind in MEDIA_SELECTORS:
            if msg.select_one(selector) and kind not in media:
                media.append(kind)
        if not media and msg.select_one(".tgme_widget_message_link_preview"):
            media.append("link")

        if not text and not media:
            continue

        forwarded = msg.select_one(".tgme_widget_message_forwarded_from_name")
        if forwarded and text:
            text = f"[Переслано из: {forwarded.get_text(strip=True)}]\n{text}"

        posts.append(
            RawPost(
                msg_id=msg_id,
                posted_at=posted_at.astimezone(timezone.utc),
                text=text,
                media=media,
                url=f"https://t.me/{data_post}",
            )
        )
    posts.sort(key=lambda p: p.msg_id)
    return title, posts


class WebSource(Source):
    name = "web"

    def __init__(self, max_pages: int = 15, delay: float = 1.0, timeout: float = 30.0):
        self.max_pages = max_pages
        self.delay = delay
        self._client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"},
            timeout=timeout,
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    def supports(self, channel: Channel) -> bool:
        return not channel.is_private

    async def _get(self, ref: str, before: int | None = None) -> str:
        params = {"before": str(before)} if before else None
        url = f"https://t.me/s/{ref}"
        for attempt in range(3):
            try:
                resp = await self._client.get(url, params=params)
                if resp.status_code == 429:
                    await asyncio.sleep(10 * (attempt + 1))
                    continue
                resp.raise_for_status()
                return resp.text
            except httpx.HTTPError as exc:
                if attempt == 2:
                    raise SourceError(f"Не удалось загрузить {url}: {exc}") from exc
                await asyncio.sleep(2 * (attempt + 1))
        raise SourceError(f"Не удалось загрузить {url}")

    async def resolve(self, ref: str) -> ChannelInfo:
        if ref.lstrip("-").isdigit():
            raise SourceError(
                "Приватные каналы доступны только через Telethon (нужны TG_API_ID/TG_API_HASH/TG_SESSION)"
            )
        html = await self._get(ref)
        title, posts = parse_channel_page(html, ref)
        if "tgme_channel_info" not in html and not posts:
            raise SourceError(
                f"Канал @{ref} не найден или у него отключён веб-предпросмотр"
            )
        return ChannelInfo(ref=ref, title=title or ref)

    async def fetch(self, channel: Channel, since: datetime) -> list[RawPost]:
        collected: dict[int, RawPost] = {}
        before: int | None = None
        for page in range(self.max_pages):
            html = await self._get(channel.ref, before)
            _, posts = parse_channel_page(html, channel.ref)
            if not posts:
                break
            done = False
            for p in posts:
                if p.msg_id > channel.last_post_id and p.posted_at >= since:
                    collected[p.msg_id] = p
            oldest = posts[0]
            if oldest.msg_id <= channel.last_post_id or oldest.posted_at < since:
                done = True
            if done or oldest.msg_id <= 1:
                break
            before = oldest.msg_id
            await asyncio.sleep(self.delay)
        return sorted(collected.values(), key=lambda p: p.msg_id)
