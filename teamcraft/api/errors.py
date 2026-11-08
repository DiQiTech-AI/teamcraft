"""Uniform API error model (M3-01).

Every failure the API surface can produce is rendered as the same JSON shape,
mirroring PLAN §8.5::

    {"code": "...", "message": "...", "details": ..., "request_id": "..."}

Unknown/internal failures never echo exception text: the message is fixed and
the real cause is only written to scrubbed server logs.
"""

from __future__ import annotations

from typing import Any

from .request_context import new_request_id

# Canonical error codes shared with clients and tests.
UNAUTHENTICATED = "UNAUTHENTICATED"
FORBIDDEN = "FORBIDDEN"
FORBIDDEN_ORIGIN = "FORBIDDEN_ORIGIN"
CSRF_CHECK_FAILED = "CSRF_CHECK_FAILED"
NOT_FOUND = "NOT_FOUND"
METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
PATH_FORBIDDEN = "PATH_FORBIDDEN"
VALIDATION_FAILED = "VALIDATION_FAILED"
CONFLICT = "CONFLICT"
SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
INTERNAL_ERROR = "INTERNAL_ERROR"
#: Added for M3-03: bounded-concurrency refusals on the SSE surface.
TOO_MANY_REQUESTS = "TOO_MANY_REQUESTS"

_STATUS_DEFAULT_CODES: dict[int, str] = {
    400: "BAD_REQUEST",
    401: UNAUTHENTICATED,
    403: FORBIDDEN,
    404: NOT_FOUND,
    405: METHOD_NOT_ALLOWED,
    409: CONFLICT,
    413: "PAYLOAD_TOO_LARGE",
    422: VALIDATION_FAILED,
    429: TOO_MANY_REQUESTS,
    503: SERVICE_UNAVAILABLE,
}


class ApiError(Exception):
    """An HTTP-visible failure with a stable machine-readable code."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def code_for_status(status_code: int) -> str:
    """Map an HTTP status to its default stable code."""

    return _STATUS_DEFAULT_CODES.get(status_code, INTERNAL_ERROR)


def error_payload(
    code: str,
    message: str,
    *,
    details: Any = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Build the uniform error body (keys are always present)."""

    return {
        "code": code,
        "message": message,
        "details": details,
        "request_id": request_id or new_request_id(),
    }


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    details: Any = None,
    request_id: str | None = None,
) -> Any:
    """Render the uniform error body as a JSON response with correlation id."""

    from fastapi.responses import JSONResponse

    payload = error_payload(
        code, message, details=details, request_id=request_id
    )
    response = JSONResponse(status_code=status_code, content=payload)
    response.headers["X-Request-Id"] = payload["request_id"]
    # This API is not a cross-origin resource: browsers must never be able to
    # read responses from other origins' pages.
    response.headers["Cache-Control"] = "no-store"
    return response
