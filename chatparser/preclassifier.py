"""T-1: the non-food pre-classifier (PRD §7.1).

Deliberately narrow: catches obvious greetings/chit-chat so they don't reach
the grammar or (later) a model, at ~0 cost. It is NOT a full food/non-food
classifier - anything it doesn't recognise falls through to T0/T1, and a
message that turns out to have no parseable food in it is handled by the
"no food identified" path (§13), not by this gate trying to be exhaustive.
The full 13-intent taxonomy (SOCIAL vs. OTHER vs. UNCLEAR, distinctly) is
Chunk 6 - this only answers "is this worth attempting a food parse at all."
"""
from __future__ import annotations

# A short message made up entirely of these words is chit-chat, not a food
# description - "hey there", "thank you", "good morning" all match. Real
# food mentions ("rice", "chicken", "200g") never appear here, so this can't
# accidentally swallow a real message; the length cap below is the second
# line of defense in case a food name ever coincides with one of these words.
_GREETING_WORDS = {
    "hi",
    "hello",
    "hey",
    "hiya",
    "yo",
    "sup",
    "thanks",
    "thank",
    "you",
    "ty",
    "good",
    "morning",
    "afternoon",
    "evening",
    "night",
    "bye",
    "goodbye",
    "ok",
    "okay",
    "cool",
    "nice",
    "great",
    "awesome",
    "there",
    "friend",
    "buddy",
    "mate",
}
_MAX_GREETING_WORDS = 5

_GREETING_PREFIXES = (
    "how are you",
    "how's it going",
    "hows it going",
    "what's up",
    "whats up",
    "how do you do",
)


def is_non_food_greeting(normalized_text: str) -> bool:
    """`normalized_text` should already be `normalize.normalize_text`-ed."""
    words = normalized_text.split()
    if words and len(words) <= _MAX_GREETING_WORDS and all(w in _GREETING_WORDS for w in words):
        return True
    return normalized_text.startswith(_GREETING_PREFIXES)
