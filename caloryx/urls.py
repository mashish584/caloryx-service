from __future__ import annotations

from django.urls import include, path
from rest_framework.permissions import AllowAny
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from assistant.views import DraftCreateView, MessageView
from common.views import health, readiness
from meals.views import LoggedMealListCreateView

# The schema describes shapes, not secrets, so it and its docs UIs bypass the
# default bearer-auth requirement - CI needs to fetch it without a token.
_open = {"authentication_classes": [], "permission_classes": [AllowAny]}

urlpatterns = [
    path("healthz", health, name="health"),
    path("readyz", readiness, name="readiness"),
    path("api/v1/auth/", include("authx.urls")),
    path("api/v1/onboarding/", include("onboarding.urls")),
    # Registered without a trailing slash, unlike every other endpoint in this
    # file: it's the collection root ("/api/v1/meals"), and `meals.urls`'s own
    # patterns (which all have a named final segment, e.g. "/foods") are
    # included under the slash-suffixed prefix below. Folding this into that
    # include as `path("", ...)` would make it reachable only via
    # "/api/v1/meals/" and hit Django's APPEND_SLASH redirect on POST, which
    # most HTTP clients turn into a GET.
    path("api/v1/meals", LoggedMealListCreateView.as_view(), name="meals-list-create"),
    path("api/v1/meals/", include("meals.urls")),
    # Same trailing-slash reasoning as meals-list-create above.
    path("api/v1/assistant/drafts", DraftCreateView.as_view(), name="assistant-drafts-create"),
    path("api/v1/assistant/drafts/", include("assistant.urls")),
    # No sibling sub-path under /messages/ (Chunk 2b), so this one needs no
    # trailing-slash split the way /drafts and /meals do.
    path("api/v1/assistant/messages", MessageView.as_view(), name="assistant-messages"),
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
