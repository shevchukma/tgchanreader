from pathlib import Path

from tgchanreader.sources.web import parse_channel_page

HTML = (Path(__file__).parent / "fixtures" / "channel_page.html").read_text(encoding="utf-8")


def test_parse_channel_page():
    title, posts = parse_channel_page(HTML, "testchan")
    assert title == "Тестовый канал"
    assert [p.msg_id for p in posts] == [100, 102, 103]  # служебное сообщение пропущено

    first = posts[0]
    assert first.text == "Первый пост\nвторая строка ссылка"
    assert first.media == []
    assert first.url == "https://t.me/testchan/100"
    assert first.posted_at.isoformat() == "2026-11-04T09:15:00+00:00"

    photo = posts[1]
    assert photo.text == ""
    assert photo.media == ["photo"]

    video = posts[2]
    assert video.text == "Видео с подписью"  # цитата из reply не попала
    assert video.media == ["video"]
