"""Shared test helpers."""
from __future__ import annotations

from datetime import date


def dob_for_age(age: int, *, today: date | None = None) -> date:
    """A birth date that derives to exactly `age` today.

    Fixtures used to carry a literal age. They now carry a date, and it has to
    be relative to today or the derived age drifts by one every year and the
    expected calorie numbers quietly stop matching.
    """
    today = today or date.today()
    try:
        return today.replace(year=today.year - age)
    except ValueError:
        # 29 February in a non-leap target year.
        return today.replace(year=today.year - age, day=28)


def dob_str(age: int) -> str:
    return dob_for_age(age).isoformat()
