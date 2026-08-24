"""Request/response shapes for the onboarding flow.

Validation posture follows PRD §9: hard physiological caps reject, everything
softer comes back as a non-blocking advisory (see engine/advisories.py) so the
user is never trapped mid-flow.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional

from django.conf import settings
from rest_framework import serializers

from common.exceptions import AgeBelowMinimumError
from engine.enums import ActivityLevel, Goal, SexAtBirth, UnitSystem

# Hard physiological bounds. Outside these we reject; inside but unusual is a
# warning only (engine.advisories.SOFT_*).
WEIGHT_KG_RANGE = (20.0, 500.0)
HEIGHT_CM_RANGE = (50.0, 272.0)

# Onboarding v7 requires a binary answer; UNSPECIFIED exists in the schema purely
# as a defensive engine fallback (§6.4) and is never accepted over the API.
ONBOARDING_SEX_CHOICES = [SexAtBirth.MALE.value, SexAtBirth.FEMALE.value]


def _age_from_dob(dob: date, today: Optional[date] = None) -> int:
    today = today or date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


class ProfileUpsertSerializer(serializers.Serializer):
    """POST /onboarding/profile.

    Everything is stored metric (§5.2); `preferredUnits` records the display
    choice so the app can restore it across devices.
    """

    sexAtBirth = serializers.ChoiceField(choices=ONBOARDING_SEX_CHOICES)
    # PRD §9 asks for a real date entry rather than a tickbox. Clients may send
    # the date and let the server derive the age, which keeps the check
    # authoritative; `age` remains accepted for offline-computed submissions.
    age = serializers.IntegerField(required=False)
    dateOfBirth = serializers.DateField(required=False)
    weightKg = serializers.FloatField(
        min_value=WEIGHT_KG_RANGE[0], max_value=WEIGHT_KG_RANGE[1]
    )
    heightCm = serializers.FloatField(
        min_value=HEIGHT_CM_RANGE[0], max_value=HEIGHT_CM_RANGE[1]
    )
    targetWeightKg = serializers.FloatField(
        required=False,
        allow_null=True,
        min_value=WEIGHT_KG_RANGE[0],
        max_value=WEIGHT_KG_RANGE[1],
    )
    goal = serializers.ChoiceField(choices=[g.value for g in Goal])
    activityLevel = serializers.ChoiceField(choices=[a.value for a in ActivityLevel])
    preferredUnits = serializers.ChoiceField(
        choices=[u.value for u in UnitSystem], default=UnitSystem.METRIC.value
    )

    def validate(self, attrs: Dict[str, Any]) -> Dict[str, Any]:
        age = attrs.get("age")
        dob = attrs.pop("dateOfBirth", None)

        if dob is not None:
            # The date wins when a client sends both: it cannot go stale.
            age = _age_from_dob(dob)
        if age is None:
            raise serializers.ValidationError(
                {"age": "Provide either `age` or `dateOfBirth`."}
            )

        if age < settings.MINIMUM_AGE_YEARS:
            # §9: below the minimum age we block rather than warn. Distinct code
            # so the client can show the dedicated screen instead of a field error.
            raise AgeBelowMinimumError(
                "CaloryX is available to people aged {} and over.".format(
                    settings.MINIMUM_AGE_YEARS
                ),
                details={"minimumAge": settings.MINIMUM_AGE_YEARS, "age": age},
            )
        if age > settings.MAXIMUM_AGE_YEARS:
            raise serializers.ValidationError(
                {"age": "Age must be {} or below.".format(settings.MAXIMUM_AGE_YEARS)}
            )

        attrs["age"] = age
        return attrs


class CompleteSerializer(serializers.Serializer):
    """POST /onboarding/complete. Body is optional; present for future opt-ins."""


def serialize_profile(profile: Any) -> Dict[str, Any]:
    return {
        "id": profile.id,
        "sexAtBirth": profile.sexAtBirth,
        "age": profile.age,
        "weightKg": profile.weightKg,
        "heightCm": profile.heightCm,
        "targetWeightKg": profile.targetWeightKg,
        "goal": profile.goal,
        "activityLevel": profile.activityLevel,
        "preferredUnits": profile.preferredUnits,
        "onboardedAt": profile.onboardedAt.isoformat() if profile.onboardedAt else None,
        "updatedAt": profile.updatedAt.isoformat(),
    }


def serialize_stored_plan(plan: Any) -> Dict[str, Any]:
    """Render a persisted Plan row in the same shape the calculator returns, so
    GET /onboarding/plan and POST /onboarding/plan are interchangeable to the client."""
    protein_kcal = plan.proteinG * 4
    carbs_kcal = plan.carbsG * 4
    fat_kcal = plan.fatG * 9
    return {
        "calories": plan.caloriesKcal,
        "macros": {
            "proteinG": plan.proteinG,
            "carbsG": plan.carbsG,
            "fatG": plan.fatG,
            "fiberG": plan.fiberG,
        },
        "macroEnergyKcal": {
            "protein": protein_kcal,
            "carbs": carbs_kcal,
            "fat": fat_kcal,
            "total": protein_kcal + carbs_kcal + fat_kcal,
        },
        "rationale": {
            "adjustmentKcal": plan.adjustmentKcal,
            "weeklyChangeKg": plan.weeklyChangeKg,
            "clamped": plan.clamped,
        },
        "isEstimate": plan.isEstimate,
        "bmr": plan.bmr,
        "tdee": plan.tdee,
        "computedAt": plan.computedAt.isoformat(),
    }
