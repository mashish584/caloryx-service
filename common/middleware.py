"""Request correlation id, attached to every log line and error response."""
from __future__ import annotations

import contextvars
import logging
import uuid
from typing import Callable

from django.http import HttpRequest, HttpResponse

REQUEST_ID_HEADER = "X-Request-Id"

# contextvars keeps the id correct under threaded and async workers alike.
_request_id: contextvars.ContextVar = contextvars.ContextVar("request_id", default="-")


def current_request_id() -> str:
    return _request_id.get()


class RequestIdLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = current_request_id()
        return True


class RequestIdMiddleware:
    """Reuses an inbound request id when the client sends one, so a single mobile
    trace can be followed end to end."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        request.request_id = request_id
        token = _request_id.set(request_id)
        try:
            response = self.get_response(request)
        finally:
            _request_id.reset(token)
        response[REQUEST_ID_HEADER] = request_id
        return response
