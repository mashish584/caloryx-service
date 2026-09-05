"""OpenAI structured-output calls for T2 (PRD §7.3).

A thin, isolated wrapper around one external API - same shape as
`authx/clerk.py`: config-driven, its own error types, nothing outside
`assistant.services` calls it directly. Not "dependency-free" like
`engine`/`nutrition`/`chatparser` (this one does real I/O), but it stays the
only module that imports the `openai` SDK, so a provider swap later is a
change to this file alone.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from django.conf import settings

from .schema import INTENT_ENVELOPE_SCHEMA

logger = logging.getLogger(__name__)

_client: Optional[Any] = None
_client_lock = threading.Lock()


class LLMConfigurationError(RuntimeError):
    """Provider settings are missing - a deployment problem, not a caller problem."""


class LLMCallError(Exception):
    """The call failed or returned something unusable - network error, timeout,
    rate limit, or a non-JSON response. Callers degrade gracefully on this
    (§11: "the chat must never be fully unavailable"), never surface a 500."""


@dataclass(frozen=True)
class LLMResponse:
    raw_envelope: Dict[str, Any]
    prompt_tokens: int
    output_tokens: int
    latency_ms: int
    model: str


def _get_client() -> Any:
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            if not settings.OPENAI_API_KEY:
                raise LLMConfigurationError("OPENAI_API_KEY must be set to call the LLM provider.")
            from openai import OpenAI  # deferred - importing the SDK costs real startup time

            _client = OpenAI(
                api_key=settings.OPENAI_API_KEY, timeout=settings.OPENAI_TIMEOUT_SECONDS
            )
    return _client


def reset_cache() -> None:
    """Test hook - drops the memoised client."""
    global _client
    with _client_lock:
        _client = None


def call_small_model(system_prompt: str, user_content: str) -> LLMResponse:
    """One call, one message, the whole intent envelope (§7.3: "never one call
    per food, and never a separate call for naming or slot"). Raises
    `LLMCallError`/`LLMConfigurationError` - it never returns a partial or
    best-effort result, so a caller either has a fully valid envelope or
    knows unambiguously that it doesn't."""
    client = _get_client()
    from openai import OpenAIError  # deferred, same reasoning as _get_client's import

    started = time.monotonic()

    try:
        response = client.chat.completions.create(
            model=settings.OPENAI_SMALL_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            max_tokens=settings.OPENAI_MAX_TOKENS,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "intent_envelope",
                    "schema": INTENT_ENVELOPE_SCHEMA,
                    "strict": True,
                },
            },
        )
    except OpenAIError as exc:
        logger.warning("llm call failed model=%s: %s", settings.OPENAI_SMALL_MODEL, exc)
        raise LLMCallError(str(exc)) from exc

    latency_ms = int((time.monotonic() - started) * 1000)

    content = response.choices[0].message.content
    try:
        raw_envelope = json.loads(content)
    except (TypeError, ValueError) as exc:
        # Shouldn't happen under strict mode, but a malformed response is a
        # parse miss, not a crash - same posture as everything else here.
        logger.warning("llm returned non-JSON content: %r", content)
        raise LLMCallError("Model response was not valid JSON.") from exc

    usage = response.usage
    return LLMResponse(
        raw_envelope=raw_envelope,
        prompt_tokens=usage.prompt_tokens if usage else 0,
        output_tokens=usage.completion_tokens if usage else 0,
        latency_ms=latency_ms,
        model=response.model,
    )
