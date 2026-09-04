"""Food-match confidence banding (PRD §12.6).

Pure string similarity (`difflib`), not Postgres trigram/embedding search -
zero infrastructure change, stays inside the no-live-DB test convention
(decided when planning Chunk 2b). Revisit if/when catalog scale makes this
insufficient; the call site (`assistant.services._resolve_food_by_name`) is
the only place that would need to change.

This is deliberately *not* the full §7.2 `parse_confidence` formula (token
coverage + match score + match margin + quantity presence + intent clarity) -
that only means something once there's a T2 to calibrate a routing threshold
against (Chunk 4). This module answers one narrower question: how well does
this matched food name resolve to a catalog entry.
"""
from __future__ import annotations

from difflib import SequenceMatcher

# Thresholds are module constants, not buried in the scoring function, so
# they're easy to retune without touching the matching logic itself.
HIGH_THRESHOLD = 0.85
MEDIUM_THRESHOLD = 0.6


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
    return best


def band_for_score(score: float) -> str:
    if score >= HIGH_THRESHOLD:
        return "HIGH"
    if score >= MEDIUM_THRESHOLD:
        return "MEDIUM"
    return "LOW"
