"""Deterministic rounding.

Python's built-in `round` is banker's rounding (round-half-to-even), so
`round(2.5) == 2`. JavaScript's `Math.round` is half-up. The client renders an
optimistic preview that must reconcile with the server value (§5.5), so the two
have to agree on the boundary case; we standardise on half-away-from-zero.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def round_half_up(value: float, ndigits: int = 0) -> float:
    quantum = Decimal(1).scaleb(-ndigits)
    return float(Decimal(repr(float(value))).quantize(quantum, rounding=ROUND_HALF_UP))


def round_int(value: float) -> int:
    return int(round_half_up(value, 0))


def round_to_nearest(value: float, granularity: int) -> int:
    """Round to the nearest multiple of `granularity` (half away from zero)."""
    if granularity <= 1:
        return round_int(value)
    return round_int(round_half_up(value / granularity, 0) * granularity)
