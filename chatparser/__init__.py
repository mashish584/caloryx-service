"""Meal Assistant chat pipeline enums (PRD §9).

Deliberately dependency-free: no Django, no Prisma, no I/O - mirrors
`engine/`/`nutrition/`'s boundary. Chunk 2a only needs the shared vocabulary;
normalization, the T-1/T0/T1 pipeline, and food-match confidence scoring land
in Chunk 2b.
"""
from .enums import (
    DraftStatus,
    ItemResolution,
    MassSource,
    MatchBand,
    ParseTier,
    QuantitySource,
)

__all__ = [
    "DraftStatus",
    "ItemResolution",
    "MassSource",
    "MatchBand",
    "ParseTier",
    "QuantitySource",
]
