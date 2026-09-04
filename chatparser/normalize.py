"""Normalize & hash (PRD §7's pipeline entry step, §7.4's cache key).

Pure text processing: lowercase, collapse whitespace, strip trailing
punctuation. The hash is what the T0 cache keys on - stable across
resends of the same message, and holding no more than the normalized text
itself (§7.4: "no PII in the key... hash only the normalized food text").
"""
from __future__ import annotations

import hashlib
import re

_WHITESPACE = re.compile(r"\s+")
_TRAILING_PUNCTUATION = re.compile(r"[!.?,;:]+$")


def normalize_text(text: str) -> str:
    lowered = text.strip().lower()
    collapsed = _WHITESPACE.sub(" ", lowered)
    return _TRAILING_PUNCTUATION.sub("", collapsed).strip()


def hash_normalized(normalized_text: str) -> str:
    return hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
