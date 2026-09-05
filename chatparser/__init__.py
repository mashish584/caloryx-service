"""Meal Assistant chat pipeline (PRD §7, §9, §12.6).

Deliberately dependency-free: no Django, no Prisma, no I/O - mirrors
`engine/`/`nutrition/`'s boundary. Turns raw text into structured intent;
`assistant.services` is what turns that structured intent into draft
mutations and persistence.
"""
from .confidence import HIGH_THRESHOLD, MEDIUM_THRESHOLD, band_for_score, score_food_match
from .enums import (
    ChatIntent,
    ChatRole,
    DraftStatus,
    ItemResolution,
    MassSource,
    MatchBand,
    ParseTier,
    QuantitySource,
)
from .grammar import ParsedEdit, ParsedItemPhrase, parse_edit_command, parse_new_item_phrases
from .normalize import hash_normalized, normalize_text
from .preclassifier import (
    classify_t1_intent,
    extract_nutrition_qa_food,
    is_advice_seeking,
    is_app_help,
    is_diary_query,
    is_diary_query_a_trend_question,
    is_non_food_greeting,
    is_nutrition_qa,
    is_unclear,
)

__all__ = [
    "ChatIntent",
    "ChatRole",
    "DraftStatus",
    "HIGH_THRESHOLD",
    "ItemResolution",
    "MEDIUM_THRESHOLD",
    "MassSource",
    "MatchBand",
    "ParseTier",
    "ParsedEdit",
    "ParsedItemPhrase",
    "QuantitySource",
    "band_for_score",
    "classify_t1_intent",
    "extract_nutrition_qa_food",
    "hash_normalized",
    "is_advice_seeking",
    "is_app_help",
    "is_diary_query",
    "is_diary_query_a_trend_question",
    "is_non_food_greeting",
    "is_nutrition_qa",
    "is_unclear",
    "normalize_text",
    "parse_edit_command",
    "parse_new_item_phrases",
    "score_food_match",
]
