"""Everything except the collection root - see caloryx/urls.py for why
`meals-list-create` is registered there instead of here."""
from __future__ import annotations

from django.urls import path

from .views import FoodSearchView, LoggedMealDetailView, LoggedMealItemView

urlpatterns = [
    path("foods", FoodSearchView.as_view(), name="meals-food-search"),
    path("<str:meal_id>", LoggedMealDetailView.as_view(), name="meals-detail"),
    path("<str:meal_id>/items/<str:item_id>", LoggedMealItemView.as_view(), name="meals-item"),
]
