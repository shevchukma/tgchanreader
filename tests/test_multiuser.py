from datetime import timedelta

from tgchanreader.db import Database
from tgchanreader.models import RawPost
from tgchanreader.service import Service

from .conftest import FakeLLM, FakeSource, now

ALICE, BOB = 10, 20
LONG = "Длинный пост о важном. " * 50


class CountingLLM(FakeLLM):
    def __init__(self):
        super().__init__()
        self.post_ids: list[int] = []

    async def complete_json(self, system, user, max_tokens=2000):
        import json

        data = json.loads(user)
        self.post_ids += [p["id"] for p in data["posts"]]
        return await super().complete_json(system, user, max_tokens)


async def test_shared_channel_processed_once(cfg):
    llm = CountingLLM()
    svc = Service(cfg, source=FakeSource(), llm=llm)
    await svc.start()
    try:
        await svc.add_channel(ALICE, "@shared_ch", ["новости"], 1)
        await svc.add_channel(BOB, "@shared_ch", ["работа"], 3)
        await svc.add_channel(BOB, "@bob_only", None, 1)
        svc.source.posts["shared_ch"] = [RawPost(1, now(), LONG), RawPost(2, now(), "коротко")]
        svc.source.posts["bob_only"] = [RawPost(1, now(), "только Бобу")]
        await svc.collect()
        await svc.collect()

        # каждый пост обработан нейросетью ровно один раз
        assert sorted(llm.post_ids) == sorted(set(llm.post_ids))
        assert len(llm.post_ids) == 3

        da = await svc.builder.build(ALICE)
        db_ = await svc.builder.build(BOB)
        assert "Краткое содержание." in da.html and "Краткое содержание." in db_.html
        assert "только Бобу" not in da.html and "только Бобу" in db_.html
        # свои теги и приоритеты
        assert da.messages()[1].endswith("#новости")
        bob_shared = [m for m in db_.messages() if "shared_ch" in m.split("\n")[0]][0]
        assert bob_shared.endswith("#работа")
        # у Боба приоритет 1 у bob_only, 3 у shared_ch — порядок по приоритету
        assert db_.messages().index(bob_shared) == 2

        # у каждого свой курсор: Алиса прочитала — Бобу всё равно придёт новое
        svc.source.posts["shared_ch"].append(RawPost(3, now(), "свежее"))
        await svc.collect()
        da2 = await svc.builder.build(ALICE)
        assert da2.stats["posts"] == 1 and "свежее" in da2.html
        assert await svc.builder.build(ALICE) is None
        db2 = await svc.builder.build(BOB)
        assert db2.stats["posts"] == 1 and "свежее" in db2.html
    finally:
        await svc.close()


async def test_late_subscriber_gets_recent_posts_only(cfg):
    cfg.first_fetch_hours = 24
    svc = Service(cfg, source=FakeSource(), llm=None)
    await svc.start()
    try:
        await svc.add_channel(ALICE, "@shared_ch")
        svc.source.posts["shared_ch"] = [
            RawPost(1, now() - timedelta(hours=30), "старое"),
            RawPost(2, now() - timedelta(hours=2), "недавнее"),
        ]
        cfg.first_fetch_hours = 48
        await svc.collect()
        cfg.first_fetch_hours = 24
        await svc.add_channel(BOB, "@shared_ch")
        d = await svc.builder.build(BOB)
        assert "недавнее" in d.html and "старое" not in d.html
    finally:
        await svc.close()


async def test_migration_from_shared_list(cfg, tmp_path):
    """База версии с общим списком каналов переезжает к владельцу."""
    import aiosqlite

    path = str(tmp_path / "old.db")
    async with aiosqlite.connect(path) as conn:
        await conn.executescript(
            """
            CREATE TABLE channels (id INTEGER PRIMARY KEY AUTOINCREMENT, ref TEXT UNIQUE, title TEXT DEFAULT '',
              tags TEXT DEFAULT '', priority INTEGER DEFAULT 2, active INTEGER DEFAULT 1,
              last_post_id INTEGER DEFAULT 0, added_at TEXT);
            CREATE TABLE posts (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id INTEGER, msg_id INTEGER,
              url TEXT, posted_at TEXT, text TEXT DEFAULT '', media TEXT DEFAULT '', status TEXT DEFAULT 'new',
              is_ad INTEGER DEFAULT 0, summary TEXT DEFAULT '', summarized INTEGER DEFAULT 0, digest_id INTEGER,
              UNIQUE(channel_id, msg_id));
            CREATE TABLE digests (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, title TEXT, html TEXT,
              stats TEXT DEFAULT '{}');
            CREATE TABLE subscribers (user_id INTEGER PRIMARY KEY, chat_id INTEGER, name TEXT DEFAULT '',
              active INTEGER DEFAULT 1, created_at TEXT);
            """
        )
        t = now().isoformat()
        await conn.execute(
            "INSERT INTO channels(ref, title, tags, priority, last_post_id, added_at) VALUES (?,?,?,?,?,?)",
            ("old_ch", "Old", "политика", 1, 6, t),
        )
        for msg_id, digest_id in ((5, 1), (6, None)):
            await conn.execute(
                "INSERT INTO posts(channel_id, msg_id, url, posted_at, text, status, digest_id)"
                " VALUES (1,?,?,?,?, 'processed', ?)",
                (msg_id, f"https://t.me/old_ch/{msg_id}", t, f"пост {msg_id}", digest_id),
            )
        await conn.execute(
            "INSERT INTO subscribers VALUES (?,?,?,1,?)", (ALICE, ALICE, "Alice", t)
        )
        await conn.commit()

    cfg.admin_ids = [ALICE]
    svc = Service(cfg, db=Database(path), source=FakeSource(), llm=None)
    await svc.start()
    try:
        channels = await svc.db.user_channels(ALICE)
        assert [c.ref for c in channels] == ["old_ch"]
        assert channels[0].tags == ["политика"] and channels[0].priority == 1
        assert (await svc.db.get_user(ALICE))["name"] == "Alice"
        d = await svc.builder.build(ALICE)
        assert "пост 6" in d.html and "пост 5" not in d.html  # отправленное раньше не повторяется
    finally:
        await svc.close()
