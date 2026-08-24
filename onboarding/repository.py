"""Profile, plan, and engine-config persistence (Prisma)."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from common.db import get_client
from engine import DEFAULT_CONFIG, EngineConfig, PlanResult, config_from_row

logger = logging.getLogger(__name__)

_PROFILE_WITH_PLAN = {"plan": True}

# The active config changes rarely but is read on every plan calculation, so it
# is cached briefly rather than fetched per request. A retune goes live within
# one TTL without a deploy (PRD §10).
_CONFIG_TTL_SECONDS = 60.0
_config_cache: Dict[str, Any] = {"value": None, "expires_at": 0.0}
_config_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# -- engine config ---------------------------------------------------------


def get_active_engine_config(*, force_refresh: bool = False) -> EngineConfig:
    """Active config row, or the compiled defaults when none is seeded.

    A database hiccup must never cost a user their plan, so a failed lookup
    falls back to the defaults instead of raising.
    """
    now = time.monotonic()
    cached = _config_cache["value"]
    if not force_refresh and cached is not None and now < _config_cache["expires_at"]:
        return cached

    with _config_lock:
        if not force_refresh and _config_cache["value"] is not None and time.monotonic() < _config_cache["expires_at"]:
            return _config_cache["value"]
        try:
            row = get_client().engineconfig.find_first(where={"isActive": True})
            config = config_from_row(row) if row else DEFAULT_CONFIG
        except Exception as exc:  # noqa: BLE001 - never block a plan on config I/O
            logger.warning("engine config lookup failed, using defaults: %s", exc)
            config = DEFAULT_CONFIG
        _config_cache["value"] = config
        _config_cache["expires_at"] = time.monotonic() + _CONFIG_TTL_SECONDS
        return config


def invalidate_engine_config_cache() -> None:
    with _config_lock:
        _config_cache["value"] = None
        _config_cache["expires_at"] = 0.0


# -- profile ---------------------------------------------------------------


def get_profile(user_id: str, *, include_plan: bool = True) -> Optional[Any]:
    return get_client().profile.find_unique(
        where={"userId": user_id},
        include=_PROFILE_WITH_PLAN if include_plan else None,
    )


def upsert_profile(user_id: str, data: Dict[str, Any]) -> Any:
    """Idempotent upsert so a user stepping back and forth through the flow
    (PRD §4) never creates a second profile."""
    payload = dict(data)
    return get_client().profile.upsert(
        where={"userId": user_id},
        data={
            "create": dict(payload, user={"connect": {"id": user_id}}),
            "update": payload,
        },
        include=_PROFILE_WITH_PLAN,
    )


def mark_onboarded(user_id: str) -> Any:
    return get_client().profile.update(
        where={"userId": user_id},
        data={"onboardedAt": _now()},
        include=_PROFILE_WITH_PLAN,
    )


# -- plan ------------------------------------------------------------------


def upsert_plan(profile_id: str, result: PlanResult) -> Any:
    payload = {
        "caloriesKcal": result.calories_kcal,
        "proteinG": result.macros.protein_g,
        "carbsG": result.macros.carbs_g,
        "fatG": result.macros.fat_g,
        "fiberG": result.macros.fiber_g,
        "bmr": result.bmr,
        "tdee": result.tdee,
        "clamped": result.clamped,
        "isEstimate": result.is_estimate,
        "adjustmentKcal": result.adjustment_kcal,
        "weeklyChangeKg": result.weekly_change_kg,
        "computedAt": _now(),
    }
    if result.config.id:
        payload["engineConfig"] = {"connect": {"id": result.config.id}}

    return get_client().plan.upsert(
        where={"profileId": profile_id},
        data={
            "create": dict(payload, profile={"connect": {"id": profile_id}}),
            "update": payload,
        },
    )


def get_plan(profile_id: str) -> Optional[Any]:
    return get_client().plan.find_unique(where={"profileId": profile_id})
