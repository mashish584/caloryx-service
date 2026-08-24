"""The authenticated caller.

Django's `User` model is unused here (Prisma owns persistence), so DRF's
`request.user` is one of these instead. It carries everything a view needs to
authorise a request without a second database round trip.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from engine.enums import AuthProvider


@dataclass(frozen=True)
class Actor:
    user_id: str
    provider: AuthProvider
    is_guest: bool
    clerk_user_id: Optional[str] = None
    email: Optional[str] = None

    # DRF/Django duck-typing.
    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def is_anonymous(self) -> bool:
        return False

    def __str__(self) -> str:  # pragma: no cover - logging convenience
        return "{}:{}".format(self.provider.value.lower(), self.user_id)
