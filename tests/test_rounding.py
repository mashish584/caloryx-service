"""The client renders an optimistic preview that has to reconcile with the
server value (§5.5), so rounding must be half-up like JavaScript's Math.round,
not Python's default round-half-to-even."""
from __future__ import annotations

import pytest

from engine.rounding import round_half_up, round_int, round_to_nearest


@pytest.mark.parametrize("value,expected", [(0.5, 1), (1.5, 2), (2.5, 3), (51.666, 52)])
def test_round_int_is_half_up_not_bankers(value, expected):
    assert round_int(value) == expected


def test_python_builtin_would_disagree():
    assert round(2.5) == 2  # documents why round_int exists
    assert round_int(2.5) == 3


@pytest.mark.parametrize(
    "value,expected", [(1856, 1860), (1854, 1850), (1855, 1860), (2256, 2260)]
)
def test_round_to_nearest_ten(value, expected):
    assert round_to_nearest(value, 10) == expected


def test_granularity_of_one_is_a_plain_round():
    assert round_to_nearest(1856.4, 1) == 1856


def test_negative_values_round_away_from_zero():
    assert round_half_up(-0.36363, 1) == -0.4
