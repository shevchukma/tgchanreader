from datetime import datetime

from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from tgchanreader.bot import TelegramBot
from tgchanreader.service import Service

from .conftest import FakeSource

ADMIN, FRIEND, STRANGER = 111, 222, 333


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent: list[SendMessage] = []

    async def make_request(self, bot, method: TelegramMethod, timeout=None):
        if isinstance(method, SendMessage):
            self.sent.append(method)
            return Message(
                message_id=len(self.sent),
                date=datetime.now(),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        return True

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        yield b""


async def make_bot(cfg):
    cfg.bot_token = "123456:" + "A" * 35
    cfg.admin_ids = [ADMIN]
    cfg.allowed_ids = [FRIEND]
    svc = Service(cfg, source=FakeSource(), llm=None)
    await svc.start()
    bot = TelegramBot(svc)
    session = RecordingSession()
    bot.bot.session = session
    return svc, bot, session


_uid = 0


async def send(bot: TelegramBot, user_id: int, text: str) -> None:
    global _uid
    _uid += 1
    msg = Message(
        message_id=_uid,
        date=datetime.now(),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name=f"U{user_id}"),
        text=text,
        entities=[{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        if text.startswith("/") else None,
    )
    await bot.dp.feed_update(bot.bot, Update(update_id=_uid, message=msg))


def last_text(session, chat_id):
    return [m.text for m in session.sent if m.chat_id == chat_id][-1]


def last_markup(session, chat_id):
    return [m.reply_markup for m in session.sent if m.chat_id == chat_id][-1]


async def press(bot: TelegramBot, user_id: int, data: str) -> None:
    global _uid
    _uid += 1
    msg = Message(
        message_id=_uid, date=datetime.now(), chat=Chat(id=user_id, type="private"), text="x"
    )
    query = CallbackQuery(
        id=str(_uid),
        from_user=User(id=user_id, is_bot=False, first_name="U"),
        chat_instance="ci",
        message=msg,
        data=data,
    )
    await bot.dp.feed_update(bot.bot, Update(update_id=_uid, callback_query=query))


async def test_access_and_personal_lists(cfg):
    svc, bot, session = await make_bot(cfg)
    try:
        await send(bot, STRANGER, "/start")
        assert "приватный бот" in last_text(session, STRANGER)
        assert str(STRANGER) in last_text(session, STRANGER)

        await send(bot, FRIEND, "/start")
        assert "Дайджест включён" in last_text(session, FRIEND)
        assert "/users" not in last_text(session, FRIEND)  # без админских команд

        # у каждого свой список
        await send(bot, FRIEND, "/add @velo_chan велосипед 1")
        assert "Добавлен" in last_text(session, FRIEND)
        await send(bot, ADMIN, "/add @news_chan новости 2")
        await send(bot, ADMIN, "/add @velo_chan вело 3")  # тот же канал, свои теги/приоритет

        await send(bot, FRIEND, "/list")
        text = last_text(session, FRIEND)
        assert "velo_chan" in text and "news_chan" not in text and "#велосипед" in text
        await send(bot, ADMIN, "/list")
        text = last_text(session, ADMIN)
        assert "news_chan" in text and "#вело" in text and "Приоритет 3" in text
        assert await svc.db.count_channels() == 2  # канал хранится один раз

        await send(bot, FRIEND, "/remove @news_chan")
        assert "нет такого канала" in last_text(session, FRIEND)
        await send(bot, FRIEND, "/remove @velo_chan")
        assert await svc.db.count_channels() == 2  # админ ещё подписан
        await send(bot, ADMIN, "/remove @velo_chan")
        assert await svc.db.count_channels() == 1  # больше никому не нужен — удалён

        await send(bot, FRIEND, "/users")
        assert "только администратору" in last_text(session, FRIEND)

        assert [u["user_id"] for u in await svc.digest_users()] == [ADMIN, FRIEND]
        cfg.allowed_ids = []  # друга убрали из .env
        assert [u["user_id"] for u in await svc.digest_users()] == [ADMIN]
        cfg.allowed_ids = [FRIEND]
        await send(bot, FRIEND, "/stop")
        assert [u["user_id"] for u in await svc.digest_users()] == [ADMIN]

        await send(bot, ADMIN, "/users")
        text = last_text(session, ADMIN)
        assert "админ, ✅ получает" in text and "друг, 🔕 отключил" in text
    finally:
        await svc.close()


async def test_schedule_command(cfg):
    svc, bot, session = await make_bot(cfg)
    try:
        await send(bot, FRIEND, "/start")
        await send(bot, FRIEND, "/schedule")
        assert "09:00" in last_text(session, FRIEND)
        assert last_markup(session, FRIEND) is not None

        await press(bot, FRIEND, "sch:t:21")
        assert svc.schedule_of(await svc.db.get_user(FRIEND)) == [9, 21]
        await press(bot, FRIEND, "sch:p:all")
        assert svc.schedule_of(await svc.db.get_user(FRIEND)) == [6, 9, 12, 15, 18, 21]
        await press(bot, FRIEND, "sch:p:1x")
        await press(bot, FRIEND, "sch:t:9")  # последний слот не выключается
        assert svc.schedule_of(await svc.db.get_user(FRIEND)) == [9]

        await send(bot, FRIEND, "/schedule 06:00, 18")
        assert svc.schedule_of(await svc.db.get_user(FRIEND)) == [6, 18]
        await send(bot, FRIEND, "/schedule 7")
        assert "Можно выбрать только" in last_text(session, FRIEND)

        # в слот 18:00 дайджест получает друг, а у админа по умолчанию только 09:00
        assert [u["user_id"] for u in await svc.digest_users(slot=18)] == [FRIEND]
        assert [u["user_id"] for u in await svc.digest_users(slot=9)] == [ADMIN]
    finally:
        await svc.close()


async def test_digest_messages_silent_after_header(cfg):
    from tgchanreader.models import RawPost

    from .conftest import now

    svc, bot, session = await make_bot(cfg)
    try:
        await svc.add_channel(ADMIN, "@aaaa_ch", None, 1)
        await svc.add_channel(ADMIN, "@bbbb_ch", None, 2)
        svc.source.posts["aaaa_ch"] = [RawPost(1, now(), "пост А")]
        svc.source.posts["bbbb_ch"] = [RawPost(1, now(), "пост Б")]
        await svc.make_and_send_digest()
        sent = [m for m in session.sent if m.chat_id == ADMIN]
        assert len(sent) == 3
        assert "Дайджест" in sent[0].text and not sent[0].disable_notification
        assert "Title aaaa_ch" in sent[1].text and sent[1].disable_notification
        assert "Title bbbb_ch" in sent[2].text
    finally:
        await svc.close()


async def test_instructions_command(cfg):
    svc, bot, session = await make_bot(cfg)
    try:
        await send(bot, FRIEND, "/instructions без дисклеймеров")
        assert "только администратору" in last_text(session, FRIEND)
        await send(bot, ADMIN, "/instructions")
        assert "не заданы" in last_text(session, ADMIN)
        await send(bot, ADMIN, "/instructions Никогда не добавляй дисклеймеры.\nПиши нейтрально.")
        assert "сохранены" in last_text(session, ADMIN)
        assert svc.processor.instructions == "Никогда не добавляй дисклеймеры.\nПиши нейтрально."
        await send(bot, ADMIN, "/instructions")
        assert "Пиши нейтрально" in last_text(session, ADMIN)
        await send(bot, ADMIN, "/instructions -")
        assert svc.processor.instructions == ""
    finally:
        await svc.close()
