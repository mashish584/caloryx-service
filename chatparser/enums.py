"""Domain enums for the Meal Assistant chat pipeline (PRD §9, §12.6).

Values match the Prisma enums exactly, following the same convention as
`nutrition/enums.py` (which also reuses `engine.enums.StrEnum`).
"""
from __future__ import annotations

from engine.enums import StrEnum


class DraftStatus(StrEnum):
    OPEN = "OPEN"
    CONFIRMED = "CONFIRMED"
    DISCARDED = "DISCARDED"
    EXPIRED = "EXPIRED"


class ParseTier(StrEnum):
    """Which tier produced a draft. `MANUAL` is not in the PRD's enum - added
    here for drafts created via Chunk 2a's structured endpoints, where no
    parsing happened at all."""

    MANUAL = "MANUAL"
    PRECLASSIFIER = "PRECLASSIFIER"
    CACHE = "CACHE"
    PARSER = "PARSER"
    LLM_SMALL = "LLM_SMALL"
    LLM_LARGE = "LLM_LARGE"


class ItemResolution(StrEnum):
    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"
    ESTIMATED_DISH = "ESTIMATED_DISH"


class QuantitySource(StrEnum):
    EXPLICIT = "EXPLICIT"
    ASSUMED = "ASSUMED"


class MassSource(StrEnum):
    DIRECT = "DIRECT"
    HOUSEHOLD_TABLE = "HOUSEHOLD_TABLE"
    USER_HISTORY = "USER_HISTORY"
    CATALOG_SERVING = "CATALOG_SERVING"
    CATEGORY_FALLBACK = "CATEGORY_FALLBACK"


class MatchBand(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
