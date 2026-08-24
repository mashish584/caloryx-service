from __future__ import annotations

from django.urls import include, path

from common.views import health, readiness

urlpatterns = [
    path("healthz", health, name="health"),
    path("readyz", readiness, name="readiness"),
    path("api/v1/auth/", include("authx.urls")),
    path("api/v1/onboarding/", include("onboarding.urls")),
]
