"""Хранилище на SQLite.

Каналы и посты общие: каждый канал читается и обрабатывается один раз, даже
если на него подписано несколько пользователей. У каждого пользователя свои
подписки (теги, приоритет, пауза) и свой «курсор» — до какого поста канала
он уже получил дайджест.
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Iterable

import aiosqlite

from .models import Channel, Post, RawPost

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '',          -- устарело (до v0.3 каналы были общие)
    priority INTEGER NOT NULL DEFAULT 2,    -- устарело
    active INTEGER NOT NULL DEFAULT 1,      -- устарело
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
    digest_id INTEGER,                      -- устарело
    UNIQUE(channel_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
CREATE INDEX IF NOT EXISTS idx_posts_posted ON posts(posted_at);

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,      -- получает дайджест
    token TEXT NOT NULL UNIQUE,             -- секрет для личных веб/RSS-ссылок
    schedule TEXT NOT NULL DEFAULT '',      -- часы рассылки "6,9,21"; пусто — по умолчанию
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    tags TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 2,
    active INTEGER NOT NULL DEFAULT 1,
    cursor INTEGER NOT NULL DEFAULT 0,      -- последний msg_id, попавший в дайджест
    added_at TEXT NOT NULL,
    PRIMARY KEY (user_id, channel_id)
);
CREATE INDEX IF NOT EXISTS idx_subs_channel ON subscriptions(channel_id);

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


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _split(value: str) -> list[str]:
    return [t for t in (value or "").split(",") if t]


def _channel(row: aiosqlite.Row) -> Channel:
    """Канал без пользовательских настроек."""
    return Channel(id=row["id"], ref=row["ref"], title=row["title"], last_post_id=row["last_post_id"])


def _user_channel(row: aiosqlite.Row) -> Channel:
    """Канал глазами пользователя: теги, приоритет, пауза и курсор из подписки."""
    return Channel(
        id=row["id"],
        ref=row["ref"],
        title=row["title"],
        last_post_id=row["last_post_id"],
        tags=_split(row["s_tags"]),
        priority=row["s_priority"],
        active=bool(row["s_active"]),
        cursor=row["s_cursor"],
        subscribed_at=_dt(row["s_added_at"]),
    )


def _post(row: aiosqlite.Row) -> Post:
    return Post(
        id=row["id"],
        channel_id=row["channel_id"],
        msg_id=row["msg_id"],
        url=row["url"],
        posted_at=_dt(row["posted_at"]),
        text=row["text"],
        media=_split(row["media"]),
        status=row["status"],
        is_ad=bool(row["is_ad"]),
        summary=row["summary"],
        summarized=bool(row["summarized"]),
    )


def _user(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "user_id": row["user_id"],
        "chat_id": row["chat_id"],
        "name": row["name"],
        "active": bool(row["active"]),
        "token": row["token"],
        "schedule": [int(h) for h in _split(row["schedule"])],
    }


USER_CHANNEL_SQL = (
    "SELECT c.*, s.tags AS s_tags, s.priority AS s_priority, s.active AS s_active,"
    " s.cursor AS s_cursor, s.added_at AS s_added_at"
    " FROM subscriptions s JOIN channels c ON c.id = s.channel_id"
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
        await self._migrate()
        await self._conn.commit()

    async def _columns(self, table: str) -> set[str]:
        async with self.conn.execute(f"PRAGMA table_info({table})") as cur:
            return {row[1] for row in await cur.fetchall()}

    async def _migrate(self) -> None:
        if "schedule" not in await self._columns("users"):
            await self.conn.execute("ALTER TABLE users ADD COLUMN schedule TEXT NOT NULL DEFAULT ''")
        if "user_id" not in await self._columns("digests"):
            await self.conn.execute("ALTER TABLE digests ADD COLUMN user_id INTEGER")
        # v0.2: таблица subscribers → users
        async with self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='subscribers'"
        ) as cur:
            legacy = await cur.fetchone()
        if legacy:
            async with self.conn.execute("SELECT * FROM subscribers") as cur:
                rows = await cur.fetchall()
            for row in rows:
                await self.conn.execute(
                    "INSERT OR IGNORE INTO users(user_id, chat_id, name, active, token, created_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (row["user_id"], row["chat_id"], row["name"], row["active"],
                     secrets.token_urlsafe(16), row["created_at"]),
                )
            await self.conn.execute("DROP TABLE subscribers")

    async def migrate_shared_channels(self, owner_id: int) -> int:
        """До v0.3 список каналов был общим — отдаём его владельцу (первому админу)."""
        async with self.conn.execute("SELECT COUNT(*) FROM subscriptions") as cur:
            (subs,) = await cur.fetchone()  # type: ignore[misc]
        if subs:
            return 0
        async with self.conn.execute("SELECT * FROM channels") as cur:
            rows = await cur.fetchall()
        if not rows:
            return 0
        await self.ensure_user(owner_id)
        for row in rows:
            # всё, что уже было в отправленных дайджестах, повторно не присылаем
            async with self.conn.execute(
                "SELECT COALESCE(MAX(msg_id), 0) FROM posts WHERE channel_id = ? AND digest_id IS NOT NULL",
                (row["id"],),
            ) as cur:
                (cursor,) = await cur.fetchone()  # type: ignore[misc]
            await self.conn.execute(
                "INSERT OR IGNORE INTO subscriptions(user_id, channel_id, tags, priority, active, cursor, added_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (owner_id, row["id"], row["tags"], row["priority"], row["active"], cursor, row["added_at"]),
            )
        await self.conn.commit()
        return len(rows)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not connected"
        return self._conn

    async def commit(self) -> None:
        await self.conn.commit()

    # ---------- пользователи ----------

    async def get_user(self, user_id: int) -> dict[str, Any] | None:
        async with self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        return _user(row) if row else None

    async def get_user_by_token(self, token: str) -> dict[str, Any] | None:
        async with self.conn.execute("SELECT * FROM users WHERE token = ?", (token,)) as cur:
            row = await cur.fetchone()
        return _user(row) if row else None

    async def subscribe(self, user_id: int, chat_id: int, name: str = "") -> bool:
        """Регистрирует пользователя / включает рассылку. True — если раньше не получал."""
        user = await self.get_user(user_id)
        if user is None:
            await self.conn.execute(
                "INSERT INTO users(user_id, chat_id, name, active, token, created_at) VALUES (?,?,?,1,?,?)",
                (user_id, chat_id, name, secrets.token_urlsafe(16), _now()),
            )
        else:
            await self.conn.execute(
                "UPDATE users SET chat_id = ?, active = 1,"
                " name = CASE WHEN ? != '' THEN ? ELSE name END WHERE user_id = ?",
                (chat_id, name, name, user_id),
            )
        await self.conn.commit()
        return not (user and user["active"])

    async def ensure_user(self, user_id: int) -> dict[str, Any]:
        """Создаёт пользователя, если его нет (не трогает тех, кто отписался)."""
        await self.conn.execute(
            "INSERT OR IGNORE INTO users(user_id, chat_id, active, token, created_at) VALUES (?,?,1,?,?)",
            (user_id, user_id, secrets.token_urlsafe(16), _now()),
        )
        await self.conn.commit()
        user = await self.get_user(user_id)
        assert user
        return user

    async def unsubscribe(self, user_id: int) -> None:
        await self.conn.execute("UPDATE users SET active = 0 WHERE user_id = ?", (user_id,))
        await self.conn.commit()

    async def set_schedule(self, user_id: int, hours: list[int]) -> None:
        await self.conn.execute(
            "UPDATE users SET schedule = ? WHERE user_id = ?",
            (",".join(str(h) for h in sorted(set(hours))), user_id),
        )
        await self.conn.commit()

    async def list_users(self, active_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM users"
        if active_only:
            sql += " WHERE active = 1"
        async with self.conn.execute(sql + " ORDER BY created_at") as cur:
            rows = await cur.fetchall()
        return [_user(r) for r in rows]

    # совместимость
    list_subscribers = list_users

    # ---------- каналы ----------

    async def get_channel(self, ref: str) -> Channel | None:
        async with self.conn.execute("SELECT * FROM channels WHERE ref = ?", (ref,)) as cur:
            row = await cur.fetchone()
        return _channel(row) if row else None

    async def get_or_create_channel(self, ref: str, title: str) -> Channel:
        await self.conn.execute(
            "INSERT OR IGNORE INTO channels(ref, title, added_at) VALUES (?,?,?)", (ref, title, _now())
        )
        if title:
            await self.conn.execute(
                "UPDATE channels SET title = ? WHERE ref = ? AND title = ''", (title, ref)
            )
        await self.conn.commit()
        channel = await self.get_channel(ref)
        assert channel
        return channel

    async def channels_to_collect(self) -> list[Channel]:
        """Каналы, на которые хоть кто-то активно подписан."""
        async with self.conn.execute(
            "SELECT * FROM channels c WHERE EXISTS ("
            " SELECT 1 FROM subscriptions s JOIN users u ON u.user_id = s.user_id"
            " WHERE s.channel_id = c.id AND s.active = 1) ORDER BY c.id"
        ) as cur:
            rows = await cur.fetchall()
        return [_channel(r) for r in rows]

    async def count_channels(self) -> int:
        async with self.conn.execute("SELECT COUNT(*) FROM channels") as cur:
            (n,) = await cur.fetchone()  # type: ignore[misc]
        return n

    # ---------- подписки пользователя ----------

    async def user_channels(self, user_id: int, active_only: bool = False) -> list[Channel]:
        sql = USER_CHANNEL_SQL + " WHERE s.user_id = ?"
        if active_only:
            sql += " AND s.active = 1"
        sql += " ORDER BY s.priority, s.tags = '', s.tags, c.title, c.ref"
        async with self.conn.execute(sql, (user_id,)) as cur:
            rows = await cur.fetchall()
        return [_user_channel(r) for r in rows]

    async def user_channel(self, user_id: int, ref: str) -> Channel | None:
        async with self.conn.execute(
            USER_CHANNEL_SQL + " WHERE s.user_id = ? AND c.ref = ?", (user_id, ref)
        ) as cur:
            row = await cur.fetchone()
        return _user_channel(row) if row else None

    async def add_subscription(
        self, user_id: int, channel: Channel, tags: list[str], priority: int
    ) -> tuple[Channel, bool]:
        existing = await self.user_channel(user_id, channel.ref)
        if existing:
            return existing, False
        await self.conn.execute(
            "INSERT INTO subscriptions(user_id, channel_id, tags, priority, added_at) VALUES (?,?,?,?,?)",
            (user_id, channel.id, ",".join(tags), priority, _now()),
        )
        await self.conn.commit()
        created = await self.user_channel(user_id, channel.ref)
        assert created
        return created, True

    async def update_subscription(self, user_id: int, channel_id: int, **fields: Any) -> None:
        allowed = {"tags", "priority", "active", "cursor"}
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
        values += [user_id, channel_id]
        await self.conn.execute(
            f"UPDATE subscriptions SET {', '.join(sets)} WHERE user_id = ? AND channel_id = ?", values
        )
        await self.conn.commit()

    async def remove_subscription(self, user_id: int, channel_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM subscriptions WHERE user_id = ? AND channel_id = ?", (user_id, channel_id)
        )
        # канал больше никому не нужен — удаляем вместе с постами
        await self.conn.execute(
            "DELETE FROM channels WHERE id = ? AND NOT EXISTS"
            " (SELECT 1 FROM subscriptions WHERE channel_id = ?)",
            (channel_id, channel_id),
        )
        await self.conn.commit()

    async def advance_cursors(self, user_id: int, cursors: dict[int, int]) -> None:
        await self.conn.executemany(
            "UPDATE subscriptions SET cursor = MAX(cursor, ?) WHERE user_id = ? AND channel_id = ?",
            [(msg_id, user_id, channel_id) for channel_id, msg_id in cursors.items()],
        )
        await self.conn.commit()

    # ---------- посты ----------

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
                    _iso(p.posted_at),
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
        return [_post(r) for r in rows]

    async def save_processing(self, post: Post) -> None:
        await self.conn.execute(
            "UPDATE posts SET status = 'processed', is_ad = ?, summary = ?, summarized = ?"
            " WHERE id = ?",
            (int(post.is_ad), post.summary, int(post.summarized), post.id),
        )

    async def pending_for_user(
        self, user_id: int, since: datetime, first_fetch_hours: int
    ) -> list[tuple[Post, Channel]]:
        """Посты подписок пользователя, которых ещё не было в его дайджестах."""
        from datetime import timedelta

        channels = {c.id: c for c in await self.user_channels(user_id, active_only=True)}
        if not channels:
            return []
        async with self.conn.execute(
            "SELECT p.* FROM posts p JOIN subscriptions s"
            " ON s.channel_id = p.channel_id AND s.user_id = ?"
            " WHERE s.active = 1 AND p.msg_id > s.cursor AND p.posted_at >= ?"
            " ORDER BY p.posted_at",
            (user_id, _iso(since)),
        ) as cur:
            rows = await cur.fetchall()
        result = []
        for row in rows:
            post = _post(row)
            channel = channels.get(post.channel_id)
            if channel is None:
                continue
            # только что подписался — берём посты не старше FIRST_FETCH_HOURS до подписки
            if channel.subscribed_at and post.posted_at < channel.subscribed_at - timedelta(
                hours=first_fetch_hours
            ):
                continue
            result.append((post, channel))
        return result

    async def reset_recent(self, since: datetime) -> int:
        """Возвращает в очередь на обработку недавние посты (для /reprocess)."""
        cur = await self.conn.execute(
            "UPDATE posts SET status = 'new', is_ad = 0, summary = '', summarized = 0"
            " WHERE status = 'processed' AND posted_at >= ?",
            (_iso(since),),
        )
        await self.conn.commit()
        return cur.rowcount

    async def recent_posts_for_user(
        self, user_id: int, limit: int, tag: str | None = None
    ) -> list[tuple[Post, Channel]]:
        channels = {c.id: c for c in await self.user_channels(user_id)}
        if tag:
            channels = {k: c for k, c in channels.items() if tag in c.tags}
        if not channels:
            return []
        marks = ",".join("?" * len(channels))
        async with self.conn.execute(
            f"SELECT * FROM posts WHERE status = 'processed' AND is_ad = 0 AND channel_id IN ({marks})"
            " ORDER BY posted_at DESC LIMIT ?",
            (*channels.keys(), limit),
        ) as cur:
            rows = await cur.fetchall()
        return [(p, channels[p.channel_id]) for p in map(_post, rows)]

    async def cleanup(self, older_than: datetime) -> None:
        await self.conn.execute("DELETE FROM posts WHERE posted_at < ?", (_iso(older_than),))
        await self.conn.execute("DELETE FROM digests WHERE created_at < ?", (_iso(older_than),))
        await self.conn.commit()

    async def stats(self, user_id: int | None = None) -> dict[str, int]:
        result: dict[str, int] = {}
        queries: dict[str, tuple[str, tuple]] = {
            "channels_total": ("SELECT COUNT(*) FROM channels", ()),
            "users": ("SELECT COUNT(*) FROM users WHERE active = 1", ()),
            "posts_new": ("SELECT COUNT(*) FROM posts WHERE status = 'new'", ()),
            "ads_total": ("SELECT COUNT(*) FROM posts WHERE is_ad = 1", ()),
        }
        if user_id is not None:
            queries.update(
                {
                    "channels": ("SELECT COUNT(*) FROM subscriptions WHERE user_id = ?", (user_id,)),
                    "active_channels": (
                        "SELECT COUNT(*) FROM subscriptions WHERE user_id = ? AND active = 1",
                        (user_id,),
                    ),
                    "posts_pending": (
                        "SELECT COUNT(*) FROM posts p JOIN subscriptions s ON s.channel_id = p.channel_id"
                        " WHERE s.user_id = ? AND s.active = 1 AND p.msg_id > s.cursor AND p.is_ad = 0",
                        (user_id,),
                    ),
                    "digests": ("SELECT COUNT(*) FROM digests WHERE user_id = ?", (user_id,)),
                }
            )
        for key, (sql, params) in queries.items():
            async with self.conn.execute(sql, params) as cur:
                row = await cur.fetchone()
            result[key] = row[0] if row else 0
        return result

    # ---------- дайджесты ----------

    async def save_digest(self, user_id: int, title: str, html: str, stats: dict[str, Any]) -> int:
        cur = await self.conn.execute(
            "INSERT INTO digests(user_id, created_at, title, html, stats) VALUES (?,?,?,?,?)",
            (user_id, _now(), title, html, json.dumps(stats, ensure_ascii=False)),
        )
        await self.conn.commit()
        return int(cur.lastrowid)

    async def list_digests(self, user_id: int, limit: int) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM digests WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit)
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
            "user_id": row["user_id"],
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
