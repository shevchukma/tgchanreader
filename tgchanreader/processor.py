"""Обработка постов: выявление рекламы и сокращение длинных текстов."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

from .config import Config
from .db import Database
from .llm import LLMClient, LLMError
from .models import Channel, Post

log = logging.getLogger(__name__)

# Маркировка рекламы по закону РФ (erid) и типичные хэштеги/фразы.
AD_PATTERNS = [
    re.compile(r"\berid\s*[:=]?\s*[A-Za-z0-9]{5,}", re.I),
    re.compile(r"#(реклама|промо|promo|ad|ads|sponsored|партнер|партнёр|партнерский|партнёрский)\b", re.I),
    re.compile(r"(^|\n)\s*реклама[\s.:,]", re.I),
    re.compile(r"на правах рекламы", re.I),
    re.compile(r"рекламодатель\s*:", re.I),
    re.compile(r"\bИНН\s*\d{10,12}\b.*реклам", re.I | re.S),
]


def heuristic_is_ad(text: str) -> bool:
    return any(p.search(text) for p in AD_PATTERNS)


def truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # обрезаем по границе предложения или слова
    for sep in (". ", "! ", "? ", "\n"):
        idx = cut.rfind(sep)
        if idx > limit * 0.6:
            return cut[: idx + 1].rstrip() + " …"
    idx = cut.rfind(" ")
    if idx > limit * 0.6:
        cut = cut[:idx]
    return cut.rstrip() + "…"


SYSTEM_PROMPT = """Ты помогаешь составлять ежедневный дайджест Telegram-каналов.
Для каждого поста определи:
1. is_ad — является ли пост рекламой: платная интеграция, продвижение товара/услуги/чужого канала,
   розыгрыш ради подписок, реферальные ссылки, промокоды, маркировка erid. Анонсы собственных
   материалов автора канала и обычные новости рекламой НЕ являются.
2. summary — краткий пересказ поста на языке: {language}.
   * Если у поста стоит "need_summary": true — сократи до 1–3 предложений
     (не длиннее {max_chars} символов), сохранив главные факты, цифры и выводы.
   * Если "need_summary": false — верни пустую строку "".
   * Без вступлений вроде «В посте говорится», сразу суть.
Ответь ТОЛЬКО JSON без пояснений в формате:
{{"posts": [{{"id": <id>, "is_ad": true|false, "summary": "..."}}]}}"""

OVERVIEW_PROMPT = """Ты редактор дайджеста Telegram-каналов. Тебе дан список
новостей за период (канал, теги, текст). Напиши блок «Главное за день»: 3–6 самых
важных пунктов, каждый — одно короткое предложение. Объединяй повторяющиеся
сюжеты из разных каналов. Язык: {language}. Ответь только списком пунктов, каждый
с новой строки, начиная с «• ». Без заголовка и без Markdown-разметки."""


COMPRESS_PROMPT = """Ты редактор дайджеста. Тебе даны посты одного Telegram-канала «{channel}».
Они не помещаются в одно сообщение, поэтому:
* выкинь несущественное: мелкие и проходные новости, повторы одного сюжета, анонсы,
  опросы, поздравления, оффтоп, посты «только медиа» без важного смысла;
* каждый оставшийся пост перескажи ОДНОЙ короткой фразой (не длиннее {line_chars} символов),
  сразу суть, без вступлений;
* суммарная длина всех фраз — не больше {budget} символов;
* важные новости не выкидывай; похожие посты можно оставить одним (выбери один id).
Язык: {language}.
Ответь ТОЛЬКО JSON, посты в порядке убывания важности:
{{"keep": [{{"id": <id>, "text": "..."}}]}}"""


@dataclass
class ProcessStats:
    processed: int = 0
    ads: int = 0
    summarized: int = 0
    llm_errors: int = 0
    deferred: int = 0


class Processor:
    def __init__(self, cfg: Config, db: Database, llm: LLMClient | None):
        self.cfg = cfg
        self.db = db
        self.llm = llm
        # одна обработка за раз: иначе параллельные вызовы отправят одни и те же посты в LLM
        self._lock = asyncio.Lock()

    def _needs_summary(self, post: Post) -> bool:
        return len(post.text) > self.cfg.summary_threshold

    def _fallback(self, post: Post) -> None:
        if self._needs_summary(post):
            post.summary = truncate(post.text, self.cfg.summary_max_chars)
            post.summarized = True

    def apply_fallback(self, post: Post) -> None:
        """Обработка без LLM: эвристики рекламы и обрезка (без записи в БД)."""
        post.is_ad = bool(post.text.strip()) and heuristic_is_ad(post.text)
        if not post.is_ad:
            self._fallback(post)

    async def process_pending(self, final: bool = False) -> ProcessStats:
        async with self._lock:
            return await self._process_pending(final)

    async def _process_pending(self, final: bool = False) -> ProcessStats:
        """Обрабатывает новые посты.

        Если LLM недоступен и final=False, посты остаются в очереди и будут
        обработаны нейросетью позже. При final=True (сборка дайджеста) для них
        используется упрощённая обработка, чтобы дайджест всё равно пришёл.
        """
        stats = ProcessStats()
        posts = await self.db.posts_by_status("new")
        if not posts:
            return stats

        to_llm: list[Post] = []
        done: list[Post] = []
        for post in posts:
            if post.media_only or not post.text.strip():
                post.is_ad = False
            elif heuristic_is_ad(post.text):
                post.is_ad = True
            elif self.llm:
                to_llm.append(post)
                continue
            else:
                self._fallback(post)
            await self.db.save_processing(post)
            done.append(post)
        await self.db.commit()

        if to_llm and self.llm and self.llm.paused and not final:
            stats.deferred = len(to_llm)
            to_llm = []

        if to_llm:
            batches = self._batches(to_llm)
            results = await asyncio.gather(
                *(self._process_batch(b) for b in batches), return_exceptions=True
            )
            errors: list[str] = []
            for batch, res in zip(batches, results):
                if isinstance(res, Exception):
                    stats.llm_errors += 1
                    errors.append(str(res))
                    if not final:
                        stats.deferred += len(batch)
                        continue
                    for post in batch:
                        self._fallback(post)
                for post in batch:
                    await self.db.save_processing(post)
                    done.append(post)
            await self.db.commit()
            if errors:
                action = (
                    "упрощённая обработка (обрезка текста)" if final
                    else "посты отложены до следующей попытки"
                )
                log.warning(
                    "LLM: ошибок в %d из %d пачек, %s. Первая ошибка: %s",
                    len(errors), len(batches), action, errors[0][:300],
                )

        for post in done:
            stats.processed += 1
            stats.ads += int(post.is_ad)
            stats.summarized += int(post.summarized)
        if stats.processed or stats.deferred:
            log.info(
                "Обработано постов: %d (реклама: %d, сокращено: %d), отложено: %d",
                stats.processed, stats.ads, stats.summarized, stats.deferred,
            )
        return stats

    def _batches(self, posts: list[Post]) -> list[list[Post]]:
        batches: list[list[Post]] = []
        current: list[Post] = []
        size = 0
        limit = self.cfg.llm.batch_max_chars
        for post in posts:
            length = min(len(post.text), limit)
            if current and (len(current) >= self.cfg.llm.batch_size or size + length > limit):
                batches.append(current)
                current, size = [], 0
            current.append(post)
            size += length
        if current:
            batches.append(current)
        return batches

    async def _process_batch(self, batch: list[Post]) -> None:
        assert self.llm
        items = [
            {
                "id": post.id,
                "need_summary": self._needs_summary(post),
                "text": post.text[: self.cfg.llm.batch_max_chars],
            }
            for post in batch
        ]
        system = SYSTEM_PROMPT.format(
            language=self.cfg.digest_language, max_chars=self.cfg.summary_max_chars
        )
        user = json.dumps({"posts": items}, ensure_ascii=False)
        data = await self.llm.complete_json(system, user, max_tokens=300 + 250 * len(batch))
        rows = data.get("posts", []) if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise LLMError("Ожидался список posts")
        by_id = {}
        for row in rows:
            if isinstance(row, dict) and "id" in row:
                try:
                    by_id[int(row["id"])] = row
                except (TypeError, ValueError):
                    continue
        for post in batch:
            row = by_id.get(post.id)
            if row is None:
                self._fallback(post)
                continue
            post.is_ad = bool(row.get("is_ad", False))
            summary = str(row.get("summary") or "").strip()
            if self._needs_summary(post):
                if summary:
                    post.summary = truncate(summary, self.cfg.summary_max_chars + 150)
                    post.summarized = True
                else:
                    self._fallback(post)

    async def compress_channel(
        self, channel: Channel, posts: list[Post], budget: int
    ) -> list[tuple[int, str]]:
        """Отбирает важные посты канала и сжимает их до одной фразы.

        Возвращает [(post_id, text)] в порядке убывания важности.
        """
        if not self.llm:
            return []
        items = []
        for post in posts:
            if post.media_only:
                body = f"[только медиа: {', '.join(post.media)}]"
            else:
                body = post.summary if post.summarized and post.summary else truncate(post.text, 700)
            items.append({"id": post.id, "text": body})
        line_chars = max(60, min(220, budget // max(1, min(len(posts), 20))))
        system = COMPRESS_PROMPT.format(
            channel=channel.display_name,
            line_chars=line_chars,
            budget=budget,
            language=self.cfg.digest_language,
        )
        data = await self.llm.complete_json(
            system,
            json.dumps({"posts": items}, ensure_ascii=False),
            max_tokens=min(4000, 200 + budget),
        )
        rows = data.get("keep", []) if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise LLMError("Ожидался список keep")
        valid = {p.id for p in posts}
        result: list[tuple[int, str]] = []
        seen: set[int] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                pid = int(row.get("id"))
            except (TypeError, ValueError):
                continue
            text = str(row.get("text") or "").strip()
            if pid in valid and pid not in seen and text:
                seen.add(pid)
                result.append((pid, truncate(text, line_chars + 40)))
        return result

    async def overview(self, entries: list[tuple[Post, Channel]]) -> str:
        """Блок «Главное за день» по всем постам дайджеста."""
        if not self.llm or not entries:
            return ""
        lines = []
        budget = 20000
        for post, channel in entries:
            body = post.summary or truncate(post.text, 400)
            if not body:
                continue
            tags = ", ".join(channel.tags)
            line = f"[{channel.display_name}{' | ' + tags if tags else ''}] {body}"
            budget -= len(line)
            if budget < 0:
                break
            lines.append(line)
        if len(lines) < 3:
            return ""
        try:
            text = await self.llm.complete(
                OVERVIEW_PROMPT.format(language=self.cfg.digest_language),
                "\n".join(lines),
                max_tokens=800,
            )
        except LLMError as exc:
            log.warning("Не удалось построить обзор дня: %s", exc)
            return ""
        bullets = [
            "• " + line.strip().lstrip("•-*· ").strip()
            for line in text.splitlines()
            if line.strip().lstrip("•-*· ").strip()
        ]
        return "\n".join(bullets[:8])
