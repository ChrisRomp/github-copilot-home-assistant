"""Actionable API failure classification, using the installed SDK's types."""

import pytest
from copilot._jsonrpc import JsonRpcError
from copilot.session_events import SessionErrorData

from custom_components.github_copilot.api_errors import (
    is_model_unavailable_error,
    is_quota_error,
)


@pytest.mark.parametrize(
    "message",
    [
        "Quota exceeded",
        "You have exceeded your Copilot quota",
        "Your AI Credits are exhausted",
        "Insufficient AI Credits",
        "Session error: usage quota has been depleted",
        "Session error: quota_exceeded",
        "Your AI Credits limit has been exceeded",
    ],
)
def test_quota_messages(message):
    assert is_quota_error(Exception(message))


def test_structured_quota_errors():
    event = SessionErrorData(
        error_type="provider_error",
        error_code="insufficient_quota",
        message="Request rejected",
        status_code=429,
    )
    assert is_quota_error(RuntimeError("Request failed"), event)
    error = JsonRpcError(
        -32603,
        "Request rejected",
        data={"error": {"code": "quota_exceeded"}},
    )
    assert is_quota_error(error)
    wrapped = RuntimeError("Model discovery failed")
    wrapped.__cause__ = error
    assert is_quota_error(wrapped)
    assert is_quota_error(
        RuntimeError("Request failed"),
        SessionErrorData(error_type="quota", message="Request blocked"),
    )


@pytest.mark.parametrize(
    "message",
    [
        "HTTP 429: Too many requests",
        "Rate limit exceeded",
        "Request timed out",
        "Authentication failed (401)",
        "Upstream server error (500)",
        "Unable to retrieve quota",
    ],
)
def test_other_errors_are_not_exhausted_quota(message):
    assert not is_quota_error(RuntimeError(message))


def test_model_errors_are_not_quota_errors():
    error = JsonRpcError(-32603, "Model 'claude-sonnet-4.5' is no longer available")
    assert is_model_unavailable_error(error)
    assert not is_quota_error(error)
    event = SessionErrorData(
        error_type="provider_error", error_code="model_not_found", message="Rejected"
    )
    assert is_model_unavailable_error(RuntimeError("Request failed"), event)
    assert not is_model_unavailable_error(
        RuntimeError("Model service connection failed")
    )
