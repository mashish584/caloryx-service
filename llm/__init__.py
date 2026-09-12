"""T2/T3 provider integration (PRD §7.3) and embeddings (§7.2, §7.6).

Isolated behind this package - same boundary as `authx/clerk.py` for Clerk.
Only `assistant.services` and `meals`' backfill command call into it.
"""
from .client import (
    LLMCallError,
    LLMConfigurationError,
    LLMResponse,
    call_large_model,
    call_small_model,
)
from .embeddings import EmbeddingResponse, embed_batch, embed_text

__all__ = [
    "EmbeddingResponse",
    "LLMCallError",
    "LLMConfigurationError",
    "LLMResponse",
    "call_large_model",
    "call_small_model",
    "embed_batch",
    "embed_text",
]
