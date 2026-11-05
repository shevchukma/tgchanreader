from datetime import timedelta

from tgchanreader.digest import split_blocks, visible_len
from tgchanreader.models import RawPost
from tgchanreader.service import Service

from .conftest import UID, FakeLLM, FakeSource, now

LONG = "Очень длинный пост про политику. " * 40


async def make_service(cfg, llm):
    source = FakeSource()
    svc = Service(cfg, source=source, llm=llm)
    await svc.start()
    return svc, source


async def test_full_flow(cfg):
    llm = FakeLLM(ads={"КУПИ"})
    svc, source = await make_service(cfg, llm)
    try:
        await svc.add_channel(UID, "@politics_ch", ["политика"], 1)
        await svc.add_channel(UID, "@velo_ch", ["велосипед"], 3)
        await svc.add_channel(UID, "@misc_ch", None, 2)

        t = now() - timedelta(hours=2)
        source.posts["politics_ch"] = [
            RawPost(1, t, LONG, url="https://t.me/politics_ch/1"),
            RawPost(2, t, "Короткая новость", url="https://t.me/politics_ch/2"),
            RawPost(3, t, "КУПИ наш курс", url="https://t.me/politics_ch/3"),
            RawPost(4, t, "Реклама. erid: abcdef123", url="https://t.me/politics_ch/4"),
            RawPost(6, t, "Ещё новость <важная>", url="https://t.me/politics_ch/6"),
        ]
        source.posts["velo_ch"] = [RawPost(10, t, "", media=["photo"], url="https://t.me/velo_ch/10")]
        source.posts["misc_ch"] = [RawPost(5, t - timedelta(days=5), "Старый пост")]

        added = await svc.collect()
        assert added == 6  # старый пост отброшен по дате

        digest = await svc.builder.build(UID)
        assert digest is not None
        assert digest.stats == {"posts": 4, "channels": 2, "ads": 2, "summarized": 1, "dropped": 0}

        text = digest.html
        # шапка + по сообщению на канал, по порядку приоритетов
        messages = digest.messages()
        assert len(messages) == 3
        assert "Дайджест" in messages[0] and "Главное за день" in messages[0]
        # приоритет определяет только порядок; в сообщении его нет, теги — в самом низу
        assert messages[1].startswith("<b>") and "Title politics_ch" in messages[1].split("\n")[0]
        assert "Title velo_ch" in messages[2].split("\n")[0]
        assert messages[1].endswith("#политика") and messages[2].endswith("#велосипед")
        assert "ПРИОРИТЕТ" not in text and "🔴" not in text and " пост" not in messages[1].split("\n\n")[0]
        assert "Краткое содержание." in text and "читать полностью" in text
        assert 'href="https://t.me/politics_ch/1"' in text
        assert "Короткая новость" in text
        assert "&lt;важная&gt;" in text  # HTML экранируется
        assert "КУПИ" not in text and "erid" not in text
        assert "Только медиа (фото)" in text and 'href="https://t.me/velo_ch/10"' in text
        assert "Главное за день" in text and "• Пункт три" in text
        assert "#политика" in text and "#велосипед" in text

        # повторно те же посты не попадают
        assert await svc.builder.build(UID) is None

        # новые посты — новый дайджест
        source.posts["velo_ch"].append(RawPost(11, now(), "Новый маршрут", url="https://t.me/velo_ch/11"))
        await svc.collect()
        d2 = await svc.builder.build(UID)
        assert d2 is not None and d2.stats["posts"] == 1
    finally:
        await svc.close()


async def test_without_llm_fallback(cfg):
    svc, source = await make_service(cfg, None)
    try:
        await svc.add_channel(UID, "@chan_one")
        source.posts["chan_one"] = [RawPost(1, now(), LONG)]
        await svc.collect()
        digest = await svc.builder.build(UID, save=False)
        assert digest is not None
        assert digest.stats["summarized"] == 1
        assert "Главное за день" not in digest.html
        # preview не помечает посты
        assert await svc.builder.build(UID, save=False) is not None
    finally:
        await svc.close()


async def test_tag_filter(cfg):
    svc, source = await make_service(cfg, None)
    try:
        await svc.add_channel(UID, "@aaaa_ch", ["политика"], 1)
        await svc.add_channel(UID, "@bbbb_ch", ["велосипед"], 1)
        source.posts["aaaa_ch"] = [RawPost(1, now(), "про политику")]
        source.posts["bbbb_ch"] = [RawPost(1, now(), "про велосипед")]
        await svc.collect()
        digest = await svc.builder.build(UID, tag="велосипед", save=False)
        assert "про велосипед" in digest.html and "про политику" not in digest.html
    finally:
        await svc.close()


def test_split_blocks():
    blocks = [f"<b>block {i}</b> " + "x" * 300 for i in range(40)]
    chunks = split_blocks(blocks, 1000)
    assert len(chunks) > 1
    assert all(visible_len(c) <= 1000 for c in chunks)
    assert "".join(chunks).count("<b>") == 40
    assert all(c.count("<b>") == c.count("</b>") for c in chunks)


async def test_next_digest_time(cfg):
    from datetime import datetime, timezone

    svc = Service(cfg, source=FakeSource(), llm=None)
    user = {"schedule": [9, 21]}
    # 2026-11-05 10:00 МСК = 07:00 UTC
    t = svc.next_digest_time(user, datetime(2026, 11, 5, 7, 0, tzinfo=timezone.utc))
    assert (t.hour, t.day) == (21, 5)
    t = svc.next_digest_time(user, datetime(2026, 11, 5, 19, 0, tzinfo=timezone.utc))
    assert (t.hour, t.day) == (9, 6)
    # общий цикл просыпается в каждый слот
    t = svc.next_slot(now=datetime(2026, 11, 5, 7, 0, tzinfo=timezone.utc))
    assert t.hour == 12
