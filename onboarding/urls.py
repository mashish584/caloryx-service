from __future__ import annotations

from django.urls import path

from .views import (
    AdvisoryCheckView,
    CompleteView,
    EngineConfigView,
    PlanView,
    ProfileView,
)

urlpatterns = [
    path("profile", ProfileView.as_view(), name="onboarding-profile"),
    path("advisories", AdvisoryCheckView.as_view(), name="onboarding-advisories"),
    path("plan", PlanView.as_view(), name="onboarding-plan"),
    path("complete", CompleteView.as_view(), name="onboarding-complete"),
    path("config", EngineConfigView.as_view(), name="onboarding-config"),
]
