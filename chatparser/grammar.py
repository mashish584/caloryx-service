"""T1: the deterministic parser (PRD §7.5).

Two grammars, both pure regex + vocabulary lookup - no catalog access, no I/O:

- `parse_new_item_phrases` - `QTY + UNIT + [STATE/PREP] + FOOD`, splitting a
  message on common connectors so "200g rice and 2 rotis" yields two phrases.
- `parse_edit_command` - the §7.5 edit patterns (`EDIT_ITEM`/`ADD_ITEM`/
  `REMOVE_ITEM`/`SET_SLOT`), tried in a fixed order.

Both require an explicit quantity + unit, same as Chunk 2a's structured
endpoint - there is no default-serving inference here (that ladder is
Chunk 4). A segment that doesn't match either grammar is reported back
verbatim, never silently dropped (§12.13).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .units import UNIT_WORDS, WORD_NUMBERS

# cooking-method word -> (FoodState value, prep word or None). "raw"/"cooked"
# are pure state words; the rest imply COOKED *and* record the method as prep
# (§7.3's envelope carries `state` and `prep` as separate fields).
_PREP_TO_STATE = {
    "raw": ("RAW", None),
    "cooked": ("COOKED", None),
    "boiled": ("COOKED", "boiled"),
    "grilled": ("COOKED", "grilled"),
    "fried": ("COOKED", "fried"),
    "steamed": ("COOKED", "steamed"),
    "roasted": ("COOKED", "roasted"),
    "baked": ("COOKED", "baked"),
}

_SPLIT_RE = re.compile(r"\s*(?:,|&|\band\b|\bwith\b|\bplus\b)\s*")

# Digits allow zero-or-more space before the unit ("200g" and "200 g" both
# match); word-numbers require a space ("a piece", never "apiece").
_DIGIT_ITEM_RE = re.compile(r"^(?P<qty>\d+(?:\.\d+)?)\s*(?P<unit>[a-z]+)\s+(?P<rest>.+)$")
_WORD_ITEM_RE = re.compile(r"^(?P<qty>[a-z]+)\s+(?P<unit>[a-z]+)\s+(?P<rest>.+)$")

_EDIT_WAS_ACTUALLY_RE = re.compile(
    r"^(?P<food>.+?)\s+was\s+actually\s+(?P<qty>\d+(?:\.\d+)?|[a-z]+)\s*(?P<unit>[a-z]+)$"
)
_EDIT_MAKE_RE = re.compile(
    r"^make\s+the\s+(?P<food>.+?)\s+(?P<qty>\d+(?:\.\d+)?|[a-z]+)\s*(?P<unit>[a-z]+)$"
)
_REMOVE_RE = re.compile(r"^(?:remove|delete)\s+(?:the\s+)?(?P<food>.+)$")
_ADD_RE = re.compile(r"^add\s+(?P<rest>.+)$")
_SET_SLOT_RE = re.compile(r"^(?:this|that)\s+was\s+(?P<slot>breakfast|lunch|dinner|snack)$")


@dataclass(frozen=True)
class ParsedItemPhrase:
    raw_text: str
    quantity: float
    unit: str  # canonical - see chatparser.units.UNIT_WORDS
    state: Optional[str]  # "RAW" | "COOKED" | None
    prep: Optional[str]
    food_text: str


@dataclass(frozen=True)
class ParsedEdit:
    intent: str  # "EDIT_ITEM" | "ADD_ITEM" | "REMOVE_ITEM" | "SET_SLOT"
    target_text: Optional[str] = None  # EDIT_ITEM / REMOVE_ITEM
    quantity: Optional[float] = None  # EDIT_ITEM
    unit: Optional[str] = None  # EDIT_ITEM
    item: Optional[ParsedItemPhrase] = None  # ADD_ITEM
    slot: Optional[str] = None  # SET_SLOT


def _parse_qty(token: str) -> Optional[float]:
    if token in WORD_NUMBERS:
        return float(WORD_NUMBERS[token])
    try:
        return float(token)
    except ValueError:
        return None


def _strip_of_prefix(text: str) -> str:
    return text[3:].strip() if text.startswith("of ") else text


def _extract_state_and_food(rest: str) -> Tuple[Optional[str], Optional[str], str]:
    rest = _strip_of_prefix(rest)
    tokens = rest.split(None, 1)
    if tokens and tokens[0] in _PREP_TO_STATE:
        state, prep = _PREP_TO_STATE[tokens[0]]
        food_text = _strip_of_prefix(tokens[1].strip()) if len(tokens) > 1 else ""
        return state, prep, food_text
    return None, None, rest


def _match_item_phrase(segment: str) -> Optional[ParsedItemPhrase]:
    segment = segment.strip()
    match = _DIGIT_ITEM_RE.match(segment) or _WORD_ITEM_RE.match(segment)
    if not match:
        return None

    quantity = _parse_qty(match.group("qty"))
    if quantity is None:
        return None
    unit = UNIT_WORDS.get(match.group("unit"))
    if unit is None:
        return None

    state, prep, food_text = _extract_state_and_food(match.group("rest").strip())
    if not food_text:
        return None

    return ParsedItemPhrase(
        raw_text=segment, quantity=quantity, unit=unit, state=state, prep=prep, food_text=food_text
    )


def parse_new_item_phrases(normalized_text: str) -> Tuple[List[ParsedItemPhrase], List[str]]:
    """Matched phrases, plus every segment that didn't match at all (§12.13) -
    the caller reports the latter back rather than dropping it."""
    segments = [s for s in _SPLIT_RE.split(normalized_text) if s]
    phrases: List[ParsedItemPhrase] = []
    unconsumed: List[str] = []
    for segment in segments:
        phrase = _match_item_phrase(segment)
        if phrase is not None:
            phrases.append(phrase)
        else:
            unconsumed.append(segment)
    return phrases, unconsumed


def _match_edit_quantity(match: "re.Match") -> Optional[Tuple[float, str]]:
    quantity = _parse_qty(match.group("qty"))
    if quantity is None:
        return None
    unit = UNIT_WORDS.get(match.group("unit"))
    if unit is None:
        return None
    return quantity, unit


def parse_edit_command(normalized_text: str) -> Optional[ParsedEdit]:
    """Tries, in order: EDIT_ITEM (both phrasings), SET_SLOT, ADD_ITEM,
    REMOVE_ITEM. Returns `None` if nothing matches - the caller falls back to
    treating the message as a fresh new-meal description."""
    text = normalized_text.strip()

    match = _EDIT_WAS_ACTUALLY_RE.match(text) or _EDIT_MAKE_RE.match(text)
    if match:
        resolved = _match_edit_quantity(match)
        food = match.group("food").strip()
        if resolved is not None and food:
            quantity, unit = resolved
            return ParsedEdit(intent="EDIT_ITEM", target_text=food, quantity=quantity, unit=unit)

    match = _SET_SLOT_RE.match(text)
    if match:
        return ParsedEdit(intent="SET_SLOT", slot=match.group("slot").upper())

    match = _ADD_RE.match(text)
    if match:
        phrase = _match_item_phrase(match.group("rest"))
        if phrase is not None:
            return ParsedEdit(intent="ADD_ITEM", item=phrase)

    match = _REMOVE_RE.match(text)
    if match:
        food = match.group("food").strip()
        if food:
            return ParsedEdit(intent="REMOVE_ITEM", target_text=food)

    return None
