"""Вырезание лишних строк из постов по правилам из .env (STRIP_LINES).

Формат: правила через «;». Каждое правило — строка поста целиком (без учёта регистра
и пробелов по краям). Дополнительно:

* «*» — любые символы:        Подписывайтесь*
* «@канал:» в начале — правило только для этого канала:  @baza:BAZA в MAX
* «re:» — регулярное выражение для всей строки:          re:^BAZA в \\w+$

Пример: STRIP_LINES=@baza:BAZA в Telegram;@baza:BAZA в MAX;Подписаться на канал*
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class LineRule:
    pattern: re.Pattern[str]
    channel: str | None = None  # ref канала или None — для всех
    source: str = ""


def _compile(raw: str) -> re.Pattern[str]:
    if raw.startswith("re:"):
        return re.compile(raw[3:].strip(), re.I)
    words = [".*".join(re.escape(part) for part in word.split("*")) for word in raw.split()]
    body = r"\s+".join(words)  # любое количество пробелов между словами
    return re.compile(rf"^\s*{body}\s*$", re.I)


def parse_rules(raw: str) -> list[LineRule]:
    rules: list[LineRule] = []
    for item in (raw or "").split(";"):
        item = item.strip()
        if not item:
            continue
        channel = None
        m = re.match(r"^@([A-Za-z0-9_\-]+)\s*:\s*(.+)$", item)
        if m:
            channel, item = m.group(1).lower(), m.group(2).strip()
        try:
            rules.append(LineRule(_compile(item), channel, item))
        except re.error as exc:
            log.error("STRIP_LINES: неверное правило %r: %s", item, exc)
    return rules


def strip_lines(text: str, rules: list[LineRule], channel_ref: str = "") -> str:
    """Удаляет строки, подходящие под правила (для этого канала или для всех)."""
    if not text or not rules:
        return text
    active = [r for r in rules if r.channel is None or r.channel == channel_ref.lower()]
    if not active:
        return text
    lines = [line for line in text.split("\n") if not any(r.pattern.match(line) for r in active)]
    result = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", result).strip()
