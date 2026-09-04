"""Everything except the collection root - see caloryx/urls.py for why
`assistant-drafts-create` is registered there instead of here (same reasoning
as meals.urls for `meals-list-create`)."""
from __future__ import annotations

from django.urls import path

from .views import DraftConfirmView, DraftDetailView, DraftItemCreateView, DraftItemDetailView

urlpatterns = [
    path("<str:draft_id>", DraftDetailView.as_view(), name="assistant-drafts-detail"),
    path("<str:draft_id>/items", DraftItemCreateView.as_view(), name="assistant-draft-items-create"),
    path(
        "<str:draft_id>/items/<str:item_id>",
        DraftItemDetailView.as_view(),
        name="assistant-draft-items-detail",
    ),
    path("<str:draft_id>/confirm", DraftConfirmView.as_view(), name="assistant-drafts-confirm"),
]
