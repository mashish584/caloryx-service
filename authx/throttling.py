from __future__ import annotations

from rest_framework.throttling import AnonRateThrottle


class GuestCreationThrottle(AnonRateThrottle):
    """Guest session creation is unauthenticated and writes a row, so it is the
    one endpoint that needs a default limit. Applied per view, not globally."""

    scope = "guest_create"

    def allow_request(self, request, view):
        if getattr(view, "throttle_scope_guest_create", False):
            return super().allow_request(request, view)
        return True
