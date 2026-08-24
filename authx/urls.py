from __future__ import annotations

from django.urls import path

from .views import ClaimGuestView, GuestSessionView, SessionView

urlpatterns = [
    path("guest", GuestSessionView.as_view(), name="auth-guest"),
    path("session", SessionView.as_view(), name="auth-session"),
    path("claim", ClaimGuestView.as_view(), name="auth-claim"),
]
