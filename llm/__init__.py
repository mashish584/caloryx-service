"""T2/T3 provider integration (PRD §7.3).

Isolated behind this package - same boundary as `authx/clerk.py` for Clerk.
Only `assistant.services` calls into it.
"""
from .client import (
    LLMCallError,
    LLMConfigurationError,
    LLMResponse,
    call_large_model,
    call_small_model,
)

__all__ = [
    "LLMCallError",
    "LLMConfigurationError",
    "LLMResponse",
    "call_large_model",
    "call_small_model",
]
