"""Сборка и рендеринг дайджеста."""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import groupby

from .config import Config
from .db import Database
from .models import MEDIA_LABELS, Channel, Post
from .processor import Processor, truncate

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096
CHUNK_LIMIT = 3900

PRIORITY_ICONS = {1: "🔴", 2: "🟠", 3: "🟡"}
MEDIA_ICONS = {
    "photo": "🖼",
    "video": "🎬",
    "gif": "🎞",
    "round": "📹",
    "voice": "🎙",
    "audio": "🎵",
    "document": "📎",
    "poll": "📊",
    "sticker": "🏷",
    "location": "📍",
    "link": "🔗",
}
MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def esc_attr(text: str) -> str:
    return html.escape(text, quote=True)


def human_date(dt: datetime) -> str:
    return f"{dt.day} {MONTHS[dt.month - 1]} {dt.year}"


@dataclass
class Digest:
    title: str
    blocks: list[str]
    stats: dict[str, int]
    post_ids: list[int] = field(default_factory=list)
    id: int | None = None

    @property
    def html(self) -> str:
        return "\n".join(self.blocks).strip()

    def chunks(self, limit: int = CHUNK_LIMIT) -> list[str]:
        return split_blocks(self.blocks, limit)


def split_blocks(blocks: list[str], limit: int = CHUNK_LIMIT) -> list[str]:
    """Склеивает блоки в сообщения не длиннее limit, не разрывая HTML внутри блока."""
    chunks: list[str] = []
    current = ""
    for block in blocks:
        if len(block) > limit:
            # аварийный случай: режем по строкам
            for line in block.split("\n"):
                line = line[:limit]
                if len(current) + len(line) + 1 > limit:
                    chunks.append(current.strip())
                    current = ""
                current += line + "\n"
            continue
        if current and len(current) + len(block) + 1 > limit:
            chunks.append(current.strip())
            current = ""
        current += block + "\n"
    if current.strip():
        chunks.append(current.strip())
    return chunks


def media_label(media: list[str]) -> str:
    return ", ".join(MEDIA_LABELS.get(m, m) for m in media)


def render_post(post: Post, cfg: Config) -> str:
    local_time = post.posted_at.astimezone(cfg.tz).strftime("%H:%M")
    url = esc_attr(post.url)
    icons = "".join(MEDIA_ICONS.get(m, "") for m in post.media if m != "link")

    if post.media_only:
        icon = MEDIA_ICONS.get(post.media[0], "📎")
        return (
            f"▫️ <i>{local_time}</i> {icon} <i>Только медиа ({esc(media_label(post.media))})</i> — "
            f'<a href="{url}">открыть пост</a>'
        )

    if post.summarized and post.summary:
        body = esc(post.summary)
        link = f'<a href="{url}">читать полностью</a>'
        prefix = "✂️ "
    else:
        body = esc(truncate(post.text, cfg.short_post_max_chars))
        link = f'<a href="{url}">оригинал</a>'
        prefix = ""
    media_note = f" {icons}" if icons else ""
    return f"▫️ <i>{local_time}</i>{media_note} {prefix}{body}\n    → {link}"


def _sort_key(item: tuple[Post, Channel]) -> tuple:
    post, ch = item
    tag = ch.primary_tag
    return (ch.priority, tag == "", tag, ch.display_name.lower(), ch.id, post.posted_at)


def render_digest(
    entries: list[tuple[Post, Channel]],
    cfg: Config,
    title: str,
    stats: dict[str, int],
    overview: str = "",
) -> list[str]:
    blocks: list[str] = []
    header = f"📰 <b>{esc(title)}</b>\n"
    header += f"Постов: {stats['posts']} · каналов: {stats['channels']}"
    if stats.get("ads"):
        header += f" · пропущено рекламы: {stats['ads']}"
    blocks.append(header)

    if overview:
        blocks.append(f"\n🧭 <b>Главное за день</b>\n{esc(overview)}")

    entries = sorted(entries, key=_sort_key)
    for priority, by_priority in groupby(entries, key=lambda e: e[1].priority):
        icon = PRIORITY_ICONS.get(priority, "⚪")
        blocks.append(f"\n{icon} <b>ПРИОРИТЕТ {priority}</b>")
        by_priority = list(by_priority)
        tags_in_priority = {e[1].primary_tag for e in by_priority}
        show_tags = tags_in_priority != {""}
        for tag, by_tag in groupby(by_priority, key=lambda e: e[1].primary_tag):
            if show_tags:
                tag_title = f"#{esc(tag)}" if tag else "без тега"
                blocks.append(f"\n🏷 <b>{tag_title}</b>")
            for _, by_channel in groupby(by_tag, key=lambda e: e[1].id):
                items = list(by_channel)
                channel = items[0][1]
                extra_tags = " ".join(f"#{esc(t)}" for t in channel.tags[1:])
                ch_line = (
                    f'\n📢 <b><a href="{esc_attr(channel.link)}">{esc(channel.display_name)}</a></b>'
                )
                if extra_tags:
                    ch_line += f" <i>{extra_tags}</i>"
                blocks.append(ch_line)
                for post, _ in items:
                    blocks.append(render_post(post, cfg))
    return blocks


class DigestBuilder:
    def __init__(self, cfg: Config, db: Database, processor: Processor):
        self.cfg = cfg
        self.db = db
        self.processor = processor

    async def build(self, tag: str | None = None, save: bool = True) -> Digest | None:
        await self.processor.process_pending()
        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=self.cfg.max_post_age_hours)
        posts = await self.db.undigested_posts(since)
        channels = {c.id: c for c in await self.db.list_channels()}

        entries: list[tuple[Post, Channel]] = []
        ads: list[Post] = []
        for post in posts:
            channel = channels.get(post.channel_id)
            if channel is None:
                continue
            if tag and tag not in channel.tags:
                continue
            if post.is_ad and self.cfg.skip_ads:
                ads.append(post)
                continue
            entries.append((post, channel))

        if not entries and not self.cfg.send_empty_digest:
            if save and ads:
                # пометим рекламу, чтобы не висела в очереди
                await self.db.mark_digested([p.id for p in ads], 0)
            return None

        local_now = now.astimezone(self.cfg.tz)
        title = f"Дайджест за {human_date(local_now)}"
        if tag:
            title += f" · #{tag}"
        stats = {
            "posts": len(entries),
            "channels": len({c.id for _, c in entries}),
            "ads": len(ads),
            "summarized": sum(1 for p, _ in entries if p.summarized),
        }

        overview = ""
        if self.cfg.digest_overview:
            overview = await self.processor.overview(sorted(entries, key=_sort_key))

        blocks = render_digest(entries, self.cfg, title, stats, overview)
        if not entries:
            blocks.append("\nНовых постов нет 🌿")
        digest = Digest(
            title=title,
            blocks=blocks,
            stats=stats,
            post_ids=[p.id for p, _ in entries] + [p.id for p in ads],
        )
        if save:
            digest.id = await self.db.save_digest(title, digest.html, stats)
            await self.db.mark_digested(digest.post_ids, digest.id)
        return digest
