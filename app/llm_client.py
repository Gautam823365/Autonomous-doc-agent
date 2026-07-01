"""
llm_client.py
==============

MANDATORY ENGINEERING IMPROVEMENT: Retry & Fallback Logic
-----------------------------------------------------------
Free-tier / local LLMs are the whole point of this assignment, but they
are also the least reliable part of the stack: Groq's free tier rate
limits aggressively, Ollama may not be running, and any HTTP call can
time out or drop. If the agent just crashes the first time a provider
hiccups, it isn't really "autonomous" -- it's brittle.

This module implements a resilient call chain:

    1. Try the primary provider (Groq) with bounded exponential-backoff
       retries (handles transient errors: timeouts, 429 rate limits,
       connection resets).
    2. If the primary provider is exhausted, fail over to the secondary
       provider (local Ollama) -- a different *kind* of failure domain
       (network/API vs local process), so a Groq outage doesn't take
       the agent down with it.
    3. If every LLM backend is unavailable (e.g. no API key configured
       and Ollama isn't installed -- the common case when this is first
       cloned), raise LLMAllProvidersFailed so the caller can fall back
       to a deterministic, template-based generator. The agent still
       produces a complete, valid Word document -- degraded quality,
       not a 500 error.

This turns "the LLM call failed" from a fatal crash into a handled,
observable state (every TaskResult records which provider actually
served it, including "fallback_template" when nothing else worked).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Optional

import httpx

logger = logging.getLogger("agent.llm_client")


class LLMAllProvidersFailed(Exception):
    """Raised when every configured provider has been exhausted."""


@dataclass
class LLMResult:
    text: str
    provider: str


class _Provider:
    name: str

    async def call(self, system: str, user: str) -> str:
        raise NotImplementedError

    def is_configured(self) -> bool:
        return True


class GroqProvider(_Provider):
    name = "groq"

    def __init__(self) -> None:
        self.api_key = os.getenv("GROQ_API_KEY", "").strip()
        self.model = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
        self.url = "https://api.groq.com/openai/v1/chat/completions"

    def is_configured(self) -> bool:
        return bool(self.api_key)

    async def call(self, system: str, user: str) -> str:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                self.url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.4,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]


class OllamaProvider(_Provider):
    name = "ollama"

    def __init__(self) -> None:
        self.base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
        self.model = os.getenv("OLLAMA_MODEL", "llama3.1")

    def is_configured(self) -> bool:
        # Always "configured" -- it's a local URL. Reachability is checked at call time.
        return True

    async def call(self, system: str, user: str) -> str:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "stream": False,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["message"]["content"]


class LLMClient:
    """Orchestrates retry-with-backoff per provider, then fails over."""

    def __init__(self, max_retries: int = 2, base_delay: float = 0.6) -> None:
        self.providers = [GroqProvider(), OllamaProvider()]
        self.max_retries = max_retries
        self.base_delay = base_delay

    async def _call_with_retry(self, provider: _Provider, system: str, user: str) -> str:
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                return await provider.call(system, user)
            except Exception as exc:  # noqa: BLE001 - we want to catch & retry broadly here
                last_exc = exc
                delay = self.base_delay * (2 ** (attempt - 1))
                logger.warning(
                    "Provider %s attempt %d/%d failed (%s); backing off %.1fs",
                    provider.name, attempt, self.max_retries, exc, delay,
                )
                if attempt < self.max_retries:
                    await asyncio.sleep(delay)
        raise last_exc  # type: ignore[misc]

    async def complete(self, system: str, user: str) -> LLMResult:
        """Try each provider in order; raise LLMAllProvidersFailed if all fail."""
        errors = []
        for provider in self.providers:
            if not provider.is_configured():
                logger.info("Skipping provider %s (not configured)", provider.name)
                continue
            try:
                text = await self._call_with_retry(provider, system, user)
                return LLMResult(text=text, provider=provider.name)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{provider.name}: {exc}")
                logger.warning("Provider %s exhausted, failing over. Errors so far: %s", provider.name, errors)
                continue
        raise LLMAllProvidersFailed("; ".join(errors) or "No providers configured")

    async def complete_json(self, system: str, user: str) -> tuple[Any, str]:
        """Call the LLM and parse JSON out of its response (LLMs often wrap JSON in prose/fences)."""
        result = await self.complete(system, user)
        parsed = _extract_json(result.text)
        if parsed is None:
            raise ValueError(f"Could not parse JSON from {result.provider} response: {result.text[:200]}")
        return parsed, result.provider


def _extract_json(text: str) -> Optional[Any]:
    """Best-effort JSON extraction from an LLM response."""
    text = text.strip()
    # Strip markdown code fences if present.
    fence_match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidate = fence_match.group(1).strip() if fence_match else text
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    # Last resort: grab the largest {...} or [...] span.
    brace_match = re.search(r"(\{.*\}|\[.*\])", candidate, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(1))
        except json.JSONDecodeError:
            return None
    return None
