"""Веб-интерфейс: архив дайджестов и RSS-ленты."""

from __future__ import annotations

import logging
import secrets
from datetime import datetime
from email.utils import format_datetime
from urllib.parse import quote
from xml.sax.saxutils import escape as xml_escape

from aiohttp import web

from .digest import esc, esc_attr, human_date, media_label
from .service import Service

log = logging.getLogger(__name__)

PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="alternate" type="application/rss+xml" title="Дайджесты" href="{rss}">
<style>
:root {{ --bg:#fafaf7; --fg:#1d1d1b; --muted:#6b6b66; --link:#1a5fb4; --card:#fff; --line:#e6e4dc; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#16171a; --fg:#e8e6e1; --muted:#9a9890; --link:#78aeed; --card:#1f2024; --line:#2e2f34; }}
}}
body {{ background:var(--bg); color:var(--fg); font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;
  margin:0; padding:24px 16px; }}
main {{ max-width:760px; margin:0 auto; }}
a {{ color:var(--link); }}
.digest {{ white-space:pre-wrap; background:var(--card); border:1px solid var(--line);
  border-radius:12px; padding:20px; overflow-wrap:anywhere; }}
.muted {{ color:var(--muted); font-size:14px; }}
ul {{ padding-left:20px; }} li {{ margin:6px 0; }}
</style></head>
<body><main>{body}</main></body></html>"""


def _rfc822(dt: datetime) -> str:
    return format_datetime(dt)


def _rss(title: str, link: str, description: str, items: list[dict]) -> str:
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        f"<title>{xml_escape(title)}</title>",
        f"<link>{xml_escape(link)}</link>",
        f"<description>{xml_escape(description)}</description>",
        "<language>ru</language>",
    ]
    for item in items:
        out.append("<item>")
        out.append(f"<title>{xml_escape(item['title'])}</title>")
        out.append(f"<link>{xml_escape(item['link'])}</link>")
        out.append(f"<guid isPermaLink=\"false\">{xml_escape(item['guid'])}</guid>")
        out.append(f"<pubDate>{_rfc822(item['date'])}</pubDate>")
        if item.get("category"):
            for cat in item["category"]:
                out.append(f"<category>{xml_escape(cat)}</category>")
        out.append(f"<description>{xml_escape(item['description'])}</description>")
        out.append("</item>")
    out += ["</channel>", "</rss>"]
    return "\n".join(out)


def build_app(service: Service) -> web.Application:
    """Маршруты:

    /u/<token>/                    личный архив дайджестов пользователя
    /u/<token>/digest/<id>         дайджест в браузере
    /u/<token>/rss/digests.xml     RSS дайджестов
    /u/<token>/rss/posts.xml[?tag] RSS постов из подписок пользователя
    /                              без бота — архив владельца; с WEB_TOKEN — список пользователей
    /rss/*.xml, /digest/<id>       то же для владельца (режим без бота или с WEB_TOKEN)
    """
    cfg = service.cfg
    db = service.db

    def admin_ok(request: web.Request) -> bool:
        token = request.query.get("token") or request.headers.get("X-Token", "")
        return bool(cfg.web_token) and secrets.compare_digest(token, cfg.web_token)

    def owner_access(request: web.Request) -> bool:
        if cfg.web_token:
            return admin_ok(request)
        return not cfg.bot_token  # режим только RSS без токена — открыто, как раньше

    async def user_from(request: web.Request) -> tuple[dict, str]:
        """(пользователь, базовый путь для ссылок)"""
        token = request.match_info.get("token")
        if token is not None:
            user = await db.get_user_by_token(token)
            if not user or user["user_id"] not in service_access():
                raise web.HTTPNotFound()
            return user, f"/u/{token}"
        if not owner_access(request):
            if cfg.web_token:
                raise web.HTTPUnauthorized(text="token required")
            raise web.HTTPNotFound(text="Личные ссылки — в боте, команда /rss")
        user = await db.ensure_user(service.owner_id)
        return user, f"/u/{user['token']}"

    def service_access() -> set[int]:
        allowed = set(cfg.access_ids)
        allowed.add(service.owner_id)
        return allowed

    def absolute(path: str) -> str:
        return f"{cfg.web_base_url}{path}"

    def html_response(title: str, body: str, rss: str) -> web.Response:
        page = PAGE.format(title=esc(title), rss=esc_attr(rss), body=body)
        return web.Response(text=page, content_type="text/html")

    async def healthz(_: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def archive(request: web.Request) -> web.Response:
        user, base = await user_from(request)
        digests = await db.list_digests(user["user_id"], 60)
        items = "".join(
            f'<li><a href="{base}/digest/{d["id"]}">{esc(d["title"])}</a> '
            f'<span class="muted">· постов {d["stats"].get("posts", 0)}</span></li>'
            for d in digests
        ) or "<li class='muted'>Дайджестов пока нет</li>"
        channels = await db.user_channels(user["user_id"])
        tags = sorted({t for c in channels for t in c.tags})
        tag_links = " ".join(
            f'<a href="{base}/rss/posts.xml?tag={esc_attr(quote(t))}">#{esc(t)}</a>' for t in tags
        )
        body = (
            "<h1>Дайджесты</h1>"
            f'<p class="muted">RSS: <a href="{base}/rss/digests.xml">дайджесты</a> · '
            f'<a href="{base}/rss/posts.xml">все посты</a>'
            + (f" · по тегам: {tag_links}" if tag_links else "")
            + f"</p><ul>{items}</ul>"
        )
        return html_response("Дайджесты", body, f"{base}/rss/digests.xml")

    async def index(request: web.Request) -> web.Response:
        if cfg.bot_token and admin_ok(request):
            known = {u["user_id"]: u for u in await db.list_users(active_only=False)}
            rows = []
            for uid in sorted(service_access()):
                user = known.get(uid)
                if not user:
                    continue
                name = esc(user["name"] or str(uid))
                rows.append(f'<li><a href="/u/{user["token"]}/">{name}</a></li>')
            body = "<h1>Пользователи</h1><ul>" + ("".join(rows) or "<li>нет</li>") + "</ul>"
            return html_response("Пользователи", body, "")
        if not cfg.bot_token and owner_access(request):
            return await archive(request)
        if cfg.web_token and not admin_ok(request):
            raise web.HTTPUnauthorized(text="token required")
        body = (
            "<h1>Дайджест Telegram-каналов</h1>"
            '<p class="muted">Личные ссылки на архив и RSS выдаёт бот командой /rss.</p>'
        )
        return html_response("Дайджест", body, "")

    async def digest_page(request: web.Request) -> web.Response:
        user, base = await user_from(request)
        try:
            digest_id = int(request.match_info["id"])
        except ValueError:
            raise web.HTTPNotFound()
        digest = await db.get_digest(digest_id)
        if not digest or digest["user_id"] != user["user_id"]:
            raise web.HTTPNotFound()
        body = (
            f'<p class="muted"><a href="{base}/">← все дайджесты</a></p>'
            f'<div class="digest">{digest["html"]}</div>'
        )
        return html_response(digest["title"], body, f"{base}/rss/digests.xml")

    async def rss_digests(request: web.Request) -> web.Response:
        user, base = await user_from(request)
        digests = await db.list_digests(user["user_id"], cfg.rss_items)
        items = [
            {
                "title": d["title"],
                "link": absolute(f"{base}/digest/{d['id']}"),
                "guid": f"digest-{d['id']}",
                "date": d["created_at"],
                "description": d["html"].replace("\n", "<br>\n"),
            }
            for d in digests
        ]
        xml = _rss("Дайджест Telegram-каналов", absolute(f"{base}/"), "Дайджесты моих каналов", items)
        return web.Response(text=xml, content_type="application/rss+xml", charset="utf-8")

    async def rss_posts(request: web.Request) -> web.Response:
        user, base = await user_from(request)
        tag = (request.query.get("tag") or "").strip().lstrip("#").lower() or None
        await service.processor.process_pending()
        rows = await db.recent_posts_for_user(user["user_id"], cfg.rss_items, tag=tag)
        items = []
        for post, channel in rows:
            if post.media_only:
                text = f"Только медиа ({media_label(post.media)})"
            else:
                text = post.summary if post.summarized and post.summary else post.text
            first_line = text.strip().split("\n")[0]
            title = f"{channel.display_name}: {first_line[:90]}{'…' if len(first_line) > 90 else ''}"
            desc = esc(text).replace("\n", "<br>\n")
            desc += f'<br><br><a href="{esc_attr(post.url)}">Открыть пост</a> · {esc(channel.display_name)}'
            items.append(
                {
                    "title": title,
                    "link": post.url,
                    "guid": post.url,
                    "date": post.posted_at,
                    "category": channel.tags,
                    "description": desc,
                }
            )
        feed_title = "Посты Telegram-каналов" + (f" · #{tag}" if tag else "")
        xml = _rss(
            feed_title,
            absolute(f"{base}/"),
            feed_title + f" (обновлено {human_date(datetime.now(cfg.tz))})",
            items,
        )
        return web.Response(text=xml, content_type="application/rss+xml", charset="utf-8")

    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/", index)
    app.router.add_get("/digest/{id}", digest_page)
    app.router.add_get("/rss/digests.xml", rss_digests)
    app.router.add_get("/rss/posts.xml", rss_posts)
    app.router.add_get("/u/{token}", archive)
    app.router.add_get("/u/{token}/", archive)
    app.router.add_get("/u/{token}/digest/{id}", digest_page)
    app.router.add_get("/u/{token}/rss/digests.xml", rss_digests)
    app.router.add_get("/u/{token}/rss/posts.xml", rss_posts)
    return app


async def start_web(service: Service) -> web.AppRunner:
    app = build_app(service)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, service.cfg.web_host, service.cfg.web_port)
    await site.start()
    log.info("Веб/RSS: http://%s:%s", service.cfg.web_host, service.cfg.web_port)
    return runner
