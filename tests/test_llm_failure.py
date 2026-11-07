import httpx

from tgchanreader.config import LLMConfig
from tgchanreader.llm import LLMError, OpenAICompatibleClient
from tgchanreader.models import RawPost
from tgchanreader.service import Service

from .conftest import UID, FakeLLM, FakeSource, now

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
        await svc.add_channel(UID, "@chan_one")
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
        preview = await svc.builder.build(UID, save=False)
        assert preview is not None and preview.stats["posts"] == 2
        assert (await svc.db.stats())["posts_new"] == 1

        # нейросеть починили — пост обрабатывается ею
        svc.processor.llm = FakeLLM()
        await svc.processor.process_pending()
        digest = await svc.builder.build(UID)
        assert "Краткое содержание." in digest.html
    finally:
        await svc.close()


async def test_digest_falls_back_when_llm_down(cfg):
    svc = Service(cfg, source=FakeSource(), llm=BrokenLLM())
    await svc.start()
    try:
        await svc.add_channel(UID, "@chan_one")
        svc.source.posts["chan_one"] = [RawPost(1, now(), LONG)]
        await svc.collect()
        digest = await svc.builder.build(UID)
        assert digest is not None and digest.stats["summarized"] == 1
        assert (await svc.db.stats())["posts_new"] == 0
    finally:
        await svc.close()


async def test_reprocess(cfg):
    svc = Service(cfg, source=FakeSource(), llm=None)
    await svc.start()
    try:
        await svc.add_channel(UID, "@chan_one")
        svc.source.posts["chan_one"] = [RawPost(1, now(), LONG)]
        await svc.collect()
        from datetime import timedelta

        assert await svc.db.reset_recent(now() - timedelta(hours=48)) == 1
        svc.processor.llm = FakeLLM()
        await svc.processor.process_pending()
        digest = await svc.builder.build(UID)
        assert "Краткое содержание." in digest.html
    finally:
        await svc.close()


def chat_response(content, finish="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish}]}


async def test_truncated_and_empty_answers():
    client, calls = make_client(lambda r: httpx.Response(200, json=chat_response("", "length")))
    try:
        await client.complete("s", "u")
    except LLMError as exc:
        assert "LLM_MAX_TOKENS" in str(exc)
    else:
        raise AssertionError("пустой ответ должен быть ошибкой")
    await client.close()

    client, _ = make_client(lambda r: httpx.Response(200, json=chat_response("• Один\n• Два\n• Тр", "length")))
    text = await client.complete("s", "u")
    assert text.truncated
    await client.close()


async def test_max_tokens_floor_and_reasoning():
    import json as _json

    seen = []

    def handler(request):
        seen.append(_json.loads(request.content))
        return httpx.Response(200, json=chat_response('{"posts": []}'))

    client, _ = make_client(handler)
    client.cfg.max_tokens = 8000
    client.cfg.reasoning = "off"
    await client.complete_json("s", "u", max_tokens=500)
    assert seen[-1]["max_tokens"] == 8000
    assert seen[-1]["reasoning"] == {"enabled": False}
    client.cfg.reasoning = "low"
    await client.complete("s", "u")
    assert seen[-1]["reasoning"] == {"effort": "low", "exclude": True}
    await client.close()


async def test_complete_json_retries_once_on_garbage():
    answers = iter([chat_response("извините, не могу"), chat_response('{"posts": []}')])
    client, calls = make_client(lambda r: httpx.Response(200, json=next(answers)))
    assert await client.complete_json("s", "u") == {"posts": []}
    assert len(calls) == 2
    await client.close()


async def test_overview_drops_cut_bullet(cfg):
    from tgchanreader.llm import LLMText
    from tgchanreader.processor import Processor

    class CutLLM(FakeLLM):
        async def complete(self, system, user, max_tokens=2000):
            text = LLMText("• Первый пункт.\n• Второй пункт.\n• Спикер Палаты представителей Майк Дж")
            text.truncated = True
            return text

    from tgchanreader.models import Channel, Post

    ch = Channel(id=1, ref="c")
    entries = [
        (Post(i, 1, i, "u", now(), f"новость {i}", []), ch) for i in range(1, 5)
    ]
    overview = await Processor(cfg, None, CutLLM()).overview(entries)
    assert overview == "• Первый пункт.\n• Второй пункт."


def test_public_web(cfg):
    assert not cfg.public_web  # http://localhost:8080 по умолчанию
    cfg.web_base_url = "https://digest.example.com"
    assert cfg.public_web
    cfg.web_enabled = False
    assert not cfg.public_web


def test_scanner_noise_filtered():
    import logging

    from aiohttp.http_exceptions import BadStatusLine

    from tgchanreader.__main__ import _drop_bad_requests

    try:
        raise BadStatusLine(b"\x16\x03\x01")
    except BadStatusLine:
        import sys

        rec = logging.LogRecord("aiohttp.server", logging.ERROR, "", 0, "Error handling request", (), sys.exc_info())
    assert _drop_bad_requests(rec) is False
    ok = logging.LogRecord("aiohttp.server", logging.ERROR, "", 0, "real error", (), None)
    assert _drop_bad_requests(ok) is True
