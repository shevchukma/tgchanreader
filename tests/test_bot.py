from datetime import datetime

from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.types import Chat, Message, Update, User

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


async def test_access_and_subscribers(cfg):
    svc, bot, session = await make_bot(cfg)
    try:
        await send(bot, STRANGER, "/start")
        assert "приватный бот" in last_text(session, STRANGER)
        assert str(STRANGER) in last_text(session, STRANGER)

        await send(bot, FRIEND, "/start")
        assert "подписаны" in last_text(session, FRIEND)
        assert "/add" not in last_text(session, FRIEND)  # друзьям — без админских команд

        await send(bot, FRIEND, "/add @some_chan")
        assert "только администратору" in last_text(session, FRIEND)
        assert await svc.db.list_channels() == []

        await send(bot, ADMIN, "/add @some_chan новости 1")
        assert "Добавлен" in last_text(session, ADMIN)

        await send(bot, FRIEND, "/list")
        assert "Title some_chan" in last_text(session, FRIEND)

        # админ подписывается автоматически при запуске
        await svc.db.ensure_subscriber(ADMIN)
        assert await bot.recipients() == [FRIEND, ADMIN]

        # друга убрали из .env — он больше не получает дайджест
        cfg.allowed_ids = []
        assert await bot.recipients() == [ADMIN]
        cfg.allowed_ids = [FRIEND]

        await send(bot, FRIEND, "/stop")
        assert await bot.recipients() == [ADMIN]

        await send(bot, ADMIN, "/subscribers")
        text = last_text(session, ADMIN)
        assert "админ, ✅ подписан" in text and "друг, 🔕 отписался" in text
    finally:
        await svc.close()


async def test_digest_messages_silent_after_header(cfg):
    from tgchanreader.models import RawPost

    from .conftest import now

    svc, bot, session = await make_bot(cfg)
    try:
        await svc.add_channel("@aaaa_ch", None, 1)
        await svc.add_channel("@bbbb_ch", None, 2)
        svc.source.posts["aaaa_ch"] = [RawPost(1, now(), "пост А")]
        svc.source.posts["bbbb_ch"] = [RawPost(1, now(), "пост Б")]
        await svc.db.ensure_subscriber(ADMIN)
        await svc.make_and_send_digest()
        sent = [m for m in session.sent if m.chat_id == ADMIN]
        assert len(sent) == 3
        assert "Дайджест" in sent[0].text and not sent[0].disable_notification
        assert "Title aaaa_ch" in sent[1].text and sent[1].disable_notification
        assert "Title bbbb_ch" in sent[2].text
    finally:
        await svc.close()
