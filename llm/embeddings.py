"""Embedding calls for the semantic half of food resolution (PRD §7.2, §7.6).

Same boundary posture as `client.py`: this and `client.py` are the only
modules that import the provider SDK, so a provider swap stays a change to
this package alone. Config-driven, its own errors reused from `client.py`,
and - deliberately - **no policy of its own**.

In particular the cost circuit breaker (§11) is *not* applied here, even
though the Chunk 9a plan first said it would be. The breaker lives in
`assistant.services._call_llm`, one layer up, because it reads and writes
`assistant.repository`; importing that from `llm/` would invert the
dependency this package exists to keep one-directional. The rule that
matters - a provider outage degrades to trigram-only rather than surfacing a
500 - is a call-site rule, and 9a has exactly one call site: the offline
backfill command, where the right response to an outage is to fail the batch
loudly and be re-run, not to trip a request-path breaker. Chunk 9b adds the
request-path call, and it goes through the breaker the same way `_call_llm`
already does.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence

from django.conf import settings

from .client import LLMCallError, LLMConfigurationError

logger = logging.getLogger(__name__)

_client: Optional[Any] = None


@dataclass(frozen=True)
class EmbeddingResponse:
    """One batch's vectors, in the same order as the input texts.

    `prompt_tokens` feeds `ParseEvent.costMicros`; embeddings have no output
    tokens, so there is no counterpart field.
    """

    vectors: List[List[float]]
    prompt_tokens: int
    latency_ms: int
    model: str


def _get_client() -> Any:
    """Its own client instance rather than `client.py`'s, because the timeout
    differs: a 256-item embedding batch is a much longer call than the
    10-second single-message completion `OPENAI_TIMEOUT_SECONDS` is sized for.
    """
    global _client
    if _client is not None:
        return _client
    if not settings.OPENAI_API_KEY:
        raise LLMConfigurationError("OPENAI_API_KEY must be set to call the embeddings API.")
    if not settings.EMBEDDING_MODEL:
        raise LLMConfigurationError("EMBEDDING_MODEL must be set to call the embeddings API.")
    from openai import OpenAI  # deferred - importing the SDK costs real startup time

    _client = OpenAI(
        api_key=settings.OPENAI_API_KEY, timeout=settings.EMBEDDING_TIMEOUT_SECONDS
    )
    return _client


def reset_cache() -> None:
    """Test hook - drops the memoised client."""
    global _client
    _client = None


def embed_batch(texts: Sequence[str]) -> EmbeddingResponse:
    """Embed up to `EMBEDDING_MAX_BATCH` texts in one call.

    Raises `LLMCallError` on any provider failure and `LLMConfigurationError`
    when the provider isn't configured - never a partial result, so a caller
    either has a vector for every input or knows it has none. Callers that
    need larger inputs chunk them; this does not silently split, because a
    split changes the cost accounting the caller is recording.
    """
    if not texts:
        return EmbeddingResponse(vectors=[], prompt_tokens=0, latency_ms=0, model=settings.EMBEDDING_MODEL)
    if len(texts) > settings.EMBEDDING_MAX_BATCH:
        raise ValueError(
            "embed_batch got {} texts, over EMBEDDING_MAX_BATCH={}".format(
                len(texts), settings.EMBEDDING_MAX_BATCH
            )
        )

    client = _get_client()
    from openai import OpenAIError  # deferred, same reasoning as _get_client's import

    started = time.monotonic()
    try:
        response = client.embeddings.create(
            model=settings.EMBEDDING_MODEL,
            input=list(texts),
            dimensions=settings.EMBEDDING_DIMENSIONS,
        )
    except OpenAIError as exc:
        logger.warning("embedding call failed model=%s n=%s: %s", settings.EMBEDDING_MODEL, len(texts), exc)
        raise LLMCallError(str(exc)) from exc

    latency_ms = int((time.monotonic() - started) * 1000)

    # The API documents `data` as returned in input order, but it also carries
    # an explicit `index` - sort by it rather than trusting the order, since a
    # silent misalignment here would attach every food to its neighbour's
    # vector and be almost impossible to spot downstream.
    ordered = sorted(response.data, key=lambda item: item.index)
    vectors = [list(item.embedding) for item in ordered]

    if len(vectors) != len(texts):
        raise LLMCallError(
            "embeddings API returned {} vectors for {} inputs".format(len(vectors), len(texts))
        )
    for vector in vectors:
        if len(vector) != settings.EMBEDDING_DIMENSIONS:
            # A width mismatch would be rejected by Postgres at insert time
            # anyway; failing here names the real cause instead.
            raise LLMCallError(
                "embeddings API returned width {}, expected EMBEDDING_DIMENSIONS={}".format(
                    len(vector), settings.EMBEDDING_DIMENSIONS
                )
            )

    usage = response.usage
    return EmbeddingResponse(
        vectors=vectors,
        prompt_tokens=usage.prompt_tokens if usage else 0,
        latency_ms=latency_ms,
        model=settings.EMBEDDING_MODEL,
    )


def embed_text(text: str) -> List[float]:
    """One text -> one vector. Chunk 9b's request-path entry point."""
    return embed_batch([text]).vectors[0]
