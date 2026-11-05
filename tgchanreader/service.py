"""Основной сервис: сбор постов, расписание и рассылка дайджестов.

Каналы читаются и обрабатываются нейросетью один раз, сколько бы пользователей
на них ни было подписано. Дайджест собирается для каждого пользователя из его
подписок, с его тегами и приоритетами.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any, Awaitable, Callable

from .config import DIGEST_SLOTS, Config, normalize_schedule
from .db import Database
from .digest import Digest, DigestBuilder
from .llm import LLMClient, build_llm
from .models import Channel, normalize_ref, parse_tags
from .cleanup import parse_rules, strip_lines
from .processor import Processor, strip_disclaimers
from .sources import Source, SourceError, build_source

log = logging.getLogger(__name__)

# sender(user, digest): user — словарь из db.list_users()
DigestSender = Callable[[dict[str, Any], Digest], Awaitable[None]]


class LimitError(ValueError):
    pass


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
        self._bg: set[asyncio.Task] = set()
        self.strip_rules = parse_rules(cfg.strip_lines)
        if self.strip_rules:
            log.info("STRIP_LINES: правил %d", len(self.strip_rules))

    @property
    def owner_id(self) -> int:
        """Владелец: первый из ADMIN_IDS (0 — режим без бота)."""
        return self.cfg.admin_ids[0] if self.cfg.admin_ids else 0

    async def start(self) -> None:
        await self.db.connect()
        await self.source.start()
        for admin_id in self.cfg.admin_ids or [self.owner_id]:
            await self.db.ensure_user(admin_id)
        saved = await self.db.get_setting("llm_instructions", "")
        if saved:
            self.processor.instructions = "" if saved == "-" else saved
        migrated = await self.db.migrate_shared_channels(self.owner_id)
        if migrated:
            log.info("Общий список каналов (%d) перенесён пользователю %s", migrated, self.owner_id)
        if self.cfg.channels_file:
            await self.sync_channels_file(self.cfg.channels_file, self.owner_id)

    async def close(self) -> None:
        for task in [*self._tasks, *self._bg]:
            task.cancel()
        await asyncio.gather(*self._tasks, *self._bg, return_exceptions=True)
        await self.source.close()
        if self.llm:
            await self.llm.close()
        await self.db.close()

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.cfg.admin_ids or (not self.cfg.admin_ids and user_id == self.owner_id)

    # ---------- каналы пользователя ----------

    async def add_channel(
        self,
        user_id: int,
        raw_ref: str,
        tags: list[str] | None = None,
        priority: int | None = None,
    ) -> tuple[Channel, bool]:
        """Подписывает пользователя на канал (или обновляет теги/приоритет)."""
        ref = normalize_ref(raw_ref)
        if not ref:
            raise ValueError(f"Не понимаю, что за канал: {raw_ref}")
        await self.db.ensure_user(user_id)

        existing = await self.db.user_channel(user_id, ref)
        if existing:
            fields: dict = {}
            if tags is not None:
                fields["tags"] = tags
            if priority is not None:
                fields["priority"] = priority
            if fields:
                await self.db.update_subscription(user_id, existing.id, **fields)
            channel = await self.db.user_channel(user_id, ref)
            assert channel
            return channel, False

        limit = self.cfg.max_channels_per_user
        if limit and not self.is_admin(user_id):
            if len(await self.db.user_channels(user_id)) >= limit:
                raise LimitError(f"Достигнут лимит: {limit} каналов на пользователя")

        channel = await self.db.get_channel(ref)
        if channel is None:
            info = await self.source.resolve(ref)
            channel = await self.db.get_or_create_channel(info.ref, info.title)
        sub, created = await self.db.add_subscription(
            user_id, channel, tags or [], priority or self.cfg.default_priority
        )
        if channel.last_post_id == 0:
            # новый для сервиса канал — сразу подтягиваем посты, не дожидаясь цикла сбора
            task = asyncio.create_task(self._collect_new(channel))
            self._bg.add(task)
            task.add_done_callback(self._bg.discard)
        return sub, created

    async def _collect_new(self, channel: Channel) -> None:
        try:
            async with self._collect_lock:
                await self._collect_one(channel, datetime.now(timezone.utc))
            await self.processor.process_pending()
        except Exception:  # noqa: BLE001
            log.exception("Ошибка первичного чтения %s", channel.ref)

    async def find_channel(self, user_id: int, raw_ref: str) -> Channel | None:
        ref = normalize_ref(raw_ref)
        if not ref:
            return None
        return await self.db.user_channel(user_id, ref)

    async def remove_channel(self, user_id: int, channel: Channel) -> None:
        await self.db.remove_subscription(user_id, channel.id)

    async def sync_channels_file(self, path: str, user_id: int) -> int:
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
                await self.add_channel(user_id, ref, tags, priority)
                count += 1
            except (ValueError, SourceError) as exc:
                log.warning("channels.yml: пропускаем %s: %s", ref, exc)
        log.info("Синхронизировано каналов из %s: %d", path, count)
        return count

    async def set_instructions(self, text: str | None) -> str:
        """Указания нейросети из бота. None — вернуться к LLM_INSTRUCTIONS из .env,
        пустая строка — без указаний (перекрывает .env)."""
        if text is None:
            await self.db.set_setting("llm_instructions", "")
            self.processor.instructions = self.cfg.llm_instructions
        else:
            text = text.strip()
            await self.db.set_setting("llm_instructions", text or "-")
            self.processor.instructions = text
        return self.processor.instructions

    # ---------- сбор (общий для всех пользователей) ----------

    async def _collect_one(self, channel: Channel, now: datetime) -> int:
        if not self.source.supports(channel):
            log.warning("Источник %s не поддерживает канал %s", self.source.name, channel.ref)
            return 0
        hours = (
            self.cfg.first_fetch_hours if channel.last_post_id == 0 else self.cfg.max_post_age_hours
        )
        try:
            posts = await self.source.fetch(channel, now - timedelta(hours=hours))
        except SourceError as exc:
            log.warning("Не удалось прочитать %s: %s", channel.ref, exc)
            return 0
        if self.cfg.strip_disclaimers or self.strip_rules:
            for post in posts:
                if self.cfg.strip_disclaimers:
                    post.text = strip_disclaimers(post.text)
                post.text = strip_lines(post.text, self.strip_rules, channel.ref)
            posts = [p for p in posts if p.text or p.media]
        added = await self.db.add_posts(channel, posts)
        if added:
            log.info("%s: новых постов %d", channel.ref, added)
        return added

    async def collect(self) -> int:
        async with self._collect_lock:
            now = datetime.now(timezone.utc)
            total = 0
            for channel in await self.db.channels_to_collect():
                try:
                    total += await self._collect_one(channel, now)
                except Exception:  # noqa: BLE001
                    log.exception("Ошибка при чтении %s", channel.ref)
            await self.db.cleanup(now - timedelta(days=30))
        try:
            await self.processor.process_pending()
        except Exception:  # noqa: BLE001
            log.exception("Ошибка обработки постов")
        return total

    # ---------- дайджесты ----------

    def schedule_of(self, user: dict[str, Any] | None) -> list[int]:
        """Часы рассылки пользователя (его настройка или DIGEST_TIMES по умолчанию)."""
        hours = normalize_schedule(user["schedule"]) if user else []
        return hours or self.cfg.default_schedule

    async def set_schedule(self, user_id: int, hours: list[int]) -> list[int]:
        hours = normalize_schedule(hours)
        if not hours:
            raise ValueError("Нужен хотя бы один раз в сутки")
        await self.db.ensure_user(user_id)
        await self.db.set_schedule(user_id, hours)
        return hours

    async def digest_users(self, slot: int | None = None) -> list[dict[str, Any]]:
        """Кому слать дайджест: подписанные пользователи с доступом по .env
        (и, если задан slot, у кого этот час есть в расписании)."""
        allowed = set(self.cfg.access_ids)
        if not self.cfg.bot_token:
            allowed.add(self.owner_id)  # режим только RSS — дайджест владельца
        users = [u for u in await self.db.list_users() if u["user_id"] in allowed]
        if slot is not None:
            users = [u for u in users if slot in self.schedule_of(u)]
        return users

    async def make_and_send_digest(
        self, user_id: int | None = None, slot: int | None = None
    ) -> dict[int, Digest]:
        """Собирает и рассылает дайджесты: одному пользователю, всем в слоте или всем."""
        async with self._digest_lock:
            users = await self.digest_users(slot)
            if user_id is not None:
                users = [u for u in users if u["user_id"] == user_id]
            if not users:
                return {}
            await self.collect()
            await self.processor.process_pending(final=True)
            result: dict[int, Digest] = {}
            for user in users:
                try:
                    multi = len(self.schedule_of(user)) > 1
                    digest = await self.builder.build(user["user_id"], multi_daily=multi)
                except Exception:  # noqa: BLE001
                    log.exception("Ошибка сборки дайджеста для %s", user["user_id"])
                    continue
                if digest is None:
                    continue
                result[user["user_id"]] = digest
                for sender in self.senders:
                    try:
                        await sender(user, digest)
                    except Exception:  # noqa: BLE001
                        log.exception("Ошибка отправки дайджеста %s", user["user_id"])
            log.info("Дайджесты: %d из %d пользователей", len(result), len(users))
            return result

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

    def next_slot(self, hours: list[int] | tuple[int, ...] = DIGEST_SLOTS,
                  now: datetime | None = None) -> datetime:
        """Ближайшее время рассылки из списка часов (по TZ)."""
        tz = self.cfg.tz
        now = (now or datetime.now(timezone.utc)).astimezone(tz)
        candidates = []
        for day in (0, 1):
            date = (now + timedelta(days=day)).date()
            for hour in hours:
                dt = datetime.combine(date, time(hour), tzinfo=tz)
                if dt > now:
                    candidates.append(dt)
        return min(candidates)

    def next_digest_time(self, user: dict[str, Any] | None = None,
                         now: datetime | None = None) -> datetime:
        return self.next_slot(self.schedule_of(user), now)

    async def _digest_loop(self) -> None:
        while True:
            target = self.next_slot()
            log.info("Следующий слот рассылки: %s", target.isoformat())
            while True:
                delay = (target - datetime.now(timezone.utc)).total_seconds()
                if delay <= 0:
                    break
                await asyncio.sleep(min(delay, 300))
            try:
                await self.make_and_send_digest(slot=target.hour)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка формирования дайджеста")
            await asyncio.sleep(1)
