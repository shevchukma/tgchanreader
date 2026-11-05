"""Telegram-бот: личные списки каналов и доставка дайджестов."""

from __future__ import annotations

import asyncio
import html
import inspect
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from .config import DIGEST_SLOTS
from .digest import Digest, esc, split_blocks
from .llm import LLMError
from .models import Channel, parse_tags
from .service import Service
from .sources import SourceError

log = logging.getLogger(__name__)

HELP_USER = """<b>Бот-дайджест Telegram-каналов</b>

В {times} ({tz}) я присылаю сводку новых постов из <b>ваших</b> каналов:
каждый канал отдельным сообщением, длинные посты сокращены, реклама пропущена.

<b>Мои каналы</b>
/add @канал [теги] [приоритет] — добавить канал
   <code>/add @example_news новости,город 1</code>
   <code>/add https://t.me/example_velo велосипед 3</code>
/remove @канал — удалить
/list — мои каналы
/tag @канал тег1,тег2 — задать теги (<code>-</code> — убрать), /tag — все мои теги
/priority @канал 1 — задать приоритет (1 — самый важный)
/pause @канал, /resume @канал — приостановить / возобновить
Можно просто переслать мне пост из канала — я его добавлю.

<b>Дайджест</b>
/digest — прислать дайджест прямо сейчас
/preview [тег] — что попадёт в следующий дайджест
/schedule — когда присылать дайджест (6, 9, 12, 15, 18, 21 ч)
/rss — личные ссылки на веб-архив и RSS
/status — статистика
/stop — не присылать дайджест, /start — снова присылать
"""

HELP_ADMIN = HELP_USER + """
<b>Администратор</b>
/users — пользователи и их подписки
/import — добавить себе все каналы аккаунта Telethon
/instructions — указания нейросети (например, «не добавляй дисклеймеры»)
/llmtest — проверить подключение к нейросети
/reprocess — заново обработать нейросетью посты за последние часы
"""


class AccessMiddleware(BaseMiddleware):
    """Пускает только пользователей из ADMIN_IDS и ALLOWED_IDS."""

    def __init__(self, service: Service):
        self.service = service

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        allowed = self.service.cfg.access_ids
        if user is None or user.id not in allowed:
            if isinstance(event, CallbackQuery):
                await event.answer("⛔ Нет доступа", show_alert=True)
                return None
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
        data["uid"] = user.id
        data["is_admin"] = self.service.is_admin(user.id)
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
    return f'• <a href="{esc(ch.link)}">{esc(ch.display_name)}</a> — {tags}{status}'


SCHEDULE_PRESETS = {
    "all": ("Каждые 3 часа", list(DIGEST_SLOTS)),
    "3x": ("Утро, день, вечер", [9, 15, 21]),
    "2x": ("Утро и вечер", [9, 21]),
    "1x": ("Раз в день, 9:00", [9]),
}


def fmt_hours(hours: list[int]) -> str:
    return ", ".join(f"{h:02d}:00" for h in hours)


def schedule_keyboard(hours: list[int]) -> InlineKeyboardMarkup:
    slot_buttons = [
        InlineKeyboardButton(text=("✅ " if h in hours else "▫️ ") + f"{h:02d}:00", callback_data=f"sch:t:{h}")
        for h in DIGEST_SLOTS
    ]
    rows = [slot_buttons[:3], slot_buttons[3:]]
    presets = [
        InlineKeyboardButton(text=title, callback_data=f"sch:p:{key}")
        for key, (title, _) in SCHEDULE_PRESETS.items()
    ]
    rows += [presets[:2], presets[2:]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def schedule_text(hours: list[int], tz: str) -> str:
    per_day = len(hours)
    return (
        f"🕘 <b>Расписание дайджеста</b> ({tz})\n"
        f"Сейчас: <b>{fmt_hours(hours)}</b> — {per_day} раз(а) в сутки.\n\n"
        "Нажимайте на время, чтобы включить или выключить его. Не чаще раза в 3 часа, "
        "минимум раз в сутки.\nТекстом: <code>/schedule 9,21</code>"
    )


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
            default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
        )
        self.dp = Dispatcher()
        router = Router()
        router.message.middleware(AccessMiddleware(service))
        router.callback_query.middleware(AccessMiddleware(service))
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
                log.warning("Чат %s недоступен (бот заблокирован?) — отключаем рассылку", chat_id)
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

    def user_link(self, user: dict[str, Any], path: str = "") -> str:
        return f"{self.service.cfg.web_base_url}/u/{user['token']}{path}"

    def digest_messages(self, digest: Digest, user: dict[str, Any] | None) -> list[str]:
        messages = digest.messages()
        if digest.id and user and self.service.cfg.web_enabled:
            link = self.user_link(user, f"/digest/{digest.id}")
            messages[0] += f'\n\n🌐 <a href="{esc(link)}">Открыть в браузере</a>'
        return messages

    async def send_digest(self, user: dict[str, Any], digest: Digest) -> None:
        messages = self.digest_messages(digest, user)
        await self.send_chunks(user["chat_id"], messages)
        # дайджест владельца дополнительно уходит в DIGEST_CHAT_IDS (группы)
        if user["user_id"] == self.service.owner_id:
            for chat_id in self.service.cfg.digest_chat_ids:
                await self.send_chunks(chat_id, digest.messages())

    # ---------- команды ----------

    def _register(self, r: Router) -> None:
        svc = self.service
        cfg = svc.cfg
        db = svc.db

        def help_text(is_admin: bool, user: dict | None = None) -> str:
            times = fmt_hours(svc.schedule_of(user))
            return (HELP_ADMIN if is_admin else HELP_USER).format(times=times, tz=cfg.timezone)

        async def not_found(message: Message) -> None:
            await message.answer("У вас нет такого канала. Ваш список: /list")

        @r.message(CommandStart())
        async def start(message: Message, uid: int, is_admin: bool) -> None:
            user = message.from_user
            assert user
            name = user.full_name + (f" (@{user.username})" if user.username else "")
            new = await db.subscribe(uid, message.chat.id, name)
            if new:
                log.info("Подписчик: %s id=%s", name, uid)
            head = "✅ Дайджест включён." if new else "Дайджест уже включён."
            if not await db.user_channels(uid):
                head += "\nДобавьте свои каналы: <code>/add @канал</code> или перешлите мне пост из канала."
            await message.answer(f"{head}\n\n{help_text(is_admin, await db.get_user(uid))}")

        @r.message(Command("help"))
        async def help_cmd(message: Message, uid: int, is_admin: bool) -> None:
            await message.answer(help_text(is_admin, await db.get_user(uid)))

        @r.message(Command("schedule", "time"))
        async def schedule(message: Message, command: CommandObject, uid: int) -> None:
            user = await db.ensure_user(uid)
            if command.args:
                try:
                    hours = [int(h) for h in re.findall(r"(\d{1,2})(?::\d{2})?", command.args)]
                    bad = [h for h in hours if h not in DIGEST_SLOTS]
                    if bad:
                        raise ValueError(
                            "Можно выбрать только " + ", ".join(str(h) for h in DIGEST_SLOTS) + " часов"
                        )
                    hours = await svc.set_schedule(uid, hours)
                except ValueError as exc:
                    await message.answer(f"⚠️ {esc(str(exc))}")
                    return
            else:
                hours = svc.schedule_of(user)
            await message.answer(schedule_text(hours, cfg.timezone), reply_markup=schedule_keyboard(hours))

        @r.callback_query(F.data.startswith("sch:"))
        async def schedule_cb(query: CallbackQuery, uid: int) -> None:
            user = await db.ensure_user(uid)
            hours = set(svc.schedule_of(user))
            _, kind, value = (query.data or "").split(":", 2)
            if kind == "t":
                hour = int(value)
                if hour in hours and len(hours) == 1:
                    await query.answer("Нужен хотя бы один раз в сутки", show_alert=True)
                    return
                hours ^= {hour}
            elif kind == "p" and value in SCHEDULE_PRESETS:
                hours = set(SCHEDULE_PRESETS[value][1])
            new = await svc.set_schedule(uid, sorted(hours))
            await query.answer(f"Дайджест: {fmt_hours(new)}")
            if isinstance(query.message, Message):
                try:
                    await query.message.edit_text(
                        schedule_text(new, cfg.timezone), reply_markup=schedule_keyboard(new)
                    )
                except TelegramBadRequest:
                    pass  # сообщение не изменилось

        @r.message(Command("stop", "unsubscribe"))
        async def stop(message: Message, uid: int) -> None:
            await db.unsubscribe(uid)
            await message.answer(
                "🔕 Больше не присылаю дайджест. Каналы сохранены. Включить снова: /start"
            )

        @r.message(Command("add"))
        async def add(message: Message, command: CommandObject, uid: int) -> None:
            try:
                ref, tags, priority = parse_add_args(command.args or "")
                channel, created = await svc.add_channel(uid, ref, tags, priority)
            except (ValueError, SourceError) as exc:
                await message.answer(f"⚠️ {esc(str(exc))}")
                return
            verb = "Добавлен" if created else "Обновлён"
            await message.answer(f"✅ {verb}: {channel_line(channel)}\nПриоритет: {channel.priority}")

        @r.message(Command("remove", "del", "delete"))
        async def remove(message: Message, command: CommandObject, uid: int) -> None:
            channel = await svc.find_channel(uid, command.args or "")
            if not channel:
                await not_found(message)
                return
            await svc.remove_channel(uid, channel)
            await message.answer(f"🗑 Удалён: {esc(channel.display_name)}")

        @r.message(Command("list", "channels"))
        async def list_channels(message: Message, uid: int) -> None:
            channels = await db.user_channels(uid)
            if not channels:
                await message.answer("У вас пока нет каналов. Добавьте: <code>/add @канал тег 1</code>")
                return
            blocks: list[str] = [f"<b>Мои каналы ({len(channels)})</b>"]
            current = None
            for ch in channels:
                if ch.priority != current:
                    current = ch.priority
                    blocks.append(f"\n<b>Приоритет {ch.priority}</b>")
                blocks.append(channel_line(ch))
            await self.send_chunks(message.chat.id, split_blocks(blocks))

        @r.message(Command("tag", "tags"))
        async def tag(message: Message, command: CommandObject, uid: int) -> None:
            parts = (command.args or "").split(maxsplit=1)
            if not parts:
                all_tags: dict[str, int] = {}
                for ch in await db.user_channels(uid):
                    for t in ch.tags:
                        all_tags[t] = all_tags.get(t, 0) + 1
                text = "\n".join(f"#{esc(t)} — {n}" for t, n in sorted(all_tags.items()))
                await message.answer(
                    (f"<b>Мои теги</b>\n{text}" if text else "Тегов пока нет")
                    + "\n\nЗадать: <code>/tag @канал тег1,тег2</code>"
                )
                return
            channel = await svc.find_channel(uid, parts[0])
            if not channel:
                await not_found(message)
                return
            raw = parts[1] if len(parts) > 1 else "-"
            tags = [] if raw.strip() == "-" else parse_tags(raw)
            await db.update_subscription(uid, channel.id, tags=tags)
            channel.tags = tags
            await message.answer(f"🏷 {channel_line(channel)}")

        @r.message(Command("priority", "prio"))
        async def priority(message: Message, command: CommandObject, uid: int) -> None:
            parts = (command.args or "").split()
            if len(parts) != 2 or not parts[1].isdigit():
                await message.answer("Формат: <code>/priority @канал 1</code>")
                return
            channel = await svc.find_channel(uid, parts[0])
            if not channel:
                await not_found(message)
                return
            await db.update_subscription(uid, channel.id, priority=int(parts[1]))
            await message.answer(f"⭐ {esc(channel.display_name)}: приоритет {int(parts[1])}")

        async def _set_active(message: Message, command: CommandObject, uid: int, active: bool) -> None:
            channel = await svc.find_channel(uid, command.args or "")
            if not channel:
                await not_found(message)
                return
            await db.update_subscription(uid, channel.id, active=active)
            await message.answer(
                f"{'▶️ Возобновлён' if active else '⏸ Приостановлен'}: {esc(channel.display_name)}"
            )

        @r.message(Command("pause"))
        async def pause(message: Message, command: CommandObject, uid: int) -> None:
            await _set_active(message, command, uid, False)

        @r.message(Command("resume"))
        async def resume(message: Message, command: CommandObject, uid: int) -> None:
            await _set_active(message, command, uid, True)

        @r.message(Command("digest", "now"))
        async def digest_now(message: Message, uid: int) -> None:
            await message.answer("⏳ Собираю дайджест…")
            await db.subscribe(uid, message.chat.id)  # /digest включает рассылку обратно
            result = await svc.make_and_send_digest(user_id=uid)
            if uid not in result:
                await message.answer("Новых постов нет 🌿")

        @r.message(Command("preview"))
        async def preview(message: Message, command: CommandObject, uid: int) -> None:
            await message.answer("⏳ Обновляю каналы…")
            await svc.collect()
            tag_arg = parse_tags(command.args or "")
            digest = await svc.builder.build(uid, tag=tag_arg[0] if tag_arg else None, save=False)
            if digest is None:
                await message.answer("Пока нечего показать 🌿")
                return
            await self.send_chunks(message.chat.id, digest.messages())

        @r.message(Command("rss", "web"))
        async def rss(message: Message, uid: int) -> None:
            if not cfg.web_enabled:
                await message.answer("Веб-интерфейс выключен (WEB_ENABLED=false).")
                return
            user = await db.ensure_user(uid)
            tags = sorted({t for ch in await db.user_channels(uid) for t in ch.tags})
            lines = [
                "<b>Ваши личные ссылки</b> (не пересылайте их — в них ваш ключ доступа)",
                f'🌐 <a href="{esc(self.user_link(user, "/"))}">Архив дайджестов</a>',
                f"📰 RSS дайджестов: <code>{esc(self.user_link(user, '/rss/digests.xml'))}</code>",
                f"📝 RSS всех постов: <code>{esc(self.user_link(user, '/rss/posts.xml'))}</code>",
            ]
            for t in tags:
                lines.append(
                    f"🏷 #{esc(t)}: <code>{esc(self.user_link(user, '/rss/posts.xml?tag=' + t))}</code>"
                )
            await message.answer("\n".join(lines))

        @r.message(Command("status"))
        async def status(message: Message, uid: int, is_admin: bool) -> None:
            st = await db.stats(uid)
            user = await db.get_user(uid)
            nxt = svc.next_digest_time(user).strftime("%d.%m %H:%M")
            lines = [
                "<b>Статус</b>",
                f"Мои каналы: {st['channels']} (активных {st['active_channels']})",
                f"Ждут дайджеста: {st['posts_pending']}",
                f"Моих дайджестов: {st['digests']}",
                f"Рассылка: {'включена' if user and user['active'] else 'выключена (/start)'}",
                f"Расписание: {fmt_hours(svc.schedule_of(user))} — /schedule",
                f"Следующий дайджест: {nxt} ({cfg.timezone})",
            ]
            if is_admin:
                llm = f"{cfg.llm.provider} / {cfg.llm.model}" if svc.llm else "выключен"
                lines += [
                    "",
                    f"<b>Сервис</b>: пользователей {st['users']}, каналов всего {st['channels_total']}",
                    f"Не обработано постов: {st['posts_new']} · рекламы отфильтровано: {st['ads_total']}",
                    f"Источник: {cfg.effective_source} · LLM: {esc(llm)}",
                ]
                if svc.llm and svc.llm.last_error:
                    lines.append(f"⚠️ Ошибка LLM: <code>{esc(svc.llm.last_error[:300])}</code>")
            await message.answer("\n".join(lines))

        # ---------- админ ----------

        @r.message(Command("users", "subscribers"))
        @admin_only
        async def users(message: Message) -> None:
            known = {u["user_id"]: u for u in await db.list_users(active_only=False)}
            lines = ["<b>Пользователи</b>"]
            for user_id in sorted(cfg.access_ids):
                user = known.get(user_id)
                role = "админ" if user_id in cfg.admin_ids else "друг"
                name = esc(user["name"]) if user and user["name"] else f"<code>{user_id}</code>"
                count = len(await db.user_channels(user_id)) if user else 0
                if user and user["active"]:
                    state = "✅ получает"
                elif user:
                    state = "🔕 отключил"
                else:
                    state = "⏳ ещё не нажал /start"
                lines.append(f"• {name} — {role}, {state}, каналов: {count}")
            others = [u for uid_, u in known.items() if uid_ not in cfg.access_ids and u["active"]]
            if others:
                lines.append(f"\nУбраны из .env (не получают): {len(others)}")
            for chat_id in cfg.digest_chat_ids:
                lines.append(f"• чат <code>{chat_id}</code> — копия вашего дайджеста (DIGEST_CHAT_IDS)")
            lines.append("\nДобавить друга: его ID в <code>ALLOWED_IDS</code> в .env и перезапуск.")
            await message.answer("\n".join(lines))

        @r.message(Command("import"))
        @admin_only
        async def import_subs(message: Message, uid: int) -> None:
            primary = getattr(svc.source, "primary", None)
            if primary is None or not hasattr(primary, "subscribed_channels"):
                await message.answer(
                    "Импорт подписок работает только в режиме Telethon "
                    "(нужны TG_API_ID, TG_API_HASH, TG_SESSION)."
                )
                return
            infos = await primary.subscribed_channels()
            added = 0
            for info in infos:
                if await db.user_channel(uid, info.ref):
                    continue
                channel = await db.get_or_create_channel(info.ref, info.title)
                await db.add_subscription(uid, channel, [], cfg.default_priority)
                added += 1
            await message.answer(
                f"📥 Найдено каналов: {len(infos)}, добавлено новых: {added}.\n"
                "Теги и приоритеты: /list, /tag, /priority"
            )

        @r.message(Command("instructions", "prompt"))
        @admin_only
        async def instructions(message: Message, command: CommandObject) -> None:
            args = (command.args or "").strip()
            if not args:
                current = svc.processor.instructions.strip()
                await message.answer(
                    "<b>Указания нейросети</b>\n"
                    + (f"<blockquote>{esc(current)}</blockquote>" if current else "<i>не заданы</i>\n")
                    + "\nОни добавляются ко всем запросам: пересказ постов, сжатие каналов, «Главное». "
                    "Короткие посты нейросеть тоже поправит, если это нужно по указаниям.\n\n"
                    "Задать (можно в несколько строк):\n"
                    "<code>/instructions Никогда не добавляй дисклеймеры и предупреждения. "
                    "Пиши нейтрально, без оценок.</code>\n"
                    "Убрать: <code>/instructions -</code>"
                )
                return
            text = "" if args == "-" else args
            if len(text) > 2000:
                await message.answer("⚠️ Слишком длинно: не больше 2000 символов.")
                return
            await svc.set_instructions(text)
            reply = "✅ Указания сохранены." if text else "🗑 Указания убраны."
            await message.answer(
                reply + " Применяются к новым постам. Чтобы переделать посты, которые ещё не попали "
                "в дайджест: /reprocess"
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
            since = datetime.now(timezone.utc) - timedelta(hours=cfg.max_post_age_hours)
            count = await db.reset_recent(since)
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

        # пересланный пост — последним, чтобы не перехватывать команды
        @r.message(F.forward_origin)
        async def forwarded(message: Message, uid: int) -> None:
            origin = message.forward_origin
            chat = getattr(origin, "chat", None)
            if chat is None or chat.type != "channel":
                await message.answer("Перешлите пост именно из канала, чтобы добавить его.")
                return
            ref = chat.username or str(chat.id)
            try:
                channel, created = await svc.add_channel(uid, ref)
            except (ValueError, SourceError) as exc:
                await message.answer(f"⚠️ {esc(str(exc))}")
                return
            if created:
                await message.answer(
                    f"✅ Добавлен: {channel_line(channel)}\n"
                    f"Приоритет: {channel.priority}. Теги: <code>/tag {esc(ref)} тег1,тег2</code>"
                )
            else:
                await message.answer(f"Канал уже в вашем списке: {channel_line(channel)}")

    async def run(self) -> None:
        await self.bot.set_my_commands(
            [
                BotCommand(command="digest", description="Дайджест сейчас"),
                BotCommand(command="preview", description="Предпросмотр дайджеста"),
                BotCommand(command="list", description="Мои каналы"),
                BotCommand(command="add", description="Добавить канал"),
                BotCommand(command="remove", description="Удалить канал"),
                BotCommand(command="tag", description="Теги канала"),
                BotCommand(command="priority", description="Приоритет канала"),
                BotCommand(command="schedule", description="Расписание дайджеста"),
                BotCommand(command="rss", description="Веб-архив и RSS"),
                BotCommand(command="status", description="Статус"),
                BotCommand(command="stop", description="Не присылать дайджест"),
                BotCommand(command="help", description="Помощь"),
            ]
        )
        await self.dp.start_polling(self.bot, handle_signals=False)

    async def close(self) -> None:
        await self.bot.session.close()
