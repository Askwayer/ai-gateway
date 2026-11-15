"""Shared, sanitized HTTP error contract."""

from typing import Any


class GatewayError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        source: str = "gateway",
        extra: dict[str, Any] | None = None,
        retry_after: str | None = None,
    ):
        self.status, self.code, self.source = status, code, source
        self.extra = extra or {}
        self.retry_after = retry_after
