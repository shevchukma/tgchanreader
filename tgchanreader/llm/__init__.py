"""Клиенты LLM.

Поддерживаются:
* openrouter — https://openrouter.ai (по умолчанию), сотни моделей через один ключ;
* openai — OpenAI API или любой OpenAI-совместимый сервер (LLM_BASE_URL):
  DeepSeek, Groq, Together, vLLM, LM Studio, Mistral и т.д.;
* ollama — локальные модели через OpenAI-совместимый API Ollama;
* anthropic — Claude напрямую через Anthropic Messages API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import httpx

from ..config import LLMConfig

log = logging.getLogger(__name__)


class LLMError(Exception):
    pass


class LLMText(str):
    """Ответ модели; truncated=True — ответ обрезан лимитом токенов."""

    truncated: bool = False


def _text(content: str, truncated: bool) -> LLMText:
    result = LLMText(content)
    result.truncated = truncated
    return result


EMPTY_HINT = (
    "пустой ответ модели: лимит токенов ушёл на размышления. "
    "Увеличьте LLM_MAX_TOKENS или задайте LLM_REASONING=off/low"
)


class LLMTruncatedError(LLMError):
    """Ответ пустой из-за лимита токенов — повтор с тем же лимитом не поможет."""


class LLMHTTPError(LLMError):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


# Ошибки, которые не исправятся повтором запроса (ключ, модель, настройки аккаунта).
FATAL_STATUSES = {400, 401, 402, 403, 404}
FATAL_PAUSE_SECONDS = 600


def _error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:300]
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err)[:500]
    if err:
        return str(err)[:500]
    return resp.text[:300]


class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self._http = httpx.AsyncClient(timeout=cfg.timeout)
        self._sem = asyncio.Semaphore(cfg.max_concurrency)
        self._paused_until = 0.0
        self.last_error = ""
        self.last_error_at: float | None = None
        self.last_ok_at: float | None = None

    async def close(self) -> None:
        await self._http.aclose()

    def reset(self) -> None:
        self._paused_until = 0.0

    @property
    def paused(self) -> bool:
        return time.monotonic() < self._paused_until

    def _fail(self, message: str) -> None:
        self.last_error = message
        self.last_error_at = time.time()

    async def complete(self, system: str, user: str, max_tokens: int = 2000) -> LLMText:
        max_tokens = max(max_tokens, self.cfg.max_tokens)
        async with self._sem:
            if self.paused:
                # уже знаем, что запросы не проходят — не долбим API и не спамим лог
                raise LLMError(self.last_error)
            last_exc: Exception | None = None
            for attempt in range(3):
                try:
                    text = await self._complete(system, user, max_tokens)
                    self.last_ok_at = time.time()
                    self.last_error = ""
                    return text
                except LLMHTTPError as exc:
                    last_exc = exc
                    if exc.status in FATAL_STATUSES:
                        self._paused_until = time.monotonic() + FATAL_PAUSE_SECONDS
                        self._fail(exc.message)
                        log.error(
                            "LLM %s (%s) отклонил запрос, HTTP %s: %s. "
                            "Запросы приостановлены на %d мин, посты подождут. Проверьте ключ, "
                            "LLM_MODEL и настройки аккаунта; проверка в боте: /llmtest",
                            self.cfg.provider, self.cfg.model, exc.status, exc.message,
                            FATAL_PAUSE_SECONDS // 60,
                        )
                        raise LLMError(exc.message) from exc
                except LLMTruncatedError as exc:
                    self._fail(str(exc))
                    log.warning("LLM %s: %s", self.cfg.provider, exc)
                    raise
                except (httpx.HTTPError, LLMError) as exc:
                    last_exc = exc
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt * 2)
            message = f"{type(last_exc).__name__}: {last_exc}"
            self._fail(message)
            log.warning("LLM %s: запрос не удался после 3 попыток: %s", self.cfg.provider, message)
            raise LLMError(message) from last_exc

    async def _complete(self, system: str, user: str, max_tokens: int) -> LLMText:
        raise NotImplementedError

    async def complete_json(self, system: str, user: str, max_tokens: int = 2000) -> Any:
        last: LLMError | None = None
        for _ in range(2):  # модели иногда отвечают не JSON — пробуем ещё раз
            text = await self.complete(system, user, max_tokens)
            try:
                return extract_json(text)
            except LLMError as exc:
                last = exc
                if text.truncated:
                    break  # обрезанный ответ повтор не исправит
        assert last
        raise last


class OpenAICompatibleClient(LLMClient):
    async def _complete(self, system: str, user: str, max_tokens: int) -> str:
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        if self.cfg.provider == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/velikoross/tgchanreader"
            headers["X-Title"] = "tgchanreader"
        payload = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.cfg.temperature,
            "max_tokens": max_tokens,
        }
        if self.cfg.provider == "openrouter" and self.cfg.reasoning:
            if self.cfg.reasoning in {"off", "none", "false", "0"}:
                payload["reasoning"] = {"enabled": False}
            else:
                payload["reasoning"] = {"effort": self.cfg.reasoning, "exclude": True}
        resp = await self._http.post(
            f"{self.cfg.base_url}/chat/completions", headers=headers, json=payload
        )
        if resp.status_code >= 400:
            raise LLMHTTPError(resp.status_code, _error_message(resp))
        data = resp.json()
        if "error" in data:
            raise LLMError(str(data["error"]))
        try:
            choice = data["choices"][0]
            content = choice["message"].get("content")
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise LLMError(f"Неожиданный ответ: {str(data)[:300]}") from exc
        if isinstance(content, list):  # некоторые провайдеры отдают части
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        truncated = choice.get("finish_reason") == "length"
        if not (content or "").strip():
            raise (LLMTruncatedError(EMPTY_HINT) if truncated else LLMError("пустой ответ модели"))
        if truncated:
            log.warning("LLM: ответ обрезан лимитом токенов (LLM_MAX_TOKENS=%s)", self.cfg.max_tokens)
        return _text(content, truncated)


class AnthropicClient(LLMClient):
    async def _complete(self, system: str, user: str, max_tokens: int) -> str:
        headers = {
            "x-api-key": self.cfg.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": self.cfg.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "temperature": self.cfg.temperature,
        }
        resp = await self._http.post(f"{self.cfg.base_url}/v1/messages", headers=headers, json=payload)
        if resp.status_code >= 400:
            raise LLMHTTPError(resp.status_code, _error_message(resp))
        data = resp.json()
        parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        truncated = data.get("stop_reason") == "max_tokens"
        content = "".join(parts)
        if not content.strip():
            raise (LLMTruncatedError(EMPTY_HINT) if truncated else LLMError("пустой ответ модели"))
        return _text(content, truncated)


def build_llm(cfg: LLMConfig) -> LLMClient | None:
    if not cfg.enabled:
        log.warning("LLM не настроен: реклама определяется эвристиками, посты просто обрезаются")
        return None
    if cfg.provider == "anthropic":
        return AnthropicClient(cfg)
    if cfg.provider in {"openrouter", "openai", "ollama", "openai-compatible", "custom"}:
        return OpenAICompatibleClient(cfg)
    raise ValueError(f"Неизвестный LLM_PROVIDER: {cfg.provider}")


def extract_json(text: str) -> Any:
    """Достаёт JSON из ответа модели (с учётом ```json ... ``` и лишнего текста)."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        end = text.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"Модель вернула не JSON: {text[:200]}")
