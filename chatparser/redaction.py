"""PII redaction before any provider call (§12.14, Chunk 8c).

Structured PII only - emails and phone-number-shaped digit runs. Free-text
names and health mentions need either an NER model or a real product
decision this codebase has nothing to calibrate against (the same "no data
to invent a threshold from" reasoning already applied to the T1->T2 router,
the T3 confidence threshold, and the wellbeing keyword net) - a documented
gap, not silently dropped.
"""
from __future__ import annotations

import re

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# A run of digits/separators at least 7 characters long between two
# whitespace-safe edges - long enough to catch real phone numbers
# ("987-654-3210", "+91 98765 43210") while leaving ordinary food quantities
# alone: a quantity like "200g" or "1.5 cups" never matches, because the
# trailing unit letter isn't in the character class, so the required
# trailing digit never lands.
_PHONE_RE = re.compile(r"(?<!\S)\+?\d[\d\-.\s]{5,}\d(?!\S)")


def redact_pii(text: str) -> str:
    redacted = _EMAIL_RE.sub("[redacted-email]", text)
    return _PHONE_RE.sub("[redacted-phone]", redacted)
