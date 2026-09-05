"""The intent-envelope JSON schema (PRD §7.3) - the model's entire contract.

Passed to the provider as an OpenAI Structured Outputs schema (`strict: true`),
which is what makes "structured output only, never prose" a guarantee the API
itself enforces rather than something this codebase has to police after the
fact. Strict mode requires every property to be listed in `required` (there is
no true "optional key" - a field the model may omit is expressed as a
nullable type instead) and `additionalProperties: false` on every object.

No `dishCategory` field: that's §7.6.1's estimated-dish path (Chunk 5's AI
half). Only the 6 `ChatIntent` members Chunk 2b defined are offered - the
other 7 don't exist anywhere else in this system yet either (Chunk 6).
"""
from __future__ import annotations

_INTENT_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        # Free text, matching what the user said - never a database row or
        # an id (§7.3: "the food database is never sent in the prompt").
        "food": {"type": "string"},
        # Nullable: whether an amount was stated at all is exactly the
        # question §5.1.1a's quantity-resolution ladder answers - the model
        # reports what's there, it doesn't guess.
        "quantity": {"type": ["number", "null"]},
        "unit": {"type": ["string", "null"]},
        "state": {"type": ["string", "null"], "enum": ["raw", "cooked", None]},
        "prep": {"type": ["string", "null"]},
        "sizeQualifier": {"type": ["string", "null"], "enum": ["small", "medium", "large", None]},
        # The model's own self-assessed extraction confidence - logged for
        # calibration, not yet blended into the server-side confidence score.
        "confidence": {"type": "number"},
    },
    "required": ["food", "quantity", "unit", "state", "prep", "sizeQualifier", "confidence"],
    "additionalProperties": False,
}

INTENT_ENVELOPE_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": ["LOG_NEW", "EDIT_ITEM", "ADD_ITEM", "REMOVE_ITEM", "SET_SLOT", "OTHER"],
        },
        # Free text referring to an existing draft item - null for LOG_NEW.
        # Unused by anything in Chunk 4a (only LOG_NEW is acted on), kept in
        # the schema now so the contract doesn't change shape in Chunk 5.
        "targetRef": {"type": ["string", "null"]},
        # Only when the user stated it explicitly; else null (§5.1.2).
        "slot": {"type": ["string", "null"], "enum": ["BREAKFAST", "LUNCH", "DINNER", "SNACK", None]},
        # Optional - free-rides on this call (§5.1.3), takes precedence over
        # the deterministic template when present.
        "mealName": {"type": ["string", "null"]},
        "items": {"type": "array", "items": _INTENT_ITEM_SCHEMA},
    },
    "required": ["intent", "targetRef", "slot", "mealName", "items"],
    "additionalProperties": False,
}
