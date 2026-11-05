from tgchanreader.llm import extract_json
from tgchanreader.processor import heuristic_is_ad, truncate


def test_heuristic_ads():
    assert heuristic_is_ad("Лучший курс! Реклама. ООО Ромашка erid: 2SDnjdLmnoP")
    assert heuristic_is_ad("Скидки #реклама")
    assert heuristic_is_ad("Реклама: подпишитесь на канал")
    assert not heuristic_is_ad("Сегодня обсуждали рекламный рынок в России")
    assert not heuristic_is_ad("Новости велоспорта")


def test_truncate():
    text = "Первое предложение. Второе предложение длиннее. Третье."
    assert truncate(text, 100) == text
    out = truncate(text, 45)
    assert out.endswith("…")
    assert len(out) <= 47


def test_extract_json():
    assert extract_json('```json\n{"posts": []}\n```') == {"posts": []}
    assert extract_json('Вот ответ: {"a": 1} спасибо') == {"a": 1}
    assert extract_json('[1, 2]') == [1, 2]
