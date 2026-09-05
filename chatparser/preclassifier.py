"""T-1: the pre-classifier for non-logging intents (PRD §5.5, §7.1).

Deliberately keyword/regex-based, not exhaustive: each detector is narrow on
purpose, since misclassifying a real food mention as chit-chat/help/etc. is
worse than leaving it to fall through to T1's grammar (and, if that also
finds nothing, T2/T3 - which can classify any of these intents too, from the
same closed set, when a phrasing's keywords miss here). `classify_t1_intent`
tries each in a fixed order and returns `None` when nothing matches, meaning
"let this continue down the pipeline" - never "classify as OTHER" (`OTHER` is
only ever the pipeline's own last-resort fallback once every tier has tried).
"""
from __future__ import annotations

import re
from typing import Iterable, Optional


def _contains_word(text: str, words: Iterable[str]) -> bool:
    """Whole-word containment for single-word triggers - plain substring
    matching would let "app" match inside "happy" or "carb" inside
    "carbonated". Multi-word phrases (a full trigger like "should i try")
    don't need this - they're specific enough that a substring match is
    already unambiguous."""
    return any(re.search(r"\b{}\b".format(re.escape(word)), text) for word in words)


# -- SOCIAL -------------------------------------------------------------------

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


# -- APP_HELP -------------------------------------------------------------

_APP_HELP_QUESTION_STARTS = ("how do i", "how can i", "how does", "where do i", "where can i")
_APP_HELP_NOUNS = (
    "goal",
    "streak",
    "setting",
    "profile",
    "reminder",
    "notification",
    "password",
    "account",
    "unit",
    "app",
)


def is_app_help(normalized_text: str) -> bool:
    if not normalized_text.startswith(_APP_HELP_QUESTION_STARTS):
        return False
    return _contains_word(normalized_text, _APP_HELP_NOUNS)


# -- DIARY_QUERY ------------------------------------------------------------

_DIARY_QUERY_TRIGGERS = (
    "how many calories do i have left",
    "how many calories left",
    "calories left",
    "calories remaining",
    "how much have i eaten",
    "what have i eaten",
    "what did i eat today",
    "how many calories have i eaten",
    "how many calories did i eat",
    "what did i log today",
    "logged today",
)
# Answering these inline would need real date-range analysis this chunk
# doesn't build (§5.5: "cross-day/analysis -> deep link to Insights, don't
# answer inline") - an honest v1 boundary, not silently ignored.
_DIARY_QUERY_TREND_WORDS = ("week", "month", "yesterday", "average", "trend", "compared")


def is_diary_query(normalized_text: str) -> bool:
    return any(trigger in normalized_text for trigger in _DIARY_QUERY_TRIGGERS)


def is_diary_query_a_trend_question(normalized_text: str) -> bool:
    """Only meaningful when `is_diary_query` is already `True` - decides
    which of the two §5.5 replies fires, not whether one does."""
    return any(word in normalized_text for word in _DIARY_QUERY_TREND_WORDS)


# -- NUTRITION_QA -------------------------------------------------------------

# Two-stage on purpose: a loose trigger classifies the *intent*; a stricter
# regex separately extracts *which food* for the answer (see
# `extract_nutrition_qa_food`) - a trigger match with no extractable food
# still classifies as NUTRITION_QA (answered with a deflect), rather than
# guessing.
_NUTRITION_QUESTION_STARTS = ("how much", "how many")
_NUTRITION_MACRO_WORDS = ("calorie", "calories", "protein", "carb", "carbs", "fat", "fiber")
# §5.5's own dividing line: report what a food contains, never judge whether
# it's good for this person. Judgment framing is never NUTRITION_QA, even
# alongside a macro word ("is quinoa healthy" != "how much protein is in
# quinoa").
_NUTRITION_JUDGMENT_WORDS = (
    "healthy",
    "good for",
    "bad for",
    "should i eat",
    "recommend",
    "best for",
)
# A question about the user's *own* consumption ("how many calories did I
# eat") is DIARY_QUERY-shaped, not a food-fact lookup, even though it shares
# the same macro/calorie vocabulary - checked first, in `classify_t1_intent`'s
# fixed order, but also excluded here directly so `is_nutrition_qa` alone is
# never `True` for this phrasing.
_NUTRITION_PERSONAL_WORDS = ("i eat", "did i", "have i", "i've eaten", "i ate")
_NUTRITION_QA_EXTRACT_RE = re.compile(
    r"^how (?:much|many) \w+ (?:is |are )?in (?:an? |the )?(?P<food>.+?)(?:\s+have)?\??$"
)


def is_nutrition_qa(normalized_text: str) -> bool:
    if any(word in normalized_text for word in _NUTRITION_JUDGMENT_WORDS):
        return False
    if any(word in normalized_text for word in _NUTRITION_PERSONAL_WORDS):
        return False
    if not normalized_text.startswith(_NUTRITION_QUESTION_STARTS):
        return False
    return _contains_word(normalized_text, _NUTRITION_MACRO_WORDS)


def extract_nutrition_qa_food(normalized_text: str) -> Optional[str]:
    match = _NUTRITION_QA_EXTRACT_RE.match(normalized_text)
    return match.group("food").strip() if match else None


# -- ADVICE_SEEKING -----------------------------------------------------------

# A consistent decline regardless of phrasing confidence (§5.5: "a hard
# boundary regardless of how confidently the underlying model could
# answer") - no sub-classification, just a trigger list.
_ADVICE_SEEKING_TRIGGERS = (
    "should i try",
    "should i do",
    "should i eat",
    "should i fast",
    "is keto",
    "is intermittent fasting",
    "diet plan",
    "which diet",
    "supplement",
    "is my target right",
    "is my calorie target",
    "enough for me",
    "medical condition",
    "diabetes",
    "pcos",
    "thyroid",
)


def is_advice_seeking(normalized_text: str) -> bool:
    return any(trigger in normalized_text for trigger in _ADVICE_SEEKING_TRIGGERS)


# -- UNCLEAR ------------------------------------------------------------------

# Deliberately narrow: a keyboard-mash like "asdfgh" is real §5.5 example
# traffic, but reliably detecting gibberish (vs. a short food name) via
# keywords isn't - left to fall through to T1/T2 rather than guessed at
# here. This only catches genuinely content-free replies.
_UNCLEAR_EXACT = {
    "yes",
    "no",
    "yeah",
    "yep",
    "nope",
    "sure",
    "maybe",
    "idk",
    "i dont know",
    "the usual",
    "same as usual",
    "same as last time",
}


def is_unclear(normalized_text: str) -> bool:
    text = normalized_text.strip()
    if text in _UNCLEAR_EXACT:
        return True
    # Punctuation/whitespace only, nothing alphabetic at all.
    return bool(text) and not any(c.isalpha() for c in text)


# -- dispatch -----------------------------------------------------------------


def classify_t1_intent(normalized_text: str) -> Optional[str]:
    """First match wins; `None` means "not recognized here" - the caller
    continues down the pipeline (T1's grammar, then T2/T3), not "classify as
    OTHER" (§5.5's `ChatIntent` values, minus the logging five and
    `WELLBEING_FLAG` - Chunk 6b)."""
    if is_non_food_greeting(normalized_text):
        return "SOCIAL"
    if is_app_help(normalized_text):
        return "APP_HELP"
    if is_diary_query(normalized_text):
        return "DIARY_QUERY"
    if is_nutrition_qa(normalized_text):
        return "NUTRITION_QA"
    if is_advice_seeking(normalized_text):
        return "ADVICE_SEEKING"
    if is_unclear(normalized_text):
        return "UNCLEAR"
    return None
