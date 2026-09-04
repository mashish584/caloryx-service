"""Quantity + unit -> canonical grams (PRD §8).

Grams are the only mass nutrition math ever runs on. `g`/`kg` are universal -
any food can be logged by mass with no catalog entry - anything else (a
household measure or a countable unit) must be declared per food via
`FoodServingUnit`, because a cup of rice and a cup of spinach are not the same
mass (§8).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

_GRAM_UNITS = {"g": 1.0, "kg": 1000.0}


class UnknownServingUnitError(Exception):
    """`unit` is neither a gram unit nor a declared serving unit for this food."""


@dataclass(frozen=True)
class ServingUnit:
    unit: str
    grams: float  # grams per one `unit`, as-served
    type: str  # ServingUnitType value; carried through, not interpreted here


def resolve_grams(quantity: float, unit: str, serving_units: Iterable[ServingUnit]) -> float:
    """`quantity` of `unit` -> grams, using the food's declared serving units."""
    normalized = unit.strip().lower()
    if normalized in _GRAM_UNITS:
        return quantity * _GRAM_UNITS[normalized]

    for serving_unit in serving_units:
        if serving_unit.unit.strip().lower() == normalized:
            return quantity * serving_unit.grams

    raise UnknownServingUnitError(
        "'{}' is not a recognised unit for this food.".format(unit)
    )
