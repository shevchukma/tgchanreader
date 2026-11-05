from datetime import time, timedelta

import pytest

from tgchanreader.config import parse_times
from tgchanreader.digest import MESSAGE_LIMIT, visible_len
from tgchanreader.models import RawPost
from tgchanreader.service import Service

from .conftest import UID, FakeLLM, FakeSource, now


def test_parse_times_range():
    assert parse_times("06:00-21:00/3") == [time(h) for h in (6, 9, 12, 15, 18, 21)]
    assert parse_times("6-21/3") == [time(h) for h in (6, 9, 12, 15, 18, 21)]
    assert parse_times("09:00-10:00/30m") == [time(9), time(9, 30), time(10)]
    assert parse_times("08:00, 19:30,08:00") == [time(8), time(19, 30)]
    with pytest.raises(ValueError):
        parse_times("25:00")


def test_visible_len():
    assert visible_len('<a href="https://very.long/url">ab</a> &lt;c&gt;') == 6
    assert visible_len("🔴") == 2  # Telegram считает в UTF-16


class CompressingLLM(FakeLLM):
    """Оставляет каждый второй пост, остальное — мелочи."""

    def __init__(self):
        super().__init__()
        self.compress_calls = 0

    async def complete_json(self, system, user, max_tokens=2000):
        if "не помещаются в одно сообщение" in system:
            import json

            self.compress_calls += 1
            posts = json.loads(user)["posts"]
            return {"keep": [{"id": p["id"], "text": f"Суть поста {p['id']}"} for p in posts[::2]]}
        return await super().complete_json(system, user, max_tokens)


def busy_channel_posts(n=40):
    t = now() - timedelta(hours=5)
    text = "Новость средней длины про события в городе, без особой важности. " * 6
    return [RawPost(i, t + timedelta(minutes=i), text, url=f"https://t.me/busy_ch/{i}") for i in range(1, n + 1)]


async def make(cfg, llm):
    svc = Service(cfg, source=FakeSource(), llm=llm)
    await svc.start()
    await svc.add_channel(UID, "@busy_ch", ["город"], 1)
    await svc.add_channel(UID, "@calm_ch", None, 2)
    svc.source.posts["busy_ch"] = busy_channel_posts()
    svc.source.posts["calm_ch"] = [RawPost(1, now(), "Одна спокойная новость")]
    await svc.collect()
    return svc


async def test_busy_channel_compressed_into_one_message(cfg):
    llm = CompressingLLM()
    svc = await make(cfg, llm)
    try:
        digest = await svc.builder.build(UID)
        messages = digest.messages()
        assert len(messages) == 3  # шапка + 2 канала
        busy = messages[1]
        assert visible_len(busy) <= MESSAGE_LIMIT
        assert "сжато" in busy and "Суть поста" in busy
        assert "…и ещё" in busy and 'href="https://t.me/busy_ch"' in busy
        assert llm.compress_calls == 1
        assert digest.stats["dropped"] >= 20
        # спокойный канал не сжимался
        assert "Одна спокойная новость" in messages[2] and "сжато" not in messages[2]
    finally:
        await svc.close()


async def test_busy_channel_without_llm_is_split(cfg):
    svc = await make(cfg, None)
    try:
        digest = await svc.builder.build(UID)
        messages = digest.messages()
        busy_parts = [m for m in messages if "busy_ch" in m.split("\n")[0]]
        assert len(busy_parts) > 1
        assert all(visible_len(m) <= MESSAGE_LIMIT for m in messages)
        assert "продолжение" in busy_parts[1]
    finally:
        await svc.close()


async def test_compression_respects_limit_even_if_llm_ignores_budget(cfg):
    class GreedyLLM(CompressingLLM):
        async def complete_json(self, system, user, max_tokens=2000):
            if "не помещаются в одно сообщение" in system:
                import json

                posts = json.loads(user)["posts"]
                return {"keep": [{"id": p["id"], "text": "Очень длинно " * 30} for p in posts]}
            return await super().complete_json(system, user, max_tokens)

    svc = await make(cfg, GreedyLLM())
    try:
        digest = await svc.builder.build(UID)
        assert all(visible_len(m) <= MESSAGE_LIMIT for m in digest.messages())
        assert digest.stats["dropped"] > 0
    finally:
        await svc.close()


async def test_users(cfg, db):
    assert await db.subscribe(1, 1, "Admin") is True
    assert await db.subscribe(1, 1, "") is False
    await db.ensure_user(2)
    await db.unsubscribe(2)
    await db.ensure_user(2)  # не включает обратно того, кто отписался
    users = await db.list_users()
    assert [u["user_id"] for u in users] == [1]
    assert users[0]["name"] == "Admin" and users[0]["token"]
