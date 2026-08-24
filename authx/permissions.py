from __future__ import annotations

from rest_framework import permissions

from .actor import Actor


class IsAuthenticatedActor(permissions.BasePermission):
    """Any session - guest, Google, or Apple - counts as authenticated (PRD §8)."""

    message = "A guest or signed-in session is required."

    def has_permission(self, request, view) -> bool:
        return isinstance(getattr(request, "user", None), Actor)


class IsRegisteredActor(permissions.BasePermission):
    """For endpoints a guest genuinely cannot use (e.g. claiming a guest profile)."""

    message = "Sign in to continue."

    def has_permission(self, request, view) -> bool:
        actor = getattr(request, "user", None)
        return isinstance(actor, Actor) and not actor.is_guest
