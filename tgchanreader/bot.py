"""Telegram-бот: управление списком каналов и доставка дайджестов."""

from __future__ import annotations

import asyncio
import html
import inspect
import logging
import re
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BotCommand, Message, TelegramObject

from .digest import Digest, esc, split_blocks
from .llm import LLMError
from .models import Channel, parse_tags
from .service import Service
from .sources import SourceError

log = logging.getLogger(__name__)

HELP_USER = """<b>Бот-дайджест Telegram-каналов</b>

В {times} ({tz}) я присылаю сводку новых постов из отслеживаемых каналов:
каждый канал отдельным сообщением, длинные посты сокращены, реклама пропущена.

/start — подписаться на дайджест
/stop — отписаться
/list — какие каналы отслеживаются
/preview [тег] — что попадёт в следующий дайджест
/status — статистика
"""

HELP_ADMIN = HELP_USER + """
<b>Управление (только администраторы)</b>
/add @канал [теги] [приоритет] — добавить канал
   <code>/add @example_news новости,город 1</code>
   <code>/add https://t.me/example_velo велосипед 3</code>
/remove @канал — удалить
/tag @канал тег1,тег2 — задать теги (<code>-</code> — убрать), /tag — все теги
/priority @канал 1 — задать приоритет (1 — самый важный)
/pause @канал, /resume @канал — приостановить / возобновить
/import — импортировать все подписки (только режим Telethon)
/digest — собрать и разослать дайджест прямо сейчас
/subscribers — кто получает дайджест
/llmtest — проверить подключение к нейросети
/reprocess — заново обработать нейросетью посты, ещё не попавшие в дайджест

Можно просто переслать мне пост из канала — я добавлю канал.
"""


class AccessMiddleware(BaseMiddleware):
    """Пускает только пользователей из ADMIN_IDS и ALLOWED_IDS."""

    def __init__(self, cfg):  # type: ignore[no-untyped-def]
        self.cfg = cfg

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        allowed = self.cfg.access_ids
        if user is None or user.id not in allowed:
            if isinstance(event, Message) and user is not None:
                log.info("Отказано в доступе: id=%s @%s", user.id, user.username)
                if not allowed:
                    await event.answer(
                        f"Бот ещё не настроен. Ваш Telegram ID: <code>{user.id}</code>\n"
                        "Укажите его в переменной окружения <code>ADMIN_IDS</code> и перезапустите контейнер."
                    )
                else:
                    await event.answer(
                        "⛔ Это приватный бот.\n"
                        f"Ваш Telegram ID: <code>{user.id}</code> — передайте его владельцу, "
                        "если хотите получать дайджест."
                    )
            return None
        data["is_admin"] = user.id in self.cfg.admin_ids
        return await handler(event, data)


def admin_only(fn):  # type: ignore[no-untyped-def]
    """Декоратор: команда доступна только ADMIN_IDS."""
    params = set(inspect.signature(fn).parameters)

    async def wrapper(message: Message, **kwargs: Any) -> Any:
        if not kwargs.get("is_admin"):
            await message.answer("🔒 Эта команда доступна только администратору.")
            return None
        return await fn(message, **{k: v for k, v in kwargs.items() if k in params})

    wrapper.__name__ = fn.__name__
    return wrapper


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
        router.message.middleware(AccessMiddleware(cfg))
        self._register(router)
        self.dp.include_router(router)
        service.senders.append(self.send_digest)

    # ---------- отправка ----------

    async def _send(self, chat_id: int, text: str, silent: bool = False) -> bool:
        """Отправляет сообщение; False — если получатель недоступен (заблокировал бота)."""
        for _ in range(5):
            try:
                await self.bot.send_message(chat_id, text, disable_notification=silent)
                return True
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
            except TelegramForbiddenError:
                log.warning("Чат %s недоступен (бот заблокирован?) — отписываем", chat_id)
                await self.service.db.unsubscribe(chat_id)
                return False
            except TelegramBadRequest as exc:
                # например, сломанная разметка — отправим текстом без форматирования
                log.error("Telegram отклонил сообщение в %s: %s", chat_id, exc)
                plain = html.unescape(re.sub(r"<[^>]+>", "", text))[:4096]
                try:
                    await self.bot.send_message(
                        chat_id, plain, parse_mode=None, disable_notification=silent
                    )
                    return True
                except Exception:  # noqa: BLE001
                    log.exception("Не удалось отправить и простым текстом")
                    return True
        log.error("Не удалось отправить сообщение в %s", chat_id)
        return True

    async def send_chunks(self, chat_id: int, chunks: list[str]) -> None:
        """Первое сообщение — со звуком, остальные — без уведомления."""
        for i, chunk in enumerate(chunks):
            if not await self._send(chat_id, chunk, silent=i > 0):
                return
            await asyncio.sleep(0.4)

    async def recipients(self) -> list[int]:
        """Подписчики, которым разрешён доступ в .env, плюс DIGEST_CHAT_IDS."""
        cfg = self.service.cfg
        allowed = cfg.access_ids
        result: list[int] = []
        for sub in await self.service.db.list_subscribers():
            if sub["user_id"] in allowed and sub["chat_id"] not in result:
                result.append(sub["chat_id"])
        for chat_id in cfg.digest_chat_ids:
            if chat_id not in result:
                result.append(chat_id)
        return result

    def digest_messages(self, digest: Digest) -> list[str]:
        messages = digest.messages()
        cfg = self.service.cfg
        if digest.id and cfg.web_enabled:
            link = f"{cfg.web_base_url}/digest/{digest.id}"
            if cfg.web_token:
                link += f"?token={cfg.web_token}"
            messages[0] += f'\n\n🌐 <a href="{html.escape(link)}">Открыть в браузере</a>'
        return messages

    async def send_digest(self, digest: Digest) -> None:
        messages = self.digest_messages(digest)
        recipients = await self.recipients()
        log.info("Отправка дайджеста: %d сообщений × %d получателей", len(messages), len(recipients))
        for chat_id in recipients:
            await self.send_chunks(chat_id, messages)

    # ---------- команды ----------

    def _register(self, r: Router) -> None:
        svc = self.service
        cfg = svc.cfg

        def help_text(is_admin: bool) -> str:
            times = ", ".join(t.strftime("%H:%M") for t in cfg.digest_times)
            return (HELP_ADMIN if is_admin else HELP_USER).format(times=times, tz=cfg.timezone)

        @r.message(CommandStart())
        async def start(message: Message, is_admin: bool) -> None:
            user = message.from_user
            assert user
            name = user.full_name + (f" (@{user.username})" if user.username else "")
            new = await svc.db.subscribe(user.id, message.chat.id, name)
            if new:
                log.info("Новый подписчик: %s id=%s", name, user.id)
                head = "✅ Вы подписаны на дайджест."
            else:
                head = "Вы уже подписаны на дайджест."
            await message.answer(f"{head}\n\n{help_text(is_admin)}")

        @r.message(Command("help"))
        async def help_cmd(message: Message, is_admin: bool) -> None:
            await message.answer(help_text(is_admin))

        @r.message(Command("stop", "unsubscribe"))
        async def stop(message: Message) -> None:
            assert message.from_user
            await svc.db.unsubscribe(message.from_user.id)
            await message.answer("🔕 Вы отписались от дайджеста. Подписаться снова: /start")

        @r.message(Command("subscribers", "users"))
        @admin_only
        async def subscribers(message: Message) -> None:
            subs = {s["user_id"]: s for s in await svc.db.list_subscribers(active_only=False)}
            lines = ["<b>Получатели дайджеста</b>"]
            for uid in sorted(cfg.access_ids):
                sub = subs.get(uid)
                role = "админ" if uid in cfg.admin_ids else "друг"
                name = esc(sub["name"]) if sub and sub["name"] else f"<code>{uid}</code>"
                if sub and sub["active"]:
                    state = "✅ подписан"
                elif sub:
                    state = "🔕 отписался"
                else:
                    state = "⏳ ещё не нажал /start"
                lines.append(f"• {name} — {role}, {state}")
            others = [s for uid, s in subs.items() if uid not in cfg.access_ids and s["active"]]
            if others:
                lines.append(f"\nНе получают (нет в .env): {len(others)}")
            for chat_id in cfg.digest_chat_ids:
                lines.append(f"• чат <code>{chat_id}</code> (DIGEST_CHAT_IDS)")
            lines.append("\nДобавить друга: его ID в <code>ALLOWED_IDS</code> в .env и перезапуск.")
            await message.answer("\n".join(lines))

        @r.message(Command("add"))
        @admin_only
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
        @admin_only
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
        @admin_only
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
        @admin_only
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
        @admin_only
        async def pause(message: Message, command: CommandObject) -> None:
            await _set_active(message, command, False)

        @r.message(Command("resume"))
        @admin_only
        async def resume(message: Message, command: CommandObject) -> None:
            await _set_active(message, command, True)

        @r.message(Command("import"))
        @admin_only
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
        @admin_only
        async def digest_now(message: Message) -> None:
            await message.answer("⏳ Собираю дайджест…")
            digest = await svc.make_and_send_digest()
            if digest is None:
                await message.answer("Новых постов нет 🌿")
            elif message.chat.id not in await self.recipients():
                await self.send_chunks(message.chat.id, self.digest_messages(digest))

        @r.message(Command("preview"))
        async def preview(message: Message, command: CommandObject) -> None:
            await message.answer("⏳ Обновляю каналы…")
            await svc.collect()
            tag_arg = parse_tags(command.args or "")
            digest = await svc.builder.build(tag=tag_arg[0] if tag_arg else None, save=False)
            if digest is None:
                await message.answer("Пока нечего показать 🌿")
                return
            await self.send_chunks(message.chat.id, digest.messages())

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
                + (f"⚠️ Ошибка LLM: <code>{esc(svc.llm.last_error[:300])}</code>\n"
                   if svc.llm and svc.llm.last_error else "")
                + f"Следующий дайджест: {nxt} ({cfg.timezone})"
            )

        @r.message(Command("llmtest"))
        @admin_only
        async def llmtest(message: Message) -> None:
            if not svc.llm:
                await message.answer("LLM выключен (LLM_PROVIDER=none или не задан LLM_API_KEY).")
                return
            svc.llm.reset()
            try:
                answer = await svc.llm.complete(
                    "Отвечай одним словом.", "Напиши слово «работает».", max_tokens=20
                )
            except LLMError as exc:
                await message.answer(
                    f"❌ {esc(cfg.llm.provider)} / <code>{esc(cfg.llm.model)}</code>\n"
                    f"<code>{esc(str(exc)[:800])}</code>"
                )
                return
            await message.answer(
                f"✅ {esc(cfg.llm.provider)} / <code>{esc(cfg.llm.model)}</code> отвечает: "
                f"{esc(answer.strip()[:100])}\nЧтобы переобработать посты: /reprocess"
            )

        @r.message(Command("reprocess"))
        @admin_only
        async def reprocess(message: Message) -> None:
            if not svc.llm:
                await message.answer("LLM выключен — переобрабатывать нечем.")
                return
            svc.llm.reset()
            count = await svc.db.reset_undigested()
            await message.answer(f"⏳ Переобрабатываю постов: {count}…")
            stats = await svc.processor.process_pending()
            text = (
                f"✅ Готово: обработано {stats.processed}, реклама {stats.ads}, "
                f"сокращено {stats.summarized}"
            )
            if stats.deferred:
                text += (
                    f"\n⚠️ Отложено {stats.deferred}: LLM не отвечает "
                    f"(<code>{esc(svc.llm.last_error[:300])}</code>)"
                )
            await message.answer(text)

        @r.message(F.forward_origin)
        @admin_only
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
        # админы получают дайджест без /start (если сами не отписались)
        for admin_id in self.service.cfg.admin_ids:
            await self.service.db.ensure_subscriber(admin_id)
        await self.bot.set_my_commands(
            [
                BotCommand(command="start", description="Подписаться на дайджест"),
                BotCommand(command="stop", description="Отписаться"),
                BotCommand(command="digest", description="Дайджест сейчас"),
                BotCommand(command="preview", description="Предпросмотр дайджеста"),
                BotCommand(command="list", description="Список каналов"),
                BotCommand(command="add", description="Добавить канал"),
                BotCommand(command="remove", description="Удалить канал"),
                BotCommand(command="tag", description="Теги канала"),
                BotCommand(command="priority", description="Приоритет канала"),
                BotCommand(command="status", description="Статус"),
                BotCommand(command="subscribers", description="Получатели дайджеста"),
                BotCommand(command="llmtest", description="Проверить нейросеть"),
                BotCommand(command="reprocess", description="Переобработать посты нейросетью"),
                BotCommand(command="help", description="Помощь"),
            ]
        )
        await self.dp.start_polling(self.bot, handle_signals=False)

    async def close(self) -> None:
        await self.bot.session.close()
