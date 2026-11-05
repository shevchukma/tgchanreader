from tgchanreader.cleanup import parse_rules, strip_lines
from tgchanreader.models import RawPost
from tgchanreader.service import Service

from .conftest import UID, FakeSource, now

BAZA_POST = "Главная новость дня.\n\nПодробности в источнике.\n\nBAZA в Telegram\nBAZA в MAX"


def test_strip_lines_rules():
    rules = parse_rules("@baza:BAZA в Telegram; @baza:baza  в  max ;Подписаться*;re:^Реклама: .+$")
    assert strip_lines(BAZA_POST, rules, "baza") == "Главная новость дня.\n\nПодробности в источнике."
    # правила @baza не трогают другие каналы
    assert strip_lines(BAZA_POST, rules, "other") == BAZA_POST
    assert strip_lines("Текст\nПодписаться на канал: t.me/x", rules, "any") == "Текст"
    assert strip_lines("Текст\nРеклама: купите", rules, "any") == "Текст"
    # строка, где фраза лишь часть текста, остаётся
    assert strip_lines("Про BAZA в MAX пишут многие", rules, "baza") == "Про BAZA в MAX пишут многие"


def test_bad_regex_is_skipped():
    assert len(parse_rules("re:([;ok")) == 1  # «;» делит правила: «re:([» битое, «ok» — нормальное


async def test_rules_applied_on_collect(cfg):
    cfg.strip_lines = "@baza:BAZA в Telegram;@baza:BAZA в MAX"
    svc = Service(cfg, source=FakeSource(), llm=None)
    await svc.start()
    try:
        await svc.add_channel(UID, "@baza")
        svc.source.posts["baza"] = [
            RawPost(1, now(), BAZA_POST),
            RawPost(2, now(), "BAZA в Telegram\nBAZA в MAX"),  # остался пустой — пропадает
        ]
        await svc.collect()
        digest = await svc.builder.build(UID)
        assert "Главная новость дня." in digest.html and "BAZA в" not in digest.html
        assert digest.stats["posts"] == 1
    finally:
        await svc.close()


async def test_instructions_from_env_and_reset(cfg):
    cfg.llm_instructions = "Без дисклеймеров."
    svc = Service(cfg, source=FakeSource(), llm=None)
    await svc.start()
    try:
        assert svc.processor.instructions == "Без дисклеймеров."
        await svc.set_instructions("Из бота")
        assert svc.processor.instructions == "Из бота"
        assert await svc.set_instructions(None) == "Без дисклеймеров."
    finally:
        await svc.close()
    svc2 = Service(cfg, source=FakeSource(), llm=None)
    await svc2.start()
    try:
        assert svc2.processor.instructions == "Без дисклеймеров."  # снова из .env
    finally:
        await svc2.close()
