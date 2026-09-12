"""Food-match confidence banding (PRD §12.6).

Pure string similarity (`difflib`), stays inside the no-live-DB test
convention (decided when planning Chunk 2b). At bulk-ingested catalog scale
(USDA/INDB/Open Food Facts) this no longer scans every food: Postgres trigram
search (`meals.repository.search_foods`) narrows the catalog to a candidate
window first, and this module only scores and bands within it (see
`assistant.services._resolve_food_by_name`).

This is deliberately *not* the full §7.2 `parse_confidence` formula (token
coverage + match score + match margin + quantity presence + intent clarity) -
that only means something once there's a T2 to calibrate a routing threshold
against (Chunk 4). This module answers one narrower question: how well does
this matched food name resolve to a catalog entry.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Tuple

# Thresholds are module constants, not buried in the scoring function, so
# they're easy to retune without touching the matching logic itself.
HIGH_THRESHOLD = 0.85
MEDIUM_THRESHOLD = 0.6
# Floor for a candidate containing every query word as a whole word, in any
# order - HIGH, but below an exact substring's 1.0.
WORD_COVERAGE_SCORE = 0.9


def score_food_match(query: str, candidate_name: str) -> float:
    """Partial-ratio similarity, not a plain whole-string ratio.

    A plain `SequenceMatcher(query, candidate).ratio()` penalizes length
    differences directly - "rice" against "Cooked White Rice" scores ~0.38
    (LOW) even though "rice" is an exact substring, because the ratio is
    computed over the combined length of both strings. Since real messages
    say "rice"/"chicken"/"egg" far more often than a food's full catalog
    name, that would make most short, correct mentions resolve as
    low-confidence. Instead: find the best-aligned window of the longer
    string (via the whole-string matcher's own matching blocks) the same
    length as the shorter one, and score the shorter string against that
    window - the standard "partial ratio" technique. An exact substring
    match scores 1.0 regardless of how much longer the candidate is.

    Partial ratio is word-order sensitive, and catalog names (USDA
    especially) invert natural order - "greek yogurt" against "Yogurt,
    Greek, plain, nonfat" partial-ratios 0.5 (LOW). So a candidate holding
    every query word as a whole word scores at least `WORD_COVERAGE_SCORE`.
    """
    query = query.strip().lower()
    candidate = candidate_name.strip().lower()
    if not query or not candidate:
        return 0.0

    shorter, longer = (query, candidate) if len(query) <= len(candidate) else (candidate, query)
    matcher = SequenceMatcher(None, shorter, longer)
    best = 0.0
    for block in matcher.get_matching_blocks():
        if block.size == 0:
            continue
        start = max(0, block.b - block.a)
        window = longer[start : start + len(shorter)]
        best = max(best, SequenceMatcher(None, shorter, window).ratio())
    query_words = _words(query)
    if query_words and query_words <= _words(candidate):
        best = max(best, WORD_COVERAGE_SCORE)
    return best


def band_for_score(score: float) -> str:
    if score >= HIGH_THRESHOLD:
        return "HIGH"
    if score >= MEDIUM_THRESHOLD:
        return "MEDIUM"
    return "LOW"


# -- ranking signals among same-band candidates -------------------------------
#
# At bulk-catalog scale many candidates land in the same band - "egg" is an
# exact substring of "Egg, whole, raw" and of "Bread, egg" alike. Catalog
# names (USDA especially) put the food itself first and qualifiers after
# commas - the *head* - so "is the query the head of this name" separates
# the food from things merely made with or named after it.

_WORD_RE = re.compile(r"[a-z0-9]+")

# Words that don't change *which* food it is: ignored when comparing a query
# to a name's head, so "rice" heads both "Rice, white, cooked" and "Cooked
# White Rice" while "Rice crackers" stays a different food, and not counted
# as extra words, so "Egg, whole, raw, fresh" reads as plainer than "Egg,
# yolk, dried". "fried" is deliberately absent - fried rice/chicken/egg is a
# different food, not a preparation note.
_DESCRIPTOR_WORDS = frozenset(
    {
        # preparation / state / plain variety
        "raw", "cooked", "boiled", "grilled", "roasted", "baked", "steamed",
        "stewed", "braised", "broiled", "sauteed", "poached", "fresh", "plain",
        "whole", "white", "regular", "homemade", "home", "prepared", "fluid",
    }
)

# USDA FNDDS's "unspecified" phrasing - "Rice, cooked, NFS", "Chicken, NS as
# to part and cooking method, skin not eaten", "no added fat". Not counted as
# extra words, but - unlike descriptors - never stripped from a name's head:
# "Chicken skin" is still not "chicken".
_QUALIFIER_WORDS = frozenset(
    {"nfs", "ns", "as", "to", "and", "or", "not", "no", "added", "fat", "part",
     "cooking", "method", "skin", "eaten"}
)
# Marks FNDDS's own generic default entry for a food - what a bare word
# should resolve to when nothing else separates the candidates.
_UNSPECIFIED_WORDS = frozenset({"nfs", "ns"})


def _fold(word: str) -> str:
    """Crude singular form - "bananas"/"banana", "tomatoes"/"tomato",
    "berries"/"berry". Applied to both sides, so an imperfect fold ("hummus"
    -> "hummu") still compares equal to itself."""
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("oes") and len(word) > 4:
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def _words(text: str) -> set:
    return {_fold(w) for w in _WORD_RE.findall(text.lower())}


def match_tie_breaks(query: str, candidate_name: str) -> Tuple[int, bool, int, bool]:
    """`(head_tier, has_all_words, extra_words, is_unspecified)` for ranking
    candidates in the same band, most important first (higher/`True`/fewer
    is better):

    - `head_tier`: 2 when the name's head (text before the first comma)
      *is* the query, descriptor words aside ("egg" -> "Egg, whole, raw");
      1 when the head is part of the query ("brown rice" -> "Rice, brown");
      0 otherwise ("Bread, egg", "Snacks, brown rice chips").
    - `has_all_words`: every query word is a whole word of the name - "white
      rice" prefers "Rice, white" over "Rice, brown"; "rice" never
      "Licorice".
    - `extra_words`: name words beyond the query, descriptors and FNDDS
      qualifiers aside - the plainest variant wins ("Egg, whole, raw, fresh"
      over "Egg, yolk, dried").
    - `is_unspecified`: FNDDS's generic "NFS"/"NS as to ..." entry.
    """
    query_words = _words(query)
    name_words = _words(candidate_name)
    query_core = query_words - _DESCRIPTOR_WORDS
    head_core = _words(candidate_name.split(",", 1)[0]) - _DESCRIPTOR_WORDS
    if query_core and head_core == query_core:
        head_tier = 2
    elif head_core and head_core <= query_core:
        head_tier = 1
    else:
        head_tier = 0
    extra_words = len(name_words - query_words - _DESCRIPTOR_WORDS - _QUALIFIER_WORDS)
    is_unspecified = bool(name_words & _UNSPECIFIED_WORDS)
    return head_tier, query_words <= name_words, extra_words, is_unspecified
