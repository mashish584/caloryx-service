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


class ChatRole(StrEnum):
    USER = "USER"
    ASSISTANT = "ASSISTANT"


class ChatIntent(StrEnum):
    """The PRD's full 13-intent taxonomy (§5.5), minus WELLBEING_FLAG - held
    back deliberately until Chunk 6b implements its required behavior
    (§5.6), so nothing can classify a message that way before anything
    handles it."""

    LOG_NEW = "LOG_NEW"
    EDIT_ITEM = "EDIT_ITEM"
    ADD_ITEM = "ADD_ITEM"
    REMOVE_ITEM = "REMOVE_ITEM"
    SET_SLOT = "SET_SLOT"
    DIARY_QUERY = "DIARY_QUERY"
    APP_HELP = "APP_HELP"
    NUTRITION_QA = "NUTRITION_QA"
    ADVICE_SEEKING = "ADVICE_SEEKING"
    SOCIAL = "SOCIAL"
    UNCLEAR = "UNCLEAR"
    OTHER = "OTHER"
