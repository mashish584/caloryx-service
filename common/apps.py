from __future__ import annotations

from django.apps import AppConfig


class CommonConfig(AppConfig):
    name = "common"
    verbose_name = "CaloryX common"

    def ready(self) -> None:
        # Registers the deployment checks (secret key, Clerk, database URL).
        from . import checks  # noqa: F401
