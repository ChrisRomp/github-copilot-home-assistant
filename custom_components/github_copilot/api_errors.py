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
_SCHEMA_CODES = {
    "invalid_json_schema",
    "unsupported_response_format",
    "invalid_response_format",
    "schema_validation_error",
}
_SCHEMA_MESSAGE = re.compile(
    r"\b(?:response[_ -]?format|json[_ -]?schema|structured (?:output|response)"
    r"|output_config\.format\.schema)\b",
    re.IGNORECASE,
)
_SCHEMA_KEYWORD = (
    r"(?:additionalProperties|uniqueItems|minItems|maxItems|minimum|maximum|"
    r"exclusiveMinimum|exclusiveMaximum|multipleOf|minLength|maxLength|pattern|format)"
)
_UNSUPPORTED_SCHEMA_KEYWORDS = re.compile(
    rf"(?<!\w)(?P<keywords>['\"]?{_SCHEMA_KEYWORD}['\"]?"
    rf"(?:\s*,\s*['\"]?{_SCHEMA_KEYWORD}['\"]?)*)\s+"
    r"(?:values\b[^.\n]{0,80}\s+)?(?:is|are)\s+not\s+"
    r"(?:supported|permitted|allowed)\b"
)
_AUTH_CODES = {
    "401",
    "bad_credentials",
    "invalid_token",
    "expired_token",
    "token_expired",
    "token_revoked",
    "authentication_required",
    "unauthenticated",
    "unauthorized",
}
_AUTH_MESSAGE = re.compile(
    r"\b(?:HTTP(?:/\d(?:\.\d)?)?\s+|status(?:\s+code)?\s*[:=]?\s*)401\b"
    r"|\b401\s*:?\s*unauthorized\b"
    r"|\bauthentication failed\s*\(401\)"
    r"|\bbad credentials\b"
    r"|\b(?:access\s+|oauth\s+|github\s+)?token\s+(?:has\s+)?(?:been\s+|is\s+)?"
    r"(?:expired|revoked|invalid)\b"
    r"|\b(?:expired|revoked|invalid)\s+(?:(?:access|oauth|github)\s+)?token\b"
    r"|\bnot authenticated\b",
    re.IGNORECASE,
)
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
        elif isinstance(value, (str, int)) and key in (
            "code",
            "errorCode",
            "errorType",
            "providerErrorType",
            "type",
            "message",
            "status",
            "statusCode",
            "status_code",
        ):
            yield key, str(value)


def _error_details(
    error: Exception, event: SessionErrorData | None
) -> Iterator[tuple[str, str]]:
    """Read only error metadata from events and exception chains."""
    if event is not None:
        yield from _details(event.to_dict())
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        data = getattr(current, "data", None)
        if isinstance(data, Mapping):
            yield from _details(data)
        yield from _details(
            {
                "code": getattr(current, "code", None),
                "message": str(current),
                "statusCode": getattr(current, "status_code", None),
                "status": getattr(current, "status", None),
            }
        )
        current = current.__cause__


def _matches(
    error: Exception,
    event: SessionErrorData | None,
    codes: set[str],
    message_pattern: re.Pattern[str],
) -> bool:
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
        for key, value in _error_details(error, event)
    )


def is_quota_error(error: Exception, event: SessionErrorData | None = None) -> bool:
    """Identify exhausted quota, not a bare HTTP 429 or transient rate limit."""
    return _matches(error, event, _QUOTA_CODES, _QUOTA_MESSAGE)


def is_model_unavailable_error(
    error: Exception, event: SessionErrorData | None = None
) -> bool:
    """Identify explicit model rejection even if discovery is still stale."""
    return _matches(error, event, _MODEL_CODES, _MODEL_MESSAGE)


def is_output_schema_error(
    error: Exception, event: SessionErrorData | None = None
) -> bool:
    """Keep request-format failures out of model availability recovery."""
    return _matches(error, event, _SCHEMA_CODES, _SCHEMA_MESSAGE)


def unsupported_schema_keywords(
    error: Exception, event: SessionErrorData | None = None
) -> tuple[str, ...]:
    """Return only allowlisted keywords explicitly described as unsupported."""
    return tuple(
        sorted(
            {
                keyword
                for key, value in _error_details(error, event)
                if key == "message"
                for match in _UNSUPPORTED_SCHEMA_KEYWORDS.finditer(value)
                for keyword in re.findall(_SCHEMA_KEYWORD, match["keywords"])
            }
        )
    )


def is_authentication_error(
    error: Exception, event: SessionErrorData | None = None
) -> bool:
    """Identify rejected credentials, not rate limits or model access policies."""
    return (
        not is_quota_error(error, event)
        and not is_model_unavailable_error(error, event)
        and _matches(error, event, _AUTH_CODES, _AUTH_MESSAGE)
    )
