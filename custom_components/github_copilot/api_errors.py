"""Recognize actionable Copilot API errors without exposing their payloads."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any

from copilot.session_events import SessionErrorData

_QUOTA_CODES = {
    "quota",
    "quota_exceeded",
    "quota_exhausted",
    "insufficient_quota",
    "usage_limit_exceeded",
    "billing_hard_limit_reached",
    "insufficient_credits",
    "credits_exhausted",
}
_MODEL_CODES = {
    "model_not_found",
    "model_not_supported",
    "model_not_available",
    "model_unavailable",
    "model_disabled",
}
_QUOTA_MESSAGE = re.compile(
    r"\bquota\b.{0,80}\b(?:exceeded|exhausted|depleted)\b"
    r"|\b(?:exceeded|exhausted|insufficient)\b.{0,80}\bquota\b"
    r"|\b(?:out of|insufficient|no remaining)\s+(?:ai\s+)?credits\b"
    r"|\b(?:ai\s+)?credits\b.{0,80}\b(?:exhausted|depleted|exceeded|insufficient)\b",
    re.IGNORECASE,
)
_MODEL_MESSAGE = re.compile(
    r"\bmodel\b.{0,100}\b(?:not found|not supported|not available|"
    r"no longer available|no longer supported|does not exist|disabled)\b",
    re.IGNORECASE,
)


def _details(payload: Mapping[str, Any]) -> Iterator[tuple[str, str]]:
    """Read error fields from SDK events and nested JSON-RPC envelopes."""
    for key, value in payload.items():
        if key in ("error", "data") and isinstance(value, Mapping):
            yield from _details(value)
        elif isinstance(value, str) and key in (
            "code",
            "errorCode",
            "errorType",
            "providerErrorType",
            "type",
            "message",
        ):
            yield key, value


def _matches(
    error: Exception,
    event: SessionErrorData | None,
    codes: set[str],
    message_pattern: re.Pattern[str],
) -> bool:
    payloads: list[Mapping[str, Any]] = []
    if event is not None:
        payloads.append(event.to_dict())
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        data = getattr(current, "data", None)
        if isinstance(data, Mapping):
            payloads.append(data)
        payloads.append(
            {"code": getattr(current, "code", None), "message": str(current)}
        )
        current = current.__cause__
    return any(
        (
            bool(message_pattern.search(value))
            or any(
                "_" in code and re.search(rf"\b{re.escape(code)}\b", value.lower())
                for code in codes
            )
        )
        if key == "message"
        else value.lower().replace("-", "_") in codes
        for payload in payloads
        for key, value in _details(payload)
    )


def is_quota_error(error: Exception, event: SessionErrorData | None = None) -> bool:
    """Identify exhausted quota, not a bare HTTP 429 or transient rate limit."""
    return _matches(error, event, _QUOTA_CODES, _QUOTA_MESSAGE)


def is_model_unavailable_error(
    error: Exception, event: SessionErrorData | None = None
) -> bool:
    """Identify explicit model rejection even if discovery is still stale."""
    return _matches(error, event, _MODEL_CODES, _MODEL_MESSAGE)
