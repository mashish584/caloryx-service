"""Bump the global `CatalogVersion` singleton (§12.3, §12.7, Chunk 8a).

There is no admin UI or automated change-detection for the food catalog -
Food/CompositeFood/DishCategoryProfile are all edited by hand (seed commands
or direct DB access). Run this after any such edit so `LoggedMeal`'s stored
`catalogVersion` and the T0/L2 parse caches (§12.7) correctly treat the
catalog as having changed.

    python manage.py bump_catalog_version
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from common.db import get_client


class Command(BaseCommand):
    help = "Increment the global CatalogVersion singleton by one."

    def handle(self, *args, **options) -> None:
        client = get_client()
        row = client.catalogversion.find_unique(where={"id": "global"})
        if row is None:
            row = client.catalogversion.create(data={"id": "global", "version": 1})
        else:
            row = client.catalogversion.update(
                where={"id": "global"}, data={"version": {"increment": 1}}
            )
        self.stdout.write(self.style.SUCCESS("catalog version is now {}".format(row.version)))
