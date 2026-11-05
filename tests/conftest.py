from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tgchanreader.config import Config
from tgchanreader.db import Database
from tgchanreader.models import Channel, RawPost
from tgchanreader.sources import ChannelInfo, Source


UID = 1


class FakeSource(Source):
    name = "fake"

    def __init__(self):
        self.posts: dict[str, list[RawPost]] = {}

    async def resolve(self, ref: str) -> ChannelInfo:
        return ChannelInfo(ref=ref, title=f"Title {ref}")

    async def fetch(self, channel: Channel, since: datetime) -> list[RawPost]:
        return [
            p for p in self.posts.get(channel.ref, [])
            if p.msg_id > channel.last_post_id and p.posted_at >= since
        ]


class FakeLLM:
    paused = False
    last_error = ""

    def reset(self):
        pass

    def __init__(self, ads: set[str] | None = None):
        self.ads = ads or set()
        self.calls = 0

    async def complete_json(self, system, user, max_tokens=2000):
        import json

        self.calls += 1
        data = json.loads(user)
        out = []
        for item in data["posts"]:
            out.append(
                {
                    "id": item["id"],
                    "is_ad": any(word in item["text"] for word in self.ads),
                    "summary": "Краткое содержание." if item["need_summary"] else "",
                }
            )
        return {"posts": out}

    async def complete(self, system, user, max_tokens=2000):
        return "• Пункт один\n• Пункт два\n- Пункт три"

    async def close(self):
        pass


@pytest.fixture
def cfg(tmp_path) -> Config:
    c = Config(data_dir=str(tmp_path), timezone="Europe/Moscow")
    c.llm.provider = "none"
    return c


@pytest.fixture
async def db(cfg):
    database = Database(cfg.db_path)
    await database.connect()
    yield database
    await database.close()


def now() -> datetime:
    return datetime.now(timezone.utc)
