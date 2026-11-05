"""Сборка и рендеринг дайджеста.

Дайджест — это шапка (заголовок, статистика, «Главное») и по одному сообщению
на канал. Если пост канала не помещаются в одно сообщение Telegram, нейросеть
отбирает важное и сжимает остальное; без нейросети сообщение делится на части.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import groupby

from .config import Config
from .db import Database
from .models import MEDIA_LABELS, Channel, Post
from .processor import Processor, truncate

log = logging.getLogger(__name__)

# Telegram ограничивает сообщение 4096 символами (UTF-16) ПОСЛЕ разбора HTML-разметки.
TELEGRAM_LIMIT = 4096
MESSAGE_LIMIT = 4000

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

_TAG_RE = re.compile(r"<[^>]+>")


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def esc_attr(text: str) -> str:
    return html.escape(text, quote=True)


def human_date(dt: datetime) -> str:
    return f"{dt.day} {MONTHS[dt.month - 1]} {dt.year}"


def visible_len(text: str) -> int:
    """Длина текста так, как её считает Telegram: без тегов, в UTF-16."""
    plain = html.unescape(_TAG_RE.sub("", text))
    return len(plain.encode("utf-16-le")) // 2


def split_blocks(blocks: list[str], limit: int = MESSAGE_LIMIT, sep: str = "\n") -> list[str]:
    """Склеивает блоки в сообщения не длиннее limit, не разрывая HTML внутри блока."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    sep_len = visible_len(sep)
    for block in blocks:
        block_len = visible_len(block)
        if block_len > limit:
            # аварийный случай: блок сам по себе огромный — режем по строкам
            for line in block.split("\n"):
                line_len = visible_len(line)
                if line_len > limit:
                    line = esc(truncate(html.unescape(_TAG_RE.sub("", line)), limit - 10))
                    line_len = visible_len(line)
                if current and size + sep_len + line_len > limit:
                    chunks.append(sep.join(current).strip())
                    current, size = [], 0
                current.append(line)
                size += line_len + sep_len
            continue
        if current and size + sep_len + block_len > limit:
            chunks.append(sep.join(current).strip())
            current, size = [], 0
        current.append(block)
        size += block_len + sep_len
    if current and sep.join(current).strip():
        chunks.append(sep.join(current).strip())
    return chunks


def media_label(media: list[str]) -> str:
    return ", ".join(MEDIA_LABELS.get(m, m) for m in media)


def _time(post: Post, cfg: Config) -> str:
    return post.posted_at.astimezone(cfg.tz).strftime("%H:%M")


def _icons(post: Post) -> str:
    icons = "".join(MEDIA_ICONS.get(m, "") for m in post.media if m != "link")
    return f" {icons}" if icons else ""


def render_post(post: Post, cfg: Config) -> str:
    local_time = _time(post, cfg)
    url = esc_attr(post.url)

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
        # summary у короткого поста — его текст с применёнными указаниями (/instructions)
        body = esc(truncate(post.summary or post.text, cfg.short_post_max_chars))
        link = f'<a href="{url}">оригинал</a>'
        prefix = ""
    return f"▫️ <i>{local_time}</i>{_icons(post)} {prefix}{body}\n    → {link}"


def render_compact(post: Post, text: str, cfg: Config) -> str:
    """Короткая строка для сжатого режима: время — ссылка на пост."""
    url = esc_attr(post.url)
    if post.media_only:
        icon = MEDIA_ICONS.get(post.media[0], "📎")
        body = f"{icon} <i>только медиа ({esc(media_label(post.media))})</i>"
    else:
        body = esc(text)
    icons = "" if post.media_only else _icons(post)
    return f'▫️ <a href="{url}">{_time(post, cfg)}</a>{icons} {body}'


def channel_header(channel: Channel, continued: bool = False) -> str:
    line = f'<b><a href="{esc_attr(channel.link)}">{esc(channel.display_name)}</a></b>'
    if continued:
        line += " <i>(продолжение)</i>"
    return line


def channel_tags(channel: Channel) -> str:
    """Теги канала — в самом низу сообщения."""
    tags = " ".join(f"#{esc(t)}" for t in channel.tags)
    return f"\n\n{tags}" if tags else ""


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 19:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def sort_key(item: tuple[Post, Channel]) -> tuple:
    post, ch = item
    tag = ch.primary_tag
    return (ch.priority, tag == "", tag, ch.display_name.lower(), ch.id, post.posted_at)


@dataclass
class Section:
    channel: Channel
    posts: list[Post]
    parts: list[str] = field(default_factory=list)
    compressed: bool = False
    dropped: int = 0


@dataclass
class Digest:
    title: str
    header: str
    sections: list[Section]
    stats: dict[str, int]
    post_ids: list[int] = field(default_factory=list)
    id: int | None = None

    def messages(self) -> list[str]:
        """Шапка + по сообщению на канал (длинные каналы без LLM — в несколько сообщений)."""
        result = [self.header]
        for section in self.sections:
            result.extend(section.parts)
        return result

    # совместимость со старым API
    def chunks(self) -> list[str]:
        return self.messages()

    @property
    def html(self) -> str:
        return "\n\n".join(self.messages()).strip()


class DigestBuilder:
    def __init__(self, cfg: Config, db: Database, processor: Processor):
        self.cfg = cfg
        self.db = db
        self.processor = processor
        # результат сжатия канала переиспользуется для всех, у кого те же посты
        self._compress_cache: dict[tuple, list[tuple[int, str]]] = {}
        self._compress_locks: dict[tuple, asyncio.Lock] = {}

    async def _compressed_keep(self, channel: Channel, posts: list[Post], budget: int) -> list[tuple[int, str]]:
        key = (channel.id, tuple(p.id for p in posts))
        lock = self._compress_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key not in self._compress_cache:
                if len(self._compress_cache) > 500:
                    self._compress_cache.clear()
                    self._compress_locks = {key: lock}
                self._compress_cache[key] = await self.processor.compress_channel(channel, posts, budget)
            return self._compress_cache[key]

    # ---------- секции каналов ----------

    def _render_full(self, section: Section) -> list[str]:
        channel = section.channel
        header = channel_header(channel)
        tags = channel_tags(channel)
        blocks = [render_post(p, self.cfg) for p in section.posts]
        text = header + "\n\n" + "\n\n".join(blocks) + tags
        if visible_len(text) <= MESSAGE_LIMIT:
            return [text]
        # не влезает и сжать нечем — делим на части с повтором заголовка, теги — в последней
        cont = channel_header(channel, continued=True)
        budget = MESSAGE_LIMIT - visible_len(cont) - visible_len(tags) - 2
        chunks = split_blocks(blocks, budget, sep="\n\n")
        parts = [(header if i == 0 else cont) + "\n\n" + c for i, c in enumerate(chunks)]
        parts[-1] += tags
        return parts

    async def _compress(self, section: Section) -> list[str] | None:
        """Отбор и сжатие постов канала нейросетью, чтобы всё влезло в одно сообщение."""
        channel, posts = section.channel, section.posts
        header = channel_header(channel)
        tags = channel_tags(channel)
        footer_reserve = 120
        budget = MESSAGE_LIMIT - visible_len(header) - visible_len(tags) - footer_reserve
        # на каждую строку уходит ~12 символов на «▫️ 09:15 » и перевод строки
        text_budget = max(400, int((budget - 12 * min(len(posts), 25)) * 0.85))

        try:
            keep = await self._compressed_keep(channel, posts, text_budget)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось сжать %s: %s", channel.ref, exc)
            return None
        if not keep:
            return None

        # keep — в порядке важности; гарантируем лимит, выкидывая наименее важное
        by_id = {p.id: p for p in posts}
        chosen: list[tuple[Post, str]] = [(by_id[i], t) for i, t in keep if i in by_id]
        while chosen:
            lines = [render_compact(p, t, self.cfg) for p, t in sorted(chosen, key=lambda x: x[0].posted_at)]
            dropped = len(posts) - len(chosen)
            footer = ""
            if dropped:
                footer = (
                    f"\n\n<i>…и ещё {dropped} {plural(dropped, 'пост', 'поста', 'постов')} "
                    f'поменьше — <a href="{esc_attr(channel.link)}">в канале</a></i>'
                )
            text = header + "\n\n" + "\n".join(lines) + footer + tags
            if visible_len(text) <= MESSAGE_LIMIT:
                section.compressed = True
                section.dropped = dropped
                return [text]
            chosen.pop()  # последний — наименее важный
        return None

    async def _build_section(self, section: Section, can_compress: bool) -> None:
        parts = self._render_full(section)
        if len(parts) > 1 and can_compress:
            compressed = await self._compress(section)
            if compressed:
                section.parts = compressed
                return
        section.parts = parts

    # ---------- сборка ----------

    def _title(self, now: datetime, multi_daily: bool) -> str:
        local = now.astimezone(self.cfg.tz)
        if multi_daily:
            return f"Дайджест · {human_date(local)}, {local.strftime('%H:%M')}"
        return f"Дайджест за {human_date(local)}"

    async def build(
        self, user_id: int, tag: str | None = None, save: bool = True, multi_daily: bool = False
    ) -> Digest | None:
        """Дайджест пользователя по его подпискам. save=False — предпросмотр."""
        await self.processor.process_pending(final=save)
        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=self.cfg.max_post_age_hours)
        pending = await self.db.pending_for_user(user_id, since, self.cfg.first_fetch_hours)

        entries: list[tuple[Post, Channel]] = []
        ads: list[Post] = []
        cursors: dict[int, int] = {}
        for post, channel in pending:
            if post.status == "new":
                # ещё не обработан нейросетью (только в предпросмотре) — упрощённо, без сохранения
                self.processor.apply_fallback(post)
            if tag and tag not in channel.tags:
                continue
            cursors[channel.id] = max(cursors.get(channel.id, 0), post.msg_id)
            if post.is_ad and self.cfg.skip_ads:
                ads.append(post)
                continue
            entries.append((post, channel))

        if not entries and not self.cfg.send_empty_digest:
            if save and cursors:
                # одна реклама — сдвинем курсор, чтобы не висела в очереди
                await self.db.advance_cursors(user_id, cursors)
            return None

        entries.sort(key=sort_key)
        sections = [
            Section(channel=items[0][1], posts=[p for p, _ in items])
            for items in (list(g) for _, g in groupby(entries, key=lambda e: e[1].id))
        ]

        llm = self.processor.llm
        can_compress = bool(llm) and not getattr(llm, "paused", False)
        overview_task = None
        if self.cfg.digest_overview:
            overview_task = asyncio.create_task(self.processor.overview(entries))
        await asyncio.gather(*(self._build_section(s, can_compress) for s in sections))
        overview = await overview_task if overview_task else ""

        title = self._title(now, multi_daily)
        if tag:
            title += f" · #{tag}"
        stats = {
            "posts": len(entries),
            "channels": len(sections),
            "ads": len(ads),
            "summarized": sum(1 for p, _ in entries if p.summarized),
            "dropped": sum(s.dropped for s in sections),
        }

        header = f"📰 <b>{esc(title)}</b>\n"
        header += f"Постов: {stats['posts']} · каналов: {stats['channels']}"
        if stats["ads"]:
            header += f" · пропущено рекламы: {stats['ads']}"
        if overview:
            heading = "Главное" if multi_daily else "Главное за день"
            header += f"\n\n🧭 <b>{heading}</b>\n{esc(overview)}"
        if not entries:
            header += "\n\nНовых постов нет 🌿"
        if visible_len(header) > MESSAGE_LIMIT:
            header = split_blocks(header.split("\n"))[0]

        digest = Digest(
            title=title,
            header=header,
            sections=sections,
            stats=stats,
            post_ids=[p.id for p, _ in entries] + [p.id for p in ads],
        )
        if save:
            digest.id = await self.db.save_digest(user_id, title, digest.html, stats)
            await self.db.advance_cursors(user_id, cursors)
        return digest
