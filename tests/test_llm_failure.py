import httpx

from tgchanreader.config import LLMConfig
from tgchanreader.llm import LLMError, OpenAICompatibleClient
from tgchanreader.models import RawPost
from tgchanreader.service import Service

from .conftest import FakeLLM, FakeSource, now

LONG = "Длинный пост о важном. " * 50

PROVIDER_ERROR = {
    "error": {
        "message": "No allowed providers are available for the selected model.",
        "code": 404,
    }
}


def make_client(handler) -> tuple[OpenAICompatibleClient, list]:
    calls: list = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    client = OpenAICompatibleClient(
        LLMConfig(provider="openrouter", api_key="k", model="m", base_url="https://x/api/v1")
    )
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    return client, calls


async def test_fatal_error_pauses_requests():
    client, calls = make_client(lambda r: httpx.Response(404, json=PROVIDER_ERROR))
    try:
        await client.complete("s", "u")
    except LLMError as exc:
        assert "No allowed providers" in str(exc)
    else:
        raise AssertionError("ожидалась ошибка")
    assert len(calls) == 1  # 404 не повторяем
    assert client.paused and "No allowed providers" in client.last_error

    for _ in range(5):
        try:
            await client.complete("s", "u")
        except LLMError:
            pass
    assert len(calls) == 1  # пока на паузе — в API не ходим

    client.reset()
    assert not client.paused
    await client.close()


async def test_ok_response():
    body = {"choices": [{"message": {"content": "работает"}}]}
    client, _ = make_client(lambda r: httpx.Response(200, json=body))
    assert await client.complete("s", "u") == "работает"
    assert client.last_error == ""
    await client.close()


class BrokenLLM(FakeLLM):
    paused = False
    last_error = ""

    async def complete_json(self, system, user, max_tokens=2000):
        self.calls += 1
        self.paused = True
        self.last_error = "No allowed providers"
        raise LLMError(self.last_error)

    async def complete(self, system, user, max_tokens=2000):
        raise LLMError("No allowed providers")


async def test_posts_deferred_until_llm_works(cfg):
    llm = BrokenLLM()
    svc = Service(cfg, source=FakeSource(), llm=llm)
    await svc.start()
    try:
        await svc.add_channel("@chan_one")
        svc.source.posts["chan_one"] = [
            RawPost(1, now(), LONG),
            RawPost(2, now(), "", media=["photo"]),
        ]
        await svc.collect()
        st = await svc.db.stats()
        assert st["posts_new"] == 1  # текстовый пост ждёт нейросеть, медиа обработано сразу

        # следующий сбор на паузе не дёргает LLM
        calls = llm.calls
        await svc.collect()
        assert llm.calls == calls

        # предпросмотр показывает пост, ничего не сохраняя
        preview = await svc.builder.build(save=False)
        assert preview is not None and preview.stats["posts"] == 2
        assert (await svc.db.stats())["posts_new"] == 1

        # нейросеть починили — пост обрабатывается ею
        svc.processor.llm = FakeLLM()
        await svc.processor.process_pending()
        digest = await svc.builder.build()
        assert "Краткое содержание." in digest.html
    finally:
        await svc.close()


async def test_digest_falls_back_when_llm_down(cfg):
    svc = Service(cfg, source=FakeSource(), llm=BrokenLLM())
    await svc.start()
    try:
        await svc.add_channel("@chan_one")
        svc.source.posts["chan_one"] = [RawPost(1, now(), LONG)]
        await svc.collect()
        digest = await svc.builder.build()
        assert digest is not None and digest.stats["summarized"] == 1
        assert (await svc.db.stats())["posts_new"] == 0
    finally:
        await svc.close()


async def test_reprocess(cfg):
    svc = Service(cfg, source=FakeSource(), llm=None)
    await svc.start()
    try:
        await svc.add_channel("@chan_one")
        svc.source.posts["chan_one"] = [RawPost(1, now(), LONG)]
        await svc.collect()
        assert await svc.db.reset_undigested() == 1
        svc.processor.llm = FakeLLM()
        await svc.processor.process_pending()
        digest = await svc.builder.build()
        assert "Краткое содержание." in digest.html
    finally:
        await svc.close()
