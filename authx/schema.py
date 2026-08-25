"""drf-spectacular extension so `BearerAuthentication` resolves to the
`bearerAuth` security scheme declared in `SPECTACULAR_SETTINGS` instead of
being silently dropped from the generated schema."""
from __future__ import annotations

from drf_spectacular.extensions import OpenApiAuthenticationExtension


class BearerAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "authx.authentication.BearerAuthentication"
    name = "bearerAuth"

    def get_security_definition(self, auto_schema):
        return {"type": "http", "scheme": "bearer"}
