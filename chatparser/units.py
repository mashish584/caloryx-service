"""Surface-form vocabulary for the T1 grammar (PRD §7.5).

A hardcoded, curated list rather than catalog-derived: keeps this module
pure/DB-free like the rest of `chatparser`, and an unrecognised unit word
fails exactly the way an unrecognised unit already does in
`nutrition.units.resolve_grams` - a normal, self-limiting failure (that
segment becomes unconsumed text, §12.13), not a systemic gap.

`UNIT_WORDS` maps a surface form to the canonical unit string
`nutrition.units.resolve_grams` expects - which is, in turn, whatever a
food's `FoodServingUnit.unit` actually is. Plurals/synonyms fold down to the
singular canonical form; g/kg are handled by `nutrition.units` itself and
don't need an entry here.
"""
from __future__ import annotations

UNIT_WORDS = {
    "g": "g",
    "gram": "g",
    "grams": "g",
    "kg": "kg",
    "kgs": "kg",
    "kilogram": "kg",
    "kilograms": "kg",
    "katori": "katori",
    "katoris": "katori",
    "cup": "cup",
    "cups": "cup",
    "piece": "piece",
    "pieces": "piece",
    "tbsp": "tbsp",
    "tablespoon": "tbsp",
    "tablespoons": "tbsp",
    "tsp": "tsp",
    "teaspoon": "tsp",
    "teaspoons": "tsp",
    "slice": "slice",
    "slices": "slice",
    "bowl": "bowl",
    "bowls": "bowl",
    "plate": "plate",
    "plates": "plate",
}

WORD_NUMBERS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
