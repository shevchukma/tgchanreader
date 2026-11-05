import pytest

from tgchanreader.models import Channel, normalize_ref, parse_tags


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
