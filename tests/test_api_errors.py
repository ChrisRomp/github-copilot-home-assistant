"""Actionable API failure classification, using the installed SDK's types."""

import pytest
from copilot._jsonrpc import JsonRpcError
from copilot.session_events import SessionErrorData

from custom_components.github_copilot.api_errors import (
    is_authentication_error,
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


@pytest.mark.parametrize(
    "message",
    [
        "HTTP 401",
        "HTTP/1.1 401 Unauthorized",
        "Model discovery failed: status code: 401",
        "Request failed: 401 Unauthorized",
        "Authentication failed (401)",
        "Bad credentials",
        "The access token has expired",
        "Token has been revoked",
        "Invalid GitHub token",
        "Not authenticated",
        "Request failed: token_expired",
    ],
)
def test_confirmed_authentication_messages(message):
    assert is_authentication_error(RuntimeError(message))


@pytest.mark.parametrize(
    "payload",
    [
        {"status": 401},
        {"statusCode": "401"},
        {"status_code": 401},
        {"error": {"code": "bad_credentials"}},
        {"data": {"error": {"errorCode": "invalid_token"}}},
    ],
)
def test_structured_authentication_errors_and_wrapped_causes(payload):
    error = JsonRpcError(-32603, "Request rejected", data=payload)
    wrapped = RuntimeError("Discovery failed")
    wrapped.__cause__ = error
    assert is_authentication_error(wrapped)


def test_http_authentication_status_and_sdk_session_event():
    error = RuntimeError("Request rejected")
    error.status = 401
    assert is_authentication_error(error)
    assert is_authentication_error(
        RuntimeError("Request rejected"),
        SessionErrorData(
            error_type="provider_error", message="Rejected", status_code=401
        ),
    )


@pytest.mark.parametrize(
    "message",
    [
        "HTTP 403 Forbidden",
        "HTTP 429 Too many requests",
        "Rate limit exceeded",
        "Model access denied by organization policy",
        "Model not available",
        "Could not contact authentication server",
        "Token refresh request failed",
        "Authentication or model discovery failed",
        "Quota exceeded",
        "Insufficient AI Credits",
    ],
)
def test_non_authentication_messages_do_not_require_reauth(message):
    assert not is_authentication_error(RuntimeError(message))


@pytest.mark.parametrize(
    "payload",
    [
        {"statusCode": 403, "code": "forbidden"},
        {"statusCode": 429, "code": "rate_limit_exceeded"},
        {"statusCode": 401, "code": "quota_exceeded"},
        {"statusCode": 401, "code": "model_disabled"},
    ],
)
def test_policy_quota_and_rate_errors_do_not_require_reauth(payload):
    assert not is_authentication_error(
        JsonRpcError(-32603, "Request rejected", data=payload)
    )
