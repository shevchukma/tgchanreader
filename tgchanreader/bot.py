"""Telegram-бот: управление списком каналов и доставка дайджестов."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BotCommand, Message, TelegramObject

from .digest import Digest, esc, split_blocks
from .models import Channel, parse_tags
from .service import Service
from .sources import SourceError

log = logging.getLogger(__name__)

HELP = """<b>Бот-дайджест Telegram-каналов</b>

Каждый день в {times} ({tz}) я присылаю сводку новых постов: длинные сокращаю,
рекламу пропускаю, медиа-посты отмечаю ссылкой.

<b>Каналы</b>
/add @канал [теги] [приоритет] — добавить канал
   <code>/add @meduzalive политика,новости 1</code>
   <code>/add https://t.me/velo_ru велосипед 3</code>
/remove @канал — удалить
/list — список каналов
/tag @канал тег1,тег2 — задать теги (<code>-</code> — убрать)
/priority @канал 1 — задать приоритет (1 — самый важный)
/pause @канал, /resume @канал — приостановить / возобновить
/import — импортировать все подписки (только режим Telethon)

Можно просто переслать мне пост из канала — я добавлю канал.

<b>Дайджест</b>
/digest — собрать и прислать дайджест прямо сейчас
/preview [тег] — посмотреть, что попадёт в дайджест (без отметки прочитанным)
/status — статистика
"""


class AdminMiddleware(BaseMiddleware):
    def __init__(self, admin_ids: list[int]):
        self.admin_ids = set(admin_ids)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or user.id not in self.admin_ids:
            if isinstance(event, Message):
                if not self.admin_ids:
                    await event.answer(
                        f"Бот ещё не настроен. Ваш Telegram ID: <code>{user.id if user else '?'}</code>\n"
                        "Укажите его в переменной окружения <code>ADMIN_IDS</code> и перезапустите контейнер."
                    )
                else:
                    await event.answer("⛔ Это приватный бот.")
            return None
        return await handler(event, data)


def channel_line(ch: Channel) -> str:
    tags = " ".join(f"#{esc(t)}" for t in ch.tags) or "<i>без тегов</i>"
    status = "" if ch.active else " ⏸"
    return f'• <a href="{ch.link}">{esc(ch.display_name)}</a> — {tags}{status}'


def parse_add_args(args: str) -> tuple[str, list[str] | None, int | None]:
    parts = args.split()
    if not parts:
        raise ValueError("Укажите канал: /add @канал [теги] [приоритет]")
    ref = parts[0]
    tags: list[str] | None = None
    priority: int | None = None
    rest = parts[1:]
    if rest and rest[-1].isdigit():
        priority = int(rest.pop())
    if rest:
        tags = parse_tags(" ".join(rest))
    return ref, tags, priority


class TelegramBot:
    def __init__(self, service: Service):
        self.service = service
        cfg = service.cfg
        self.bot = Bot(
            cfg.bot_token,
            default=DefaultBotProperties(
                parse_mode=ParseMode.HTML,
                link_preview_is_disabled=True,
            ),
        )
        self.dp = Dispatcher()
        router = Router()
        router.message.middleware(AdminMiddleware(cfg.admin_ids))
        self._register(router)
        self.dp.include_router(router)
        service.senders.append(self.send_digest)

    # ---------- отправка ----------

    async def _send(self, chat_id: int, text: str) -> None:
        for attempt in range(5):
            try:
                await self.bot.send_message(chat_id, text)
                return
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
        log.error("Не удалось отправить сообщение в %s", chat_id)

    async def send_chunks(self, chat_id: int, chunks: list[str]) -> None:
        for chunk in chunks:
            await self._send(chat_id, chunk)
            await asyncio.sleep(0.5)

    async def send_digest(self, digest: Digest) -> None:
        chunks = digest.chunks()
        if digest.id and self.service.cfg.web_enabled:
            base = self.service.cfg.web_base_url
            chunks[-1] += f'\n\n🌐 <a href="{base}/digest/{digest.id}">Открыть в браузере</a>'
        for chat_id in self.service.cfg.recipients:
            await self.send_chunks(chat_id, chunks)

    # ---------- команды ----------

    def _register(self, r: Router) -> None:
        svc = self.service
        cfg = svc.cfg

        @r.message(CommandStart())
        @r.message(Command("help"))
        async def start(message: Message) -> None:
            times = ", ".join(t.strftime("%H:%M") for t in cfg.digest_times)
            await message.answer(HELP.format(times=times, tz=cfg.timezone))

        @r.message(Command("add"))
        async def add(message: Message, command: CommandObject) -> None:
            try:
                ref, tags, priority = parse_add_args(command.args or "")
                channel, created = await svc.add_channel(ref, tags, priority)
            except (ValueError, SourceError) as exc:
                await message.answer(f"⚠️ {esc(str(exc))}")
                return
            verb = "Добавлен" if created else "Обновлён"
            await message.answer(
                f"✅ {verb}: {channel_line(channel)}\nПриоритет: {channel.priority}"
            )

        @r.message(Command("remove", "del", "delete"))
        async def remove(message: Message, command: CommandObject) -> None:
            channel = await svc.find_channel(command.args or "")
            if not channel:
                await message.answer("Канал не найден. Список: /list")
                return
            await svc.db.remove_channel(channel.id)
            await message.answer(f"🗑 Удалён: {esc(channel.display_name)}")

        @r.message(Command("list", "channels"))
        async def list_channels(message: Message) -> None:
            channels = await svc.db.list_channels()
            if not channels:
                await message.answer("Каналов пока нет. Добавьте: /add @канал тег 1")
                return
            blocks: list[str] = [f"<b>Каналы ({len(channels)})</b>"]
            current = None
            for ch in channels:
                if ch.priority != current:
                    current = ch.priority
                    blocks.append(f"\n<b>Приоритет {ch.priority}</b>")
                blocks.append(channel_line(ch))
            await self.send_chunks(message.chat.id, split_blocks(blocks))

        @r.message(Command("tag", "tags"))
        async def tag(message: Message, command: CommandObject) -> None:
            parts = (command.args or "").split(maxsplit=1)
            if not parts:
                channels = await svc.db.list_channels()
                all_tags: dict[str, int] = {}
                for ch in channels:
                    for t in ch.tags:
                        all_tags[t] = all_tags.get(t, 0) + 1
                text = "\n".join(f"#{esc(t)} — {n}" for t, n in sorted(all_tags.items()))
                await message.answer(
                    (f"<b>Теги</b>\n{text}" if text else "Тегов пока нет")
                    + "\n\nЗадать: <code>/tag @канал тег1,тег2</code>"
                )
                return
            channel = await svc.find_channel(parts[0])
            if not channel:
                await message.answer("Канал не найден. Список: /list")
                return
            raw = parts[1] if len(parts) > 1 else "-"
            tags = [] if raw.strip() == "-" else parse_tags(raw)
            await svc.db.update_channel(channel.id, tags=tags)
            channel.tags = tags
            await message.answer(f"🏷 {channel_line(channel)}")

        @r.message(Command("priority", "prio"))
        async def priority(message: Message, command: CommandObject) -> None:
            parts = (command.args or "").split()
            if len(parts) != 2 or not parts[1].isdigit():
                await message.answer("Формат: <code>/priority @канал 1</code>")
                return
            channel = await svc.find_channel(parts[0])
            if not channel:
                await message.answer("Канал не найден. Список: /list")
                return
            await svc.db.update_channel(channel.id, priority=int(parts[1]))
            await message.answer(f"⭐ {esc(channel.display_name)}: приоритет {int(parts[1])}")

        async def _set_active(message: Message, command: CommandObject, active: bool) -> None:
            channel = await svc.find_channel(command.args or "")
            if not channel:
                await message.answer("Канал не найден. Список: /list")
                return
            await svc.db.update_channel(channel.id, active=active)
            await message.answer(
                f"{'▶️ Возобновлён' if active else '⏸ Приостановлен'}: {esc(channel.display_name)}"
            )

        @r.message(Command("pause"))
        async def pause(message: Message, command: CommandObject) -> None:
            await _set_active(message, command, False)

        @r.message(Command("resume"))
        async def resume(message: Message, command: CommandObject) -> None:
            await _set_active(message, command, True)

        @r.message(Command("import"))
        async def import_subs(message: Message) -> None:
            source = svc.source
            primary = getattr(source, "primary", None)
            if primary is None or not hasattr(primary, "subscribed_channels"):
                await message.answer(
                    "Импорт подписок работает только в режиме Telethon "
                    "(нужны TG_API_ID, TG_API_HASH, TG_SESSION)."
                )
                return
            infos = await primary.subscribed_channels()
            added = 0
            for info in infos:
                if await svc.db.get_channel(info.ref):
                    continue
                await svc.db.add_channel(info.ref, info.title, [], cfg.default_priority)
                added += 1
            await message.answer(
                f"📥 Найдено каналов: {len(infos)}, добавлено новых: {added}.\n"
                "Теги и приоритеты: /list, /tag, /priority"
            )

        @r.message(Command("digest", "now"))
        async def digest_now(message: Message) -> None:
            await message.answer("⏳ Собираю дайджест…")
            digest = await svc.make_and_send_digest()
            if digest is None:
                await message.answer("Новых постов нет 🌿")
            elif message.chat.id not in cfg.recipients:
                await self.send_chunks(message.chat.id, digest.chunks())

        @r.message(Command("preview"))
        async def preview(message: Message, command: CommandObject) -> None:
            await message.answer("⏳ Обновляю каналы…")
            await svc.collect()
            tag_arg = parse_tags(command.args or "")
            digest = await svc.builder.build(tag=tag_arg[0] if tag_arg else None, save=False)
            if digest is None:
                await message.answer("Пока нечего показать 🌿")
                return
            await self.send_chunks(message.chat.id, digest.chunks())

        @r.message(Command("status"))
        async def status(message: Message) -> None:
            st = await svc.db.stats()
            nxt = svc.next_digest_time().strftime("%d.%m %H:%M")
            llm = f"{cfg.llm.provider} / {cfg.llm.model}" if svc.llm else "выключен"
            await message.answer(
                "<b>Статус</b>\n"
                f"Каналов: {st['channels']} (активных {st['active_channels']})\n"
                f"Ждут дайджеста: {st['posts_pending']} · не обработано: {st['posts_new']}\n"
                f"Отфильтровано рекламы всего: {st['ads_total']}\n"
                f"Дайджестов отправлено: {st['digests']}\n"
                f"Источник: {cfg.effective_source} · LLM: {esc(llm)}\n"
                f"Следующий дайджест: {nxt} ({cfg.timezone})"
            )

        @r.message(F.forward_origin)
        async def forwarded(message: Message) -> None:
            origin = message.forward_origin
            chat = getattr(origin, "chat", None)
            if chat is None or chat.type != "channel":
                await message.answer("Перешлите пост именно из канала, чтобы добавить его.")
                return
            ref = chat.username or str(chat.id)
            try:
                channel, created = await svc.add_channel(ref)
            except (ValueError, SourceError) as exc:
                await message.answer(f"⚠️ {esc(str(exc))}")
                return
            if created:
                await message.answer(
                    f"✅ Добавлен: {channel_line(channel)}\n"
                    f"Приоритет: {channel.priority}. Теги: <code>/tag {esc(ref)} тег1,тег2</code>"
                )
            else:
                await message.answer(f"Канал уже отслеживается: {channel_line(channel)}")

    async def run(self) -> None:
        await self.bot.set_my_commands(
            [
                BotCommand(command="digest", description="Дайджест сейчас"),
                BotCommand(command="preview", description="Предпросмотр дайджеста"),
                BotCommand(command="list", description="Список каналов"),
                BotCommand(command="add", description="Добавить канал"),
                BotCommand(command="remove", description="Удалить канал"),
                BotCommand(command="tag", description="Теги канала"),
                BotCommand(command="priority", description="Приоритет канала"),
                BotCommand(command="status", description="Статус"),
                BotCommand(command="help", description="Помощь"),
            ]
        )
        await self.dp.start_polling(self.bot, handle_signals=False)

    async def close(self) -> None:
        await self.bot.session.close()
