"""Application error type shared by engines, routes, and the global handler."""

from __future__ import annotations


class AppError(Exception):
    """An error with a stable machine-readable code and HTTP status."""

    def __init__(self, code: str, http_status: int, message: str):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.message = message


# Error codes and their HTTP statuses (documented contract).
INVALID_URL = ("invalid_url", 400)
UNSUPPORTED_FORMAT = ("unsupported_format", 400)
UNAUTHORIZED = ("unauthorized", 401)
VIDEO_UNAVAILABLE = ("video_unavailable", 404)
NO_SUCH_LANGUAGE = ("no_such_language", 404)
NO_CAPTIONS = ("no_captions", 422)
RATE_LIMITED = ("rate_limited", 429)
UPSTREAM_ERROR = ("upstream_error", 502)
