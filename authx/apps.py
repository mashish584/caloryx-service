from __future__ import annotations

from django.apps import AppConfig


class AuthxConfig(AppConfig):
    name = "authx"
    verbose_name = "CaloryX identity"

    def ready(self) -> None:
        from . import schema  # noqa: F401 - registers the drf-spectacular auth extension
