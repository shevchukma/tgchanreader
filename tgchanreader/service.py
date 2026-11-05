"""Основной сервис: сбор постов, расписание дайджестов."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from .config import Config
from .db import Database
from .digest import Digest, DigestBuilder
from .llm import LLMClient, build_llm
from .models import Channel, normalize_ref, parse_tags
from .processor import Processor
from .sources import Source, SourceError, build_source

log = logging.getLogger(__name__)

DigestSender = Callable[[Digest], Awaitable[None]]


class Service:
    def __init__(self, cfg: Config, db: Database | None = None, source: Source | None = None,
                 llm: LLMClient | None | bool = False):
        self.cfg = cfg
        self.db = db or Database(cfg.db_path)
        self.source = source or build_source(cfg)
        self.llm = build_llm(cfg.llm) if llm is False else (llm or None)
        self.processor = Processor(cfg, self.db, self.llm)
        self.builder = DigestBuilder(cfg, self.db, self.processor)
        self.senders: list[DigestSender] = []
        self._collect_lock = asyncio.Lock()
        self._digest_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        await self.db.connect()
        await self.source.start()
        if self.cfg.channels_file:
            await self.sync_channels_file(self.cfg.channels_file)

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.source.close()
        if self.llm:
            await self.llm.close()
        await self.db.close()

    # ---------- управление каналами ----------

    async def add_channel(
        self, raw_ref: str, tags: list[str] | None = None, priority: int | None = None
    ) -> tuple[Channel, bool]:
        ref = normalize_ref(raw_ref)
        if not ref:
            raise ValueError(f"Не понимаю, что за канал: {raw_ref}")
        existing = await self.db.get_channel(ref)
        if existing:
            fields: dict = {}
            if tags is not None:
                fields["tags"] = tags
            if priority is not None:
                fields["priority"] = priority
            if fields:
                await self.db.update_channel(existing.id, **fields)
            channel = await self.db.get_channel(ref)
            assert channel
            return channel, False
        info = await self.source.resolve(ref)
        return await self.db.add_channel(
            info.ref, info.title, tags or [], priority or self.cfg.default_priority
        )

    async def find_channel(self, raw_ref: str) -> Channel | None:
        ref = normalize_ref(raw_ref)
        if not ref:
            return None
        return await self.db.get_channel(ref)

    async def sync_channels_file(self, path: str) -> int:
        import yaml

        try:
            with open(path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except FileNotFoundError:
            log.warning("Файл каналов %s не найден", path)
            return 0
        items = data.get("channels", []) if isinstance(data, dict) else data
        count = 0
        for item in items or []:
            if isinstance(item, str):
                ref, tags, priority = item, None, None
            elif isinstance(item, dict):
                ref = str(item.get("name") or item.get("channel") or "")
                raw_tags = item.get("tags")
                if isinstance(raw_tags, list):
                    tags = parse_tags(",".join(map(str, raw_tags)))
                elif raw_tags:
                    tags = parse_tags(str(raw_tags))
                else:
                    tags = None
                priority = int(item["priority"]) if item.get("priority") is not None else None
            else:
                continue
            try:
                await self.add_channel(ref, tags, priority)
                count += 1
            except (ValueError, SourceError) as exc:
                log.warning("channels.yml: пропускаем %s: %s", ref, exc)
        log.info("Синхронизировано каналов из %s: %d", path, count)
        return count

    # ---------- сбор ----------

    async def collect(self) -> int:
        async with self._collect_lock:
            now = datetime.now(timezone.utc)
            total = 0
            for channel in await self.db.list_channels(active_only=True):
                if not self.source.supports(channel):
                    log.warning("Источник %s не поддерживает канал %s", self.source.name, channel.ref)
                    continue
                hours = (
                    self.cfg.first_fetch_hours
                    if channel.last_post_id == 0
                    else self.cfg.max_post_age_hours
                )
                since = now - timedelta(hours=hours)
                try:
                    posts = await self.source.fetch(channel, since)
                except SourceError as exc:
                    log.warning("Не удалось прочитать %s: %s", channel.ref, exc)
                    continue
                except Exception:  # noqa: BLE001
                    log.exception("Ошибка при чтении %s", channel.ref)
                    continue
                added = await self.db.add_posts(channel, posts)
                total += added
                if added:
                    log.info("%s: новых постов %d", channel.ref, added)
            await self.db.cleanup(now - timedelta(days=30))
        try:
            await self.processor.process_pending()
        except Exception:  # noqa: BLE001
            log.exception("Ошибка обработки постов")
        return total

    # ---------- дайджест ----------

    async def make_and_send_digest(self) -> Digest | None:
        async with self._digest_lock:
            await self.collect()
            digest = await self.builder.build()
            if digest is None:
                log.info("Новых постов для дайджеста нет")
                return None
            for sender in self.senders:
                try:
                    await sender(digest)
                except Exception:  # noqa: BLE001
                    log.exception("Ошибка отправки дайджеста")
            return digest

    # ---------- фоновые циклы ----------

    def run_background(self) -> None:
        self._tasks.append(asyncio.create_task(self._collect_loop(), name="collect"))
        self._tasks.append(asyncio.create_task(self._digest_loop(), name="digest"))

    async def _collect_loop(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                await self.collect()
            except Exception:  # noqa: BLE001
                log.exception("Ошибка цикла сбора")
            await asyncio.sleep(self.cfg.collect_interval_minutes * 60)

    def next_digest_time(self, now: datetime | None = None) -> datetime:
        tz = self.cfg.tz
        now = (now or datetime.now(timezone.utc)).astimezone(tz)
        candidates = []
        for day in (0, 1):
            date = (now + timedelta(days=day)).date()
            for t in self.cfg.digest_times:
                dt = datetime.combine(date, t, tzinfo=tz)
                if dt > now:
                    candidates.append(dt)
        return min(candidates)

    async def _digest_loop(self) -> None:
        while True:
            target = self.next_digest_time()
            log.info("Следующий дайджест: %s", target.isoformat())
            while True:
                delay = (target - datetime.now(timezone.utc)).total_seconds()
                if delay <= 0:
                    break
                await asyncio.sleep(min(delay, 300))
            try:
                await self.make_and_send_digest()
            except Exception:  # noqa: BLE001
                log.exception("Ошибка формирования дайджеста")
            await asyncio.sleep(1)
