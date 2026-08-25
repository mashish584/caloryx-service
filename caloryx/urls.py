from __future__ import annotations

from django.urls import include, path
from rest_framework.permissions import AllowAny
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from common.views import health, readiness

# The schema describes shapes, not secrets, so it and its docs UIs bypass the
# default bearer-auth requirement - CI needs to fetch it without a token.
_open = {"authentication_classes": [], "permission_classes": [AllowAny]}

urlpatterns = [
    path("healthz", health, name="health"),
    path("readyz", readiness, name="readiness"),
    path("api/v1/auth/", include("authx.urls")),
    path("api/v1/onboarding/", include("onboarding.urls")),
    # Schema is the source of truth for FE type generation (see
    # scripts/export_openapi_schema.sh); docs UIs are for humans only.
    path("api/schema", SpectacularAPIView.as_view(**_open), name="schema"),
    path(
        "api/schema/docs",
        SpectacularSwaggerView.as_view(url_name="schema", **_open),
        name="swagger-ui",
    ),
    path(
        "api/schema/redoc",
        SpectacularRedocView.as_view(url_name="schema", **_open),
        name="redoc",
    ),
]
