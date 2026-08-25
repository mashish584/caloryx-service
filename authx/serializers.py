from __future__ import annotations

from typing import Any, Dict, Optional

from rest_framework import serializers

from engine.enums import AuthProvider

from .actor import Actor


class ClaimGuestRequestSerializer(serializers.Serializer):
    guestToken = serializers.CharField(
        help_text="The guest bearer token whose onboarding data should move to this account."
    )


class UserSerializer(serializers.Serializer):
    """Mirrors `serialize_user` - the persisted User row."""

    id = serializers.CharField()
    isGuest = serializers.BooleanField()
    authProvider = serializers.ChoiceField(choices=[p.value for p in AuthProvider])
    externalProvider = serializers.CharField(allow_null=True, required=False)
    email = serializers.CharField(allow_null=True)
    claimedAt = serializers.DateTimeField(allow_null=True)
    createdAt = serializers.DateTimeField()


class ActorSerializer(serializers.Serializer):
    """Mirrors `serialize_actor` - the authenticated request identity, which is
    cheaper than `UserSerializer` because it never re-reads the DB row."""

    id = serializers.CharField()
    isGuest = serializers.BooleanField()
    authProvider = serializers.ChoiceField(choices=[p.value for p in AuthProvider])
    email = serializers.CharField(allow_null=True)


class OnboardingStateSerializer(serializers.Serializer):
    hasProfile = serializers.BooleanField()
    hasPlan = serializers.BooleanField()
    onboardedAt = serializers.DateTimeField(allow_null=True)


class GuestSessionResponseSerializer(serializers.Serializer):
    token = serializers.CharField()
    expiresAt = serializers.DateTimeField()
    user = UserSerializer()
    onboarding = OnboardingStateSerializer()


class SessionResponseSerializer(serializers.Serializer):
    user = ActorSerializer()
    onboarding = OnboardingStateSerializer()


class ClaimResponseSerializer(serializers.Serializer):
    user = UserSerializer()
    onboarding = OnboardingStateSerializer()


def serialize_user(user: Any) -> Dict[str, Any]:
    return {
        "id": user.id,
        "isGuest": user.isGuest,
        "authProvider": user.authProvider,
        "externalProvider": getattr(user, "externalProvider", None),
        "email": user.email,
        "claimedAt": user.claimedAt.isoformat() if user.claimedAt else None,
        "createdAt": user.createdAt.isoformat(),
    }


def serialize_actor(actor: Actor) -> Dict[str, Any]:
    return {
        "id": actor.user_id,
        "isGuest": actor.is_guest,
        "authProvider": actor.provider.value,
        "email": actor.email,
    }


def serialize_onboarding_state(profile: Optional[Any]) -> Dict[str, Any]:
    """What the app needs to resume the flow at the right step (PRD §4)."""
    if profile is None:
        return {"hasProfile": False, "hasPlan": False, "onboardedAt": None}
    plan = getattr(profile, "plan", None)
    return {
        "hasProfile": True,
        "hasPlan": plan is not None,
        "onboardedAt": profile.onboardedAt.isoformat() if profile.onboardedAt else None,
    }
