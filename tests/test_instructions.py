import json

import pytest

from tgchanreader.models import RawPost
from tgchanreader.processor import strip_disclaimers
from tgchanreader.service import Service

from .conftest import UID, FakeLLM, FakeSource, now

FA_OLD = (
    "ДАННОЕ СООБЩЕНИЕ (МАТЕРИАЛ) СОЗДАНО И (ИЛИ) РАСПРОСТРАНЕНО ИНОСТРАННЫМ СРЕДСТВОМ МАССОВОЙ "
    "ИНФОРМАЦИИ, ВЫПОЛНЯЮЩИМ ФУНКЦИИ ИНОСТРАННОГО АГЕНТА, И (ИЛИ) РОССИЙСКИМ ЮРИДИЧЕСКИМ ЛИЦОМ, "
    "ВЫПОЛНЯЮЩИМ ФУНКЦИИ ИНОСТРАННОГО АГЕНТА."
)
FA_NEW = (
    "НАСТОЯЩИЙ МАТЕРИАЛ (ИНФОРМАЦИЯ) ПРОИЗВЕДЕН, РАСПРОСТРАНЕН И (ИЛИ) НАПРАВЛЕН ИНОСТРАННЫМ АГЕНТОМ "
    "ИВАНОВЫМ ИВАНОМ ИВАНОВИЧЕМ ЛИБО КАСАЕТСЯ ДЕЯТЕЛЬНОСТИ ИНОСТРАННОГО АГЕНТА ИВАНОВА ИВАНА ИВАНОВИЧА. 18+"
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (f"Открыли мост.\n\n{FA_OLD}\n18+", "Открыли мост."),
        (f"{FA_NEW}\nПогода хорошая.", "Погода хорошая."),
        ("Текст. НАСТОЯЩИЙ МАТЕРИАЛ ПРОИЗВЕДЕН ИНОСТРАННЫМ АГЕНТОМ ПЕТРОВЫМ. Дальше.", "Текст. Дальше."),
        ("Данное сообщение (материал) создано иностранным агентом Сидоровым.\nТекст", "Текст"),
        (FA_OLD, ""),
        # реальные примеры
        (
            "Текст поста.\n\n18+ НАСТОЯЩИЙ МАТЕРИАЛ (ИНФОРМАЦИЯ) ПРОИЗВЕДЕН, РАСПРОСТРАНЕН ИЛИ НАПРАВЛЕН "
            "ИНОСТРАННЫМ АГЕНТОМ МИХНОВЫМ-ВАЙТЕНКО ГРИГОРИЕМ АЛЕКСАНДРОВИЧЕМ, ЛИБО КАСАЕТСЯ ДЕЯТЕЛЬНОСТИ "
            "ИНОСТРАННОГО АГЕНТА МИХНОВА-ВАЙТЕНКО ГРИГОРИЯ АЛЕКСАНДРОВИЧА \n(Архиепископа Григория) ",
            "Текст поста.",
        ),
        (
            "18+ НАСТОЯЩИЙ МАТЕРИАЛ ПРОИЗВЕДЕН, РАСПРОСТРАНЕН ИНОСТРАННЫМ АГЕНТОМ ШТЕФАНОВЫМ АЛЕКСАНДРОМ "
            "АНДРЕЕВИЧЕМ\n\nПост про путешествия.",
            "Пост про путешествия.",
        ),
        (
            "Пост.\n\nНАСТОЯЩИЙ МАТЕРИАЛ (ИНФОРМАЦИЯ) ПРОИЗВЕДЕН, РАСПРОСТРАНЕН И (ИЛИ) НАПРАВЛЕН ИНОСТРАННЫМ "
            "АГЕНТОМ ШУЛЬМАН ЕКАТЕРИНОЙ МИХАЙЛОВНОЙ, ЛИБО КАСАЕТСЯ ДЕЯТЕЛЬНОСТИ ИНОСТРАННОГО АГЕНТА ШУЛЬМАН "
            "ЕКАТЕРИНЫ МИХАЙЛОВНЫ",
            "Пост.",
        ),
        ("18+ Данное сообщение создано иностранным агентом Ивановым.\nТекст", "Текст"),
        # скобки в обычном тексте (не после плашки) остаются
        ("Текст\n(важное уточнение)", None),
        # новости про иноагентов не трогаем
        ("Минюст внёс журналиста в реестр иностранных агентов.", None),
        ("СРОЧНО: МИНЮСТ ПРИЗНАЛ ИНОСТРАННЫМ АГЕНТОМ ЖУРНАЛИСТА", None),
        ("Обычный пост", None),
    ],
)
def test_strip_disclaimers(raw, expected):
    assert strip_disclaimers(raw) == (raw if expected is None else expected)


class RulesLLM(FakeLLM):
    def __init__(self):
        super().__init__()
        self.systems: list[str] = []

    async def complete_json(self, system, user, max_tokens=2000):
        self.systems.append(system)
        data = await super().complete_json(system, user, max_tokens)
        if '"clean"' in system:
            for row, item in zip(data["posts"], json.loads(user)["posts"]):
                if not item["need_summary"] and "Подписывайтесь" in item["text"]:
                    row["clean"] = item["text"].replace(" Подписывайтесь!", "")
        return data

    async def complete(self, system, user, max_tokens=2000):
        self.systems.append(system)
        return await super().complete(system, user, max_tokens)


async def test_instructions_applied(cfg):
    llm = RulesLLM()
    svc = Service(cfg, source=FakeSource(), llm=llm)
    await svc.start()
    try:
        await svc.set_instructions("Не добавляй дисклеймеры. Убирай призывы подписаться.")
        await svc.add_channel(UID, "@chan_one")
        svc.source.posts["chan_one"] = [
            RawPost(1, now(), "Мост открыт. Подписывайтесь!"),
            RawPost(2, now(), f"Погода хорошая.\n\n{FA_OLD}\n18+"),
            RawPost(3, now(), FA_OLD),  # пост только из плашки — пропадает
        ]
        await svc.collect()
        assert all("Убирай призывы подписаться" in s for s in llm.systems)
        digest = await svc.builder.build(UID)
        text = digest.html
        assert "Мост открыт." in text and "Подписывайтесь" not in text
        assert "Погода хорошая." in text and "ИНОСТРАН" not in text
        assert digest.stats["posts"] == 2
    finally:
        await svc.close()

    # указания сохраняются между перезапусками; «пусто» перекрывает LLM_INSTRUCTIONS
    cfg.llm_instructions = "из .env"
    svc2 = Service(cfg, source=FakeSource(), llm=None)
    await svc2.start()
    try:
        assert svc2.processor.instructions.startswith("Не добавляй")
        await svc2.set_instructions("")
    finally:
        await svc2.close()
    svc3 = Service(cfg, source=FakeSource(), llm=None)
    await svc3.start()
    try:
        assert svc3.processor.instructions == ""
    finally:
        await svc3.close()


async def test_no_clean_field_without_instructions(cfg):
    llm = RulesLLM()
    svc = Service(cfg, source=FakeSource(), llm=llm)
    await svc.start()
    try:
        await svc.add_channel(UID, "@chan_one")
        svc.source.posts["chan_one"] = [RawPost(1, now(), "Мост открыт. Подписывайтесь!")]
        await svc.collect()
        assert llm.systems and all('"clean"' not in s for s in llm.systems)
    finally:
        await svc.close()
