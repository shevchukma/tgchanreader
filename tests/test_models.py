import pytest

from tgchanreader.models import Channel, find_refs, normalize_ref, parse_tags


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("@Example_News", "example_news"),
        ("example_news", "example_news"),
        ("https://t.me/example_news", "example_news"),
        ("t.me/s/example_news", "example_news"),
        ("https://t.me/example_news/12345", "example_news"),
        ("https://t.me/c/1234567/89", "-1001234567"),
        ("-1001234567", "-1001234567"),
        ("https://t.me/example_news/123?single", "example_news"),
        ("https://t.me/s/example_news?before=10", "example_news"),
        ("https://t.me/example_news.", "example_news"),
        ("<https://t.me/example_news>", "example_news"),
        ("https://example_news.t.me", "example_news"),
        ("tg://resolve?domain=example_news&post=5", "example_news"),
        ("https://t.me/boost/example_news", "example_news"),
        ("https://t.me/+AbCdEf12345", "+AbCdEf12345"),
        ("https://t.me/joinchat/AbCdEf12345", "+AbCdEf12345"),
        ("https://t.me/share/url?url=x", None),
        ("https://t.me/addstickers/pack", None),
        ("https://t.me/joinchat", None),
        ("https://example.com/news", None),
        ("", None),
        ("@a", None),
    ],
)
def test_normalize_ref(raw, expected):
    assert normalize_ref(raw) == expected


def test_parse_tags():
    assert parse_tags("#Политика, велосипед;политика  IT") == ["политика", "велосипед", "it"]


def test_channel_links():
    pub = Channel(id=1, ref="example_blog")
    assert pub.post_url(5) == "https://t.me/example_blog/5"
    priv = Channel(id=2, ref="-1001234567")
    assert priv.is_private
    assert priv.post_url(5) == "https://t.me/c/1234567/5"


def test_find_refs():
    text = "Каналы: https://t.me/aaaa_news, @bbbb_velo и t.me/+AbCdEf12345. Почта a@mail.ru, ещё раз @bbbb_velo"
    assert find_refs(text) == ["aaaa_news", "bbbb_velo", "+AbCdEf12345"]
    assert find_refs("просто текст") == []
