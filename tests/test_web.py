from aiohttp.test_utils import TestClient, TestServer

from tgchanreader.models import RawPost
from tgchanreader.service import Service
from tgchanreader.web import build_app

from .conftest import FakeSource, now

ALICE, BOB = 10, 20


async def test_personal_links(cfg):
    cfg.bot_token = "x"
    cfg.admin_ids = [ALICE]
    cfg.allowed_ids = [BOB]
    svc = Service(cfg, source=FakeSource(), llm=None)
    await svc.start()
    client = TestClient(TestServer(build_app(svc)))
    await client.start_server()
    try:
        await svc.add_channel(ALICE, "@alice_ch", ["вело"])
        await svc.add_channel(BOB, "@bob_ch")
        svc.source.posts["alice_ch"] = [RawPost(1, now(), "пост Алисы")]
        svc.source.posts["bob_ch"] = [RawPost(1, now(), "пост Боба")]
        await svc.collect()
        da = await svc.builder.build(ALICE)
        alice = await svc.db.get_user(ALICE)
        bob = await svc.db.get_user(BOB)

        r = await client.get(f"/u/{alice['token']}/")
        assert r.status == 200 and "Дайджест" in await r.text()
        r = await client.get(f"/u/{alice['token']}/digest/{da.id}")
        assert r.status == 200 and "пост Алисы" in await r.text()
        # чужой дайджест по своему токену не открывается
        r = await client.get(f"/u/{bob['token']}/digest/{da.id}")
        assert r.status == 404
        r = await client.get(f"/u/{bob['token']}/rss/posts.xml")
        body = await r.text()
        assert "пост Боба" in body and "пост Алисы" not in body
        r = await client.get(f"/u/{alice['token']}/rss/posts.xml?tag=вело")
        assert "пост Алисы" in await r.text()
        assert (await client.get("/u/wrong/")).status == 404
        # общий RSS в режиме с ботом и без WEB_TOKEN закрыт
        assert (await client.get("/rss/posts.xml")).status == 404
        assert (await client.get("/healthz")).status == 200

        # друга убрали из .env — ссылка перестаёт работать
        cfg.allowed_ids = []
        assert (await client.get(f"/u/{bob['token']}/rss/posts.xml")).status == 404
    finally:
        await client.close()
        await svc.close()


async def test_rss_only_mode(cfg):
    svc = Service(cfg, source=FakeSource(), llm=None)  # без бота: владелец user 0
    await svc.start()
    client = TestClient(TestServer(build_app(svc)))
    await client.start_server()
    try:
        await svc.add_channel(svc.owner_id, "@some_ch")
        svc.source.posts["some_ch"] = [RawPost(1, now(), "пост")]
        await svc.collect()
        result = await svc.make_and_send_digest()
        assert svc.owner_id in result
        r = await client.get("/rss/digests.xml")
        assert r.status == 200 and "Дайджест" in await r.text()
        assert (await client.get("/")).status == 200
    finally:
        await client.close()
        await svc.close()
