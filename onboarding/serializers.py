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
from engine.advisories import SOFT_HEIGHT_CM, SOFT_WEIGHT_KG
from engine.enums import (
    ActivityLevel,
    AdvisoryCode,
    AdvisoryField,
    AdvisorySeverity,
    Goal,
    HeightUnit,
    SexAtBirth,
    WeightUnit,
)

# Hard physiological bounds. Outside these we reject; inside but unusual is a
# warning only (engine.advisories.SOFT_*).
WEIGHT_KG_RANGE = (20.0, 500.0)
HEIGHT_CM_RANGE = (50.0, 272.0)

# Onboarding v7 requires a binary answer; UNSPECIFIED exists in the schema purely
# as a defensive engine fallback (§6.4) and is never accepted over the API.
ONBOARDING_SEX_CHOICES = [SexAtBirth.MALE.value, SexAtBirth.FEMALE.value]


def validation_bounds() -> Dict[str, Any]:
    """What the API rejects, and what it merely warns about (§9).

    Published through GET /onboarding/config so the client can stop a bad value
    at the field instead of discovering it as a 400 four screens later. It reads
    the same constants the serializers below enforce, and the age limits straight
    from settings - those are env-tunable, so a client's hardcoded copy would go
    stale the moment a market moved them. `tests/test_config_bounds.py` fails if
    what is advertised here stops matching what is enforced.

    Soft bounds do not reject: inside the hard caps but outside these, the API
    accepts the value and returns an advisory (engine/advisories.py).

    `age` bounds apply to the age *derived* from `dateOfBirth` (§9). A client
    constrains its date picker from them rather than hardcoding a range:
    maxDate = today - age.min years, minDate = today - age.max years.
    """
    return {
        "age": {
            "min": settings.MINIMUM_AGE_YEARS,
            "max": settings.MAXIMUM_AGE_YEARS,
        },
        "weightKg": {
            "min": WEIGHT_KG_RANGE[0],
            "max": WEIGHT_KG_RANGE[1],
            "softMin": SOFT_WEIGHT_KG[0],
            "softMax": SOFT_WEIGHT_KG[1],
        },
        "heightCm": {
            "min": HEIGHT_CM_RANGE[0],
            "max": HEIGHT_CM_RANGE[1],
            "softMin": SOFT_HEIGHT_CM[0],
            "softMax": SOFT_HEIGHT_CM[1],
        },
    }


def age_from_dob(dob: date, today: Optional[date] = None) -> int:
    """Whole years elapsed, the way a birthday works.

    The tuple comparison is the whole point: someone born in December is still
    the younger age until December comes round again. Age is derived here on
    every read rather than stored, so it never goes stale.
    """
    today = today or date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


class PreferredUnitsSerializer(serializers.Serializer):
    """How the client renders measurements (§5.2).

    Display only: values on the wire and in the database are always kg and cm,
    and the client converts at its own boundary. Two units rather than one
    system, because kg + ft/in is a real choice a single METRIC/IMPERIAL flag
    could not represent. Weight covers current and target weight alike - they
    are never shown in different units.
    """

    weight = serializers.ChoiceField(choices=[u.value for u in WeightUnit])
    height = serializers.ChoiceField(choices=[u.value for u in HeightUnit])


DEFAULT_PREFERRED_UNITS = {
    "weight": WeightUnit.KG.value,
    "height": HeightUnit.CM.value,
}


class ProfileUpsertSerializer(serializers.Serializer):
    """POST /onboarding/profile.

    Everything is stored metric (§5.2); `preferredUnits` records the display
    choice so the app can restore it across devices.
    """

    sexAtBirth = serializers.ChoiceField(choices=ONBOARDING_SEX_CHOICES)
    # PRD §9: a real date entry, not a tickbox and not a self-reported integer.
    # Age is derived from this and never stored, so it cannot freeze at signup.
    dateOfBirth = serializers.DateField()
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
    preferredUnits = PreferredUnitsSerializer(default=DEFAULT_PREFERRED_UNITS)

    def validate(self, attrs: Dict[str, Any]) -> Dict[str, Any]:
        # `dateOfBirth` stays in attrs: it is what gets persisted. Age is derived
        # for the §9 gate here, and again on every read - it is never stored.
        dob = attrs["dateOfBirth"]

        if dob > date.today():
            # Would otherwise fall through as a negative age and trip the
            # under-18 gate, whose copy makes no sense for a future date.
            raise serializers.ValidationError(
                {"dateOfBirth": "Date of birth cannot be in the future."}
            )

        age = age_from_dob(dob)

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
                {
                    "dateOfBirth": "Age must be {} or below.".format(
                        settings.MAXIMUM_AGE_YEARS
                    )
                }
            )

        # TRANSITIONAL - `Profile.age` is still NOT NULL until the contract-phase
        # push drops it. Nothing reads it: age comes from `dateOfBirth` on every
        # read. Delete this line with the column.
        attrs["age"] = age
        return attrs


class CompleteSerializer(serializers.Serializer):
    """POST /onboarding/complete. Body is optional; present for future opt-ins."""


def serialize_profile(profile: Any) -> Dict[str, Any]:
    return {
        "id": profile.id,
        "sexAtBirth": profile.sexAtBirth,
        "dateOfBirth": profile.dateOfBirth.isoformat() if profile.dateOfBirth else None,
        "weightKg": profile.weightKg,
        "heightCm": profile.heightCm,
        "targetWeightKg": profile.targetWeightKg,
        "goal": profile.goal,
        "activityLevel": profile.activityLevel,
        "preferredUnits": {
            "weight": profile.weightUnit,
            "height": profile.heightUnit,
        },
        "onboardedAt": profile.onboardedAt.isoformat() if profile.onboardedAt else None,
        "updatedAt": profile.updatedAt.isoformat(),
    }


class AdvisoryPatchSerializer(serializers.Serializer):
    """The request-body fragment a one-tap option would resend (§9).

    Every field is optional and every name is a real `ProfileUpsertSerializer`
    field: the client merges this into the upsert body and resends it, so a key
    the request does not accept would produce a 400 the user cannot act on.
    `tests/test_advisories.py` enforces that subset relation.

    Only `goal` is produced today. The wider shape is deliberate - it is the
    stable contract, so a future advisory that corrects a measurement is not a
    breaking type change for generated clients.

    Scoped to what a hint could plausibly suggest changing. `sexAtBirth` and
    `dateOfBirth` are facts rather than choices, and `preferredUnits` is a
    display setting - no advisory corrects any of the three.
    """

    weightKg = serializers.FloatField(required=False)
    heightCm = serializers.FloatField(required=False)
    targetWeightKg = serializers.FloatField(required=False, allow_null=True)
    goal = serializers.ChoiceField(choices=[g.value for g in Goal], required=False)
    activityLevel = serializers.ChoiceField(
        choices=[a.value for a in ActivityLevel], required=False
    )


class AdvisoryOptionSerializer(serializers.Serializer):
    """One-tap correction offered by an advisory (see engine.advisories).

    `id` is deliberately a free string: the client renders `label` and applies
    `patch`, so it serves only as a list key and an analytics label.
    """

    id = serializers.CharField()
    label = serializers.CharField()
    patch = AdvisoryPatchSerializer()


class AdvisorySerializer(serializers.Serializer):
    """A non-blocking hint (§9). The client switches on `code` for copy, styles
    by `severity`, and attaches the message to the input named by `field`."""

    code = serializers.ChoiceField(choices=[c.value for c in AdvisoryCode])
    message = serializers.CharField()
    field = serializers.ChoiceField(
        choices=[f.value for f in AdvisoryField], required=False
    )
    severity = serializers.ChoiceField(choices=[s.value for s in AdvisorySeverity])
    options = AdvisoryOptionSerializer(many=True, required=False)


class ProfileSerializer(serializers.Serializer):
    """Mirrors `serialize_profile`."""

    id = serializers.CharField()
    sexAtBirth = serializers.ChoiceField(choices=[s.value for s in SexAtBirth])
    # The client derives the age it displays; the server never stores one.
    dateOfBirth = serializers.DateField(allow_null=True)
    weightKg = serializers.FloatField()
    heightCm = serializers.FloatField()
    targetWeightKg = serializers.FloatField(allow_null=True)
    goal = serializers.ChoiceField(choices=[g.value for g in Goal])
    activityLevel = serializers.ChoiceField(choices=[a.value for a in ActivityLevel])
    preferredUnits = PreferredUnitsSerializer()
    onboardedAt = serializers.DateTimeField(allow_null=True)
    updatedAt = serializers.DateTimeField()


class ProfileStateResponseSerializer(serializers.Serializer):
    """GET /onboarding/profile when no profile exists yet."""

    profile = ProfileSerializer(allow_null=True)
    advisories = AdvisorySerializer(many=True)


class ProfileUpsertResponseSerializer(serializers.Serializer):
    """POST /onboarding/profile. `services.save_profile` always has a profile,
    unlike the GET above where it may be None before onboarding starts."""

    profile = ProfileSerializer()
    advisories = AdvisorySerializer(many=True)


class MacrosSerializer(serializers.Serializer):
    proteinG = serializers.IntegerField()
    carbsG = serializers.IntegerField()
    fatG = serializers.IntegerField()
    fiberG = serializers.IntegerField()


class MacroEnergyKcalSerializer(serializers.Serializer):
    protein = serializers.IntegerField()
    carbs = serializers.IntegerField()
    fat = serializers.IntegerField()
    total = serializers.IntegerField()


class PlanRationaleSerializer(serializers.Serializer):
    """POST /onboarding/plan. See `PlanResult.to_response`."""

    adjustmentKcal = serializers.IntegerField()
    weeklyChangeKg = serializers.FloatField()
    clamped = serializers.BooleanField()
    safetyFloorKcal = serializers.IntegerField()
    requestedAdjustmentKcal = serializers.IntegerField()


class PlanResponseSerializer(serializers.Serializer):
    """POST and GET /onboarding/plan - one shape for both.

    They were separate once and drifted twice: the stored form first lost the
    §6.2 clamp fields, which cost a resumed plan its explanation, then lagged
    behind on `computedAt`. A single serializer makes a third divergence
    impossible, and lets a client cache the POST result and age it without a
    follow-up GET.
    """

    calories = serializers.IntegerField()
    macros = MacrosSerializer()
    macroEnergyKcal = MacroEnergyKcalSerializer()
    rationale = PlanRationaleSerializer()
    isEstimate = serializers.BooleanField()
    bmr = serializers.FloatField()
    tdee = serializers.FloatField()
    computedAt = serializers.DateTimeField()
    advisories = AdvisorySerializer(many=True)


class CompleteResponseSerializer(serializers.Serializer):
    profile = ProfileSerializer()


class EngineConfigMacrosSerializer(serializers.Serializer):
    proteinPerKg = serializers.FloatField()
    fatPct = serializers.FloatField()
    fiberGPer1000Kcal = serializers.FloatField()


class AgeBoundsSerializer(serializers.Serializer):
    """Whole years. Below `min` the API raises `age_below_minimum` with its own
    error code rather than a field error, so the client can route to the
    dedicated screen (§9)."""

    min = serializers.IntegerField()
    max = serializers.IntegerField()


class MeasurementBoundsSerializer(serializers.Serializer):
    """`min`/`max` reject; `softMin`/`softMax` only trigger an advisory."""

    min = serializers.FloatField()
    max = serializers.FloatField()
    softMin = serializers.FloatField()
    softMax = serializers.FloatField()


class ValidationBoundsSerializer(serializers.Serializer):
    """See `validation_bounds`. Metric, matching the wire format."""

    age = AgeBoundsSerializer()
    weightKg = MeasurementBoundsSerializer()
    heightCm = MeasurementBoundsSerializer()


class EngineConfigResponseSerializer(serializers.Serializer):
    """GET /onboarding/config. See `EngineConfig.to_public_dict`.

    The three keyed-by-enum dicts (goal, activity level, sex) vary by config
    row, not by request, so they're typed as records rather than fixed fields.
    """

    configName = serializers.CharField()
    goalAdjustmentsKcal = serializers.DictField(child=serializers.IntegerField())
    activityMultipliers = serializers.DictField(child=serializers.FloatField())
    macros = EngineConfigMacrosSerializer()
    safetyFloorsKcal = serializers.DictField(child=serializers.IntegerField())
    targetRoundingKcal = serializers.IntegerField()
    kcalPerKgBodyMass = serializers.FloatField()
    validation = ValidationBoundsSerializer()


def serialize_stored_plan(plan: Any) -> Dict[str, Any]:
    """Render a persisted Plan row in the shape the calculator returns, so GET
    /onboarding/plan matches POST field for field apart from the extra
    `computedAt`. Keep `rationale` in step with `PlanResult.to_response`."""
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
            # Null on rows written before these columns existed; `fetch_plan`
            # fills them in rather than handing the client a missing key.
            "safetyFloorKcal": plan.safetyFloorKcal,
            "requestedAdjustmentKcal": plan.requestedAdjustmentKcal,
        },
        "isEstimate": plan.isEstimate,
        "bmr": plan.bmr,
        "tdee": plan.tdee,
        "computedAt": plan.computedAt.isoformat(),
    }
