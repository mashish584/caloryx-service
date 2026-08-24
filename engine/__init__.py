"""CaloryX calorie & macro engine (PRD §6).

Deliberately dependency-free: no Django, no Prisma, no I/O. The API layer feeds
it a `PlanInput` plus an `EngineConfig` and persists what comes back, which keeps
the PRD's "authoritative calculation lives in a Python service" boundary intact
even though it currently runs in-process.
"""
from .advisories import Advisory, bmi, clamped_advisory, evaluate_profile
from .calculator import (
    EngineError,
    Macros,
    PlanInput,
    PlanReconciliationError,
    PlanResult,
    calculate_bmr,
    calculate_plan,
)
from .config import DEFAULT_CONFIG, EngineConfig, config_from_row
from .enums import ActivityLevel, AuthProvider, Goal, SexAtBirth, UnitSystem

__all__ = [
    "Advisory",
    "ActivityLevel",
    "AuthProvider",
    "DEFAULT_CONFIG",
    "EngineConfig",
    "EngineError",
    "Goal",
    "Macros",
    "PlanInput",
    "PlanReconciliationError",
    "PlanResult",
    "SexAtBirth",
    "UnitSystem",
    "bmi",
    "calculate_bmr",
    "calculate_plan",
    "clamped_advisory",
    "config_from_row",
    "evaluate_profile",
]
