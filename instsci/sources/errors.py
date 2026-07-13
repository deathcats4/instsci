"""Shared error contract for scholarly metadata providers."""

from __future__ import annotations

from typing import Any


class ProviderSearchError(RuntimeError):
    """A provider request failed in a way callers should distinguish from zero hits."""

    def __init__(self, provider: str, status: str, detail: str = "") -> None:
        self.provider = provider
        self.status = status
        self.detail = detail
        super().__init__(detail or f"{provider} search failed: {status}")


def classify_provider_exception(exc: BaseException) -> str:
    """Map common HTTP/network failures to the public source-status vocabulary."""
    response: Any = getattr(exc, "response", None)
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code == 429 or "rate limit" in str(exc).lower() or "too many requests" in str(exc).lower():
        return "rate_limited"
    name = type(exc).__name__.lower()
    if "timeout" in name or "timed out" in str(exc).lower():
        return "timeout"
    if status_code or any(marker in name for marker in ("connection", "request", "ssl")):
        return "network_error"
    return "error"
