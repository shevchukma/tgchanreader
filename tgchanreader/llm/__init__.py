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
from typing import Any

import httpx

from ..config import LLMConfig

log = logging.getLogger(__name__)


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self._http = httpx.AsyncClient(timeout=cfg.timeout)
        self._sem = asyncio.Semaphore(cfg.max_concurrency)

    async def close(self) -> None:
        await self._http.aclose()

    async def complete(self, system: str, user: str, max_tokens: int = 2000) -> str:
        async with self._sem:
            last_exc: Exception | None = None
            for attempt in range(3):
                try:
                    return await self._complete(system, user, max_tokens)
                except (httpx.HTTPError, LLMError) as exc:
                    last_exc = exc
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    if status in (400, 401, 403, 404):
                        break
                    await asyncio.sleep(2 ** attempt * 2)
            raise LLMError(f"LLM-запрос не удался: {last_exc}") from last_exc

    async def _complete(self, system: str, user: str, max_tokens: int) -> str:
        raise NotImplementedError

    async def complete_json(self, system: str, user: str, max_tokens: int = 2000) -> Any:
        text = await self.complete(system, user, max_tokens)
        return extract_json(text)


class OpenAICompatibleClient(LLMClient):
    async def _complete(self, system: str, user: str, max_tokens: int) -> str:
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        if self.cfg.provider == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/shevchukma/tgchanreader"
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
        resp = await self._http.post(
            f"{self.cfg.base_url}/chat/completions", headers=headers, json=payload
        )
        if resp.status_code >= 400:
            log.error("LLM %s: HTTP %s %s", self.cfg.provider, resp.status_code, resp.text[:500])
            resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise LLMError(str(data["error"]))
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Неожиданный ответ: {str(data)[:300]}") from exc
        if isinstance(content, list):  # некоторые провайдеры отдают части
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return content or ""


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
            log.error("LLM anthropic: HTTP %s %s", resp.status_code, resp.text[:500])
            resp.raise_for_status()
        data = resp.json()
        parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        return "".join(parts)


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
