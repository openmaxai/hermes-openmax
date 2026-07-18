"""SDK exception hierarchy."""

from __future__ import annotations

from typing import Any, Optional


class CwsError(Exception):
    """Base class for all SDK errors."""


class CwsAuthError(CwsError):
    """Authentication/authorization failure (bad api key, expired token)."""

    def __init__(self, message: str, *, status: Optional[int] = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class CwsApiError(CwsError):
    """Non-2xx REST response."""

    def __init__(self, message: str, *, status: int, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class CwsWsFatal(CwsError):
    """WebSocket closed with a non-recoverable close code — do not reconnect."""

    def __init__(self, code: int, reason: str = ""):
        super().__init__(f"WS fatal close {code}: {reason}")
        self.code = code
        self.reason = reason
