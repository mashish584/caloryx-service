"""T1: the deterministic parser (PRD §7.5).

Three grammars, all pure regex + vocabulary lookup - no catalog access, no I/O:

- `parse_new_item_phrases` - a quantity *and* unit in either order, splitting
  a message on common connectors so "200g rice and noodles 1 bowl" yields two
  phrases. `QTY + UNIT + [STATE/PREP] + FOOD` ("200g rice") is tried first;
  `[STATE/PREP] + FOOD + QTY + UNIT` ("rice 200g", "noodles 1 bowl") second.
  Food-first phrasing is extremely common in chat and needs no model to read -
  see `_POSTFIX_ITEM_RE`.
- `parse_food_mentions` - the weaker second pass, run by the caller over
  whatever `parse_new_item_phrases` could not consume: `COUNT + FOOD`
  ("2 rotis") and, when the caller opts in, a bare `FOOD` ("noodles"). These
  carry no unit, so they resolve no mass here; they name a food and (maybe) a
  count, and `assistant.services` finishes them through §5.1.1a's
  quantity-resolution ladder, which needs catalog/history access this pure
  module doesn't have.
- `parse_edit_command` - the §7.5 edit patterns (`EDIT_ITEM`/`ADD_ITEM`/
  `REMOVE_ITEM`/`SET_SLOT`), tried in a fixed order.

`parse_new_item_phrases` still requires an explicit quantity + unit, same as
Chunk 2a's structured endpoint - the default-serving inference lives behind
`parse_food_mentions` instead, so a caller that wants only stated amounts can
just not run the second pass. A segment no grammar matches is reported back
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

# Food-first phrasing: "rice 200g", "noodles 1 bowl", "boiled egg 2 pieces".
# One pattern covers digit and word-number quantities alike, since `unit` is
# anchored to the end and can hold no space: the only split the engine can
# ever find puts `unit` on the last token and `qty` on the one before it (or
# glued to it, "200g"), so the non-greedy `rest` can't be talked into a wrong
# reading of a multi-word food name ("chicken tikka masala one bowl").
_POSTFIX_ITEM_RE = re.compile(
    r"^(?P<rest>.+?)\s+(?P<qty>\d+(?:\.\d+)?|[a-z]+)\s*(?P<unit>[a-z]+)$"
)

# Second pass (`parse_food_mentions`), both unit-less:
# "2 rotis"/"two boiled eggs" - a stated count, no unit distinct from the
# food name - and a bare food name with no amount at all.
_COUNT_ONLY_RE = re.compile(r"^(?P<qty>\d+(?:\.\d+)?|[a-z]+)\s+(?P<rest>.+)$")

# Both second-pass grammars are reading segments nothing else could read, so
# they need their own brakes - a food name is short, and prose is not.
# Applies to the food part, so "3 boiled eggs" (2 food words) passes and
# "a really long bit of prose here" does not.
_MAX_MENTION_FOOD_WORDS = 4

# An implausibly large count is almost never a count - it's a mass with a unit
# this vocabulary doesn't know yet ("500 ml milk", "200 smidges rice"), and
# reading it as "500 of the food named ml milk" would be a worse guess than
# admitting the miss. Set well above any real per-meal count so small
# countables ("24 almonds", "30 grapes") still read as counts.
_MAX_MENTION_COUNT = 50.0

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
    # Provenance overrides (AI Meal Assistant PRD §5.1.1a, Chunk 4b) - every T1
    # match is stated/direct, so these defaults reproduce today's behavior
    # exactly. A quantity-resolution-ladder-produced phrase (assistant.services)
    # sets both explicitly instead of leaving them to be inferred from `unit`.
    quantity_source: str = "EXPLICIT"
    mass_source: Optional[str] = None


@dataclass(frozen=True)
class ParsedFoodMention:
    """A food this module could name but not measure - `parse_food_mentions`'s
    output. `count` is a stated number of servings ("2 rotis") or `None` for a
    bare mention ("noodles"); either way the *mass* is unknown here and only
    §5.1.1a's ladder, in `assistant.services`, can supply it. Deliberately not
    a `ParsedItemPhrase`: that type's `quantity`/`unit` are resolved grams-or-
    serving-unit facts, and there is nothing honest to put in them yet."""

    raw_text: str
    count: Optional[float]
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


def _item_phrase_from_match(segment: str, match: "re.Match") -> Optional[ParsedItemPhrase]:
    """Shared tail of the prefix and postfix grammars - both name their groups
    `qty`/`unit`/`rest`, so the only thing that differs is where in the
    segment they sat."""
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


def _match_item_phrase(segment: str) -> Optional[ParsedItemPhrase]:
    """Quantity-first, then food-first. Order matters: the prefix grammar is
    the older, better-calibrated one, so a segment both could read keeps
    exactly the reading it has today."""
    segment = segment.strip()

    match = _DIGIT_ITEM_RE.match(segment) or _WORD_ITEM_RE.match(segment)
    if match is not None:
        phrase = _item_phrase_from_match(segment, match)
        if phrase is not None:
            return phrase

    match = _POSTFIX_ITEM_RE.match(segment)
    if match is not None:
        return _item_phrase_from_match(segment, match)

    return None


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


def _match_food_mention(
    segment: str, *, allow_count_only: bool, allow_bare_food: bool
) -> Optional[ParsedFoodMention]:
    """`COUNT + FOOD` first, then (opt-in) a bare `FOOD`. A count-only segment
    is only reachable here when no unit word was found, since `_match_item_phrase`
    already consumed anything with one - so "2 rotis" lands here while
    "2 pieces roti" never does."""
    segment = segment.strip()
    if not segment:
        return None

    if allow_count_only:
        match = _COUNT_ONLY_RE.match(segment)
        if match is not None:
            count = _parse_qty(match.group("qty"))
            if count is not None and 0 < count <= _MAX_MENTION_COUNT:
                state, prep, food_text = _extract_state_and_food(match.group("rest").strip())
                if food_text and len(food_text.split()) <= _MAX_MENTION_FOOD_WORDS:
                    return ParsedFoodMention(
                        raw_text=segment, count=count, state=state, prep=prep, food_text=food_text
                    )

    if not allow_bare_food:
        return None

    state, prep, food_text = _extract_state_and_food(segment)
    if not food_text or len(food_text.split()) > _MAX_MENTION_FOOD_WORDS:
        return None
    return ParsedFoodMention(
        raw_text=segment, count=None, state=state, prep=prep, food_text=food_text
    )


def parse_food_mentions(
    segments: List[str], *, allow_count_only: bool = True, allow_bare_food: bool = False
) -> Tuple[List[ParsedFoodMention], List[str]]:
    """Second pass over `parse_new_item_phrases`'s unconsumed segments: the
    mentions it can name, plus whatever still went unread (§12.13 - the
    caller reports those back exactly as before).

    Both grammars are switches rather than always-on, because they trade
    honesty for reach in different amounts. `allow_count_only` ("2 rotis")
    reads a quantity the user actually stated and only assumes the mass, so
    it defaults on. `allow_bare_food` ("noodles") assumes *both*, on the
    weakest possible signal, and defaults off - the caller's setting is what
    decides (see `settings.PARSER_BARE_FOOD_MENTION_ENABLED`).

    Passing neither is valid and is exactly today's behavior: every segment
    comes straight back as unconsumed.
    """
    mentions: List[ParsedFoodMention] = []
    unconsumed: List[str] = []
    for segment in segments:
        mention = _match_food_mention(
            segment, allow_count_only=allow_count_only, allow_bare_food=allow_bare_food
        )
        if mention is not None:
            mentions.append(mention)
        else:
            unconsumed.append(segment)
    return mentions, unconsumed


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
