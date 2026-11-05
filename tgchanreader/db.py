"""Хранилище на SQLite."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Iterable

import aiosqlite

from .models import Channel, Post, RawPost

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 2,
    active INTEGER NOT NULL DEFAULT 1,
    last_post_id INTEGER NOT NULL DEFAULT 0,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    msg_id INTEGER NOT NULL,
    url TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    media TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    is_ad INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT '',
    summarized INTEGER NOT NULL DEFAULT 0,
    digest_id INTEGER,
    UNIQUE(channel_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
CREATE INDEX IF NOT EXISTS idx_posts_digest ON posts(digest_id);
CREATE INDEX IF NOT EXISTS idx_posts_posted ON posts(posted_at);

CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    title TEXT NOT NULL,
    html TEXT NOT NULL,
    stats TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _row_to_channel(row: aiosqlite.Row) -> Channel:
    return Channel(
        id=row["id"],
        ref=row["ref"],
        title=row["title"],
        tags=[t for t in row["tags"].split(",") if t],
        priority=row["priority"],
        active=bool(row["active"]),
        last_post_id=row["last_post_id"],
    )


def _row_to_post(row: aiosqlite.Row) -> Post:
    return Post(
        id=row["id"],
        channel_id=row["channel_id"],
        msg_id=row["msg_id"],
        url=row["url"],
        posted_at=_dt(row["posted_at"]),
        text=row["text"],
        media=[m for m in row["media"].split(",") if m],
        status=row["status"],
        is_ad=bool(row["is_ad"]),
        summary=row["summary"],
        summarized=bool(row["summarized"]),
        digest_id=row["digest_id"],
    )


class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not connected"
        return self._conn

    # ---------- channels ----------

    async def add_channel(
        self, ref: str, title: str, tags: list[str], priority: int
    ) -> tuple[Channel, bool]:
        """Возвращает (канал, создан_ли_новый)."""
        existing = await self.get_channel(ref)
        if existing:
            return existing, False
        await self.conn.execute(
            "INSERT INTO channels(ref, title, tags, priority, added_at) VALUES (?,?,?,?,?)",
            (ref, title, ",".join(tags), priority, _now()),
        )
        await self.conn.commit()
        channel = await self.get_channel(ref)
        assert channel
        return channel, True

    async def get_channel(self, ref: str) -> Channel | None:
        async with self.conn.execute("SELECT * FROM channels WHERE ref = ?", (ref,)) as cur:
            row = await cur.fetchone()
        return _row_to_channel(row) if row else None

    async def get_channel_by_id(self, channel_id: int) -> Channel | None:
        async with self.conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_channel(row) if row else None

    async def list_channels(self, active_only: bool = False) -> list[Channel]:
        sql = "SELECT * FROM channels"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY priority, tags, title, ref"
        async with self.conn.execute(sql) as cur:
            rows = await cur.fetchall()
        return [_row_to_channel(r) for r in rows]

    async def update_channel(self, channel_id: int, **fields: Any) -> None:
        allowed = {"title", "tags", "priority", "active", "last_post_id"}
        sets, values = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(key)
            if key == "tags" and isinstance(value, list):
                value = ",".join(value)
            if key == "active":
                value = int(bool(value))
            sets.append(f"{key} = ?")
            values.append(value)
        if not sets:
            return
        values.append(channel_id)
        await self.conn.execute(f"UPDATE channels SET {', '.join(sets)} WHERE id = ?", values)
        await self.conn.commit()

    async def remove_channel(self, channel_id: int) -> None:
        await self.conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
        await self.conn.commit()

    # ---------- posts ----------

    async def add_posts(self, channel: Channel, posts: Iterable[RawPost]) -> int:
        count = 0
        max_id = channel.last_post_id
        for p in posts:
            cur = await self.conn.execute(
                "INSERT OR IGNORE INTO posts(channel_id, msg_id, url, posted_at, text, media)"
                " VALUES (?,?,?,?,?,?)",
                (
                    channel.id,
                    p.msg_id,
                    p.url or channel.post_url(p.msg_id),
                    p.posted_at.astimezone(timezone.utc).isoformat(),
                    p.text,
                    ",".join(p.media),
                ),
            )
            count += cur.rowcount
            max_id = max(max_id, p.msg_id)
        if max_id != channel.last_post_id:
            await self.conn.execute(
                "UPDATE channels SET last_post_id = ? WHERE id = ?", (max_id, channel.id)
            )
            channel.last_post_id = max_id
        await self.conn.commit()
        return count

    async def posts_by_status(self, status: str) -> list[Post]:
        async with self.conn.execute(
            "SELECT * FROM posts WHERE status = ? ORDER BY channel_id, msg_id", (status,)
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_post(r) for r in rows]

    async def save_processing(self, post: Post) -> None:
        await self.conn.execute(
            "UPDATE posts SET status = 'processed', is_ad = ?, summary = ?, summarized = ?"
            " WHERE id = ?",
            (int(post.is_ad), post.summary, int(post.summarized), post.id),
        )

    async def commit(self) -> None:
        await self.conn.commit()

    async def undigested_posts(self, since: datetime) -> list[Post]:
        async with self.conn.execute(
            "SELECT p.* FROM posts p JOIN channels c ON c.id = p.channel_id"
            " WHERE p.digest_id IS NULL AND p.status = 'processed' AND p.posted_at >= ?"
            " ORDER BY p.posted_at",
            (since.astimezone(timezone.utc).isoformat(),),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_post(r) for r in rows]

    async def recent_posts(self, limit: int, tag: str | None = None) -> list[tuple[Post, Channel]]:
        async with self.conn.execute(
            "SELECT p.* FROM posts p WHERE p.status = 'processed' AND p.is_ad = 0"
            " ORDER BY p.posted_at DESC LIMIT ?",
            (limit * 5 if tag else limit,),
        ) as cur:
            rows = await cur.fetchall()
        result: list[tuple[Post, Channel]] = []
        cache: dict[int, Channel | None] = {}
        for row in rows:
            post = _row_to_post(row)
            if post.channel_id not in cache:
                cache[post.channel_id] = await self.get_channel_by_id(post.channel_id)
            channel = cache[post.channel_id]
            if channel is None:
                continue
            if tag and tag not in channel.tags:
                continue
            result.append((post, channel))
            if len(result) >= limit:
                break
        return result

    async def mark_digested(self, post_ids: list[int], digest_id: int) -> None:
        if not post_ids:
            return
        await self.conn.executemany(
            "UPDATE posts SET digest_id = ? WHERE id = ?", [(digest_id, pid) for pid in post_ids]
        )
        await self.conn.commit()

    async def cleanup(self, older_than: datetime) -> None:
        await self.conn.execute(
            "DELETE FROM posts WHERE posted_at < ?", (older_than.astimezone(timezone.utc).isoformat(),)
        )
        await self.conn.commit()

    async def stats(self) -> dict[str, int]:
        result: dict[str, int] = {}
        queries = {
            "channels": "SELECT COUNT(*) FROM channels",
            "active_channels": "SELECT COUNT(*) FROM channels WHERE active = 1",
            "posts_new": "SELECT COUNT(*) FROM posts WHERE status = 'new'",
            "posts_pending": "SELECT COUNT(*) FROM posts WHERE status = 'processed' AND digest_id IS NULL",
            "ads_total": "SELECT COUNT(*) FROM posts WHERE is_ad = 1",
            "digests": "SELECT COUNT(*) FROM digests",
        }
        for key, sql in queries.items():
            async with self.conn.execute(sql) as cur:
                row = await cur.fetchone()
            result[key] = row[0] if row else 0
        return result

    # ---------- digests ----------

    async def save_digest(self, title: str, html: str, stats: dict[str, Any]) -> int:
        cur = await self.conn.execute(
            "INSERT INTO digests(created_at, title, html, stats) VALUES (?,?,?,?)",
            (_now(), title, html, json.dumps(stats, ensure_ascii=False)),
        )
        await self.conn.commit()
        return int(cur.lastrowid)

    async def list_digests(self, limit: int) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM digests ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
        return [self._digest_row(r) for r in rows]

    async def get_digest(self, digest_id: int) -> dict[str, Any] | None:
        async with self.conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)) as cur:
            row = await cur.fetchone()
        return self._digest_row(row) if row else None

    @staticmethod
    def _digest_row(row: aiosqlite.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "created_at": _dt(row["created_at"]),
            "title": row["title"],
            "html": row["html"],
            "stats": json.loads(row["stats"] or "{}"),
        }

    # ---------- settings ----------

    async def get_setting(self, key: str, default: str = "") -> str:
        async with self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.conn.commit()
