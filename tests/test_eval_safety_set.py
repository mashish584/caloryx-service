"""Held-out safety evaluation set (§12.11, Chunk 8e).

`WELLBEING_FLAG` is never T-1-classified by design (§5.6, Chunk 6b) - it only
ever comes from a T2/T3 envelope, so the actual model-level classification
can't be evaluated without a live OpenAI key, the same LLM-tier gap the main
golden set (`tests/test_eval_golden_set.py`) documents. What this file
measures instead is the first line of defense: `chatparser.has_wellbeing_signal`,
the keyword net deciding whether the real pipeline even spends a T3 call
checking a message at all (§5.6). A false negative here means the model is
never even asked - a materially different, and arguably more important,
failure than a T2/T3 misclassification.

The target is zero false negatives on this held-out starter set. The real
production false-negative-rate target is a clinical/trust-and-safety policy
number this codebase has never been asked to invent - same
placeholder-pending-review posture as `_WELLBEING_FLAG_REPLY`/
`WELLBEING_RESOURCES` (Chunk 6b).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import chatparser

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "eval_safety_set.json"
_SAFETY_SET = json.loads(_FIXTURE_PATH.read_text())
assert _SAFETY_SET["version"] == 1

_POSITIVE_CASES = [c for c in _SAFETY_SET["cases"] if c["expectedFlag"] is True]
_CONTROL_CASES = [c for c in _SAFETY_SET["cases"] if c["expectedFlag"] is False]


@pytest.mark.parametrize("case", _POSITIVE_CASES, ids=[c["id"] for c in _POSITIVE_CASES])
def test_wellbeing_signal_recall(case):
    """Zero false negatives on this held-out set (§12.11) - the metric that
    actually matters here, since a miss means the real pipeline never even
    asks a model."""
    assert chatparser.has_wellbeing_signal(chatparser.normalize_text(case["input"])) is True


@pytest.mark.parametrize("case", _CONTROL_CASES, ids=[c["id"] for c in _CONTROL_CASES])
def test_wellbeing_signal_does_not_fire_on_ordinary_messages(case):
    """Informational, not the named metric - over-triggering just costs one
    extra T3 call (§5.6); a one-sided positive-only set would prove nothing
    about whether the net is at least somewhat targeted."""
    assert chatparser.has_wellbeing_signal(chatparser.normalize_text(case["input"])) is False
