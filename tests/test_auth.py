"""Tests for GitHub credential handling."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.github_copilot.auth import (
    OAuthError,
    poll_for_device_token,
    request_device_code,
    validate_pat,
)
from custom_components.github_copilot.const import (
    AUTH_METHOD_OAUTH,
    AUTH_METHOD_PAT,
    CONF_ACCESS_TOKEN,
    CONF_ACCESS_TOKEN_EXPIRES_AT,
    CONF_AUTH_METHOD,
    CONF_REFRESH_TOKEN,
    OAUTH_CLIENT_ID,
    OAUTH_SCOPE,
)


def test_oauth_app_is_configured_for_expiring_tokens() -> None:
    assert OAUTH_CLIENT_ID.startswith("Ov23")
    assert "offline_access" in OAUTH_SCOPE.split()


def test_validate_pat_rejects_classic_token() -> None:
    with pytest.raises(ValueError, match="Classic"):
        validate_pat("ghp_example")


def test_validate_pat_trims_input() -> None:
    assert validate_pat("  github_pat_example  ") == "github_pat_example"


def test_validate_pat_rejects_empty_value() -> None:
    with pytest.raises(ValueError, match="Enter"):
        validate_pat("  ")


class _Response:
    def __init__(self, payload: dict[str, str]) -> None:
        self.payload = payload

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def json(self) -> dict[str, str]:
        return self.payload


class _Session:
    def __init__(self, *payloads: dict[str, str]) -> None:
        self.payloads = list(payloads)

    def post(self, *_: object, **__: object) -> _Response:
        return _Response(self.payloads.pop(0))


@pytest.mark.asyncio
async def test_device_code_request_validates_and_normalizes_response() -> None:
    payload = await request_device_code(
        _Session(
            {
                "device_code": "opaque-code",
                "user_code": "ABCD-1234",
                "verification_uri": "https://github.com/login/device",
                "expires_in": "900",
            }
        ),
        "public-client-id",
    )

    assert payload["expires_in"] == 900
    assert payload["interval"] == 5


@pytest.mark.asyncio
async def test_device_code_request_rejects_untrusted_verification_url() -> None:
    with pytest.raises(OAuthError, match="unexpected"):
        await request_device_code(
            _Session(
                {
                    "device_code": "opaque-code",
                    "user_code": "ABCD-1234",
                    "verification_uri": "https://github.com.attacker.invalid/device",
                    "expires_in": "900",
                }
            ),
            "public-client-id",
        )


@pytest.mark.asyncio
async def test_device_flow_honors_slow_down_and_returns_tokens() -> None:
    sleeps: list[float] = []
    ticks = iter([0.0, 0.0, 5.0, 5.0])

    async def sleep(duration: float) -> None:
        sleeps.append(duration)

    payload = await poll_for_device_token(
        _Session(
            {"error": "authorization_pending"},
            {"error": "slow_down", "interval": "11"},
            {
                "access_token": "gho_access",
                "refresh_token": "ghr_refresh",
                "expires_in": "28800",
            },
        ),
        client_id="public-client-id",
        device_code="opaque-device-code",
        expires_in=60,
        interval=5,
        sleep=sleep,
        monotonic=lambda: next(ticks, 6.0),
    )

    assert payload["access_token"] == "gho_access"
    assert payload["refresh_token"] == "ghr_refresh"
    assert len(sleeps) == 3
    assert sleeps[0] == 5
    assert sleeps[1] >= 5


@pytest.mark.asyncio
async def test_device_flow_rejects_access_denied() -> None:
    with pytest.raises(OAuthError, match="denied"):
        await poll_for_device_token(
            _Session({"error": "access_denied"}),
            client_id="public-client-id",
            device_code="opaque-device-code",
            expires_in=60,
            interval=1,
            sleep=_immediate_sleep,
            monotonic=_clock(),
        )


@pytest.mark.asyncio
async def test_device_flow_rejects_non_expiring_oauth_token() -> None:
    with pytest.raises(OAuthError, match="incomplete"):
        await poll_for_device_token(
            _Session({"access_token": "gho_access"}),
            client_id="public-client-id",
            device_code="opaque-device-code",
            expires_in=60,
            interval=1,
            sleep=_immediate_sleep,
            monotonic=_clock(),
        )


async def _immediate_sleep(_: float) -> None:
    return None


def _clock():
    current = 0.0

    def now() -> float:
        nonlocal current
        current += 1
        return current

    return now


@pytest.fixture
def credentials(ha_boundary, monkeypatch):
    module = ha_boundary.load("auth")
    monkeypatch.setattr(module.time, "time", lambda: 10000)
    entry = SimpleNamespace(
        entry_id="entry",
        data={
            CONF_AUTH_METHOD: AUTH_METHOD_OAUTH,
            CONF_ACCESS_TOKEN: "old-access-token",
            CONF_ACCESS_TOKEN_EXPIRES_AT: 10000 + 3600,
            CONF_REFRESH_TOKEN: "old-refresh-token",
        },
    )
    update = Mock(side_effect=lambda entry, *, data: setattr(entry, "data", data))
    hass = SimpleNamespace(
        data={}, config_entries=SimpleNamespace(async_update_entry=update)
    )
    rotated = {
        CONF_AUTH_METHOD: AUTH_METHOD_OAUTH,
        CONF_ACCESS_TOKEN: "new-access-token",
        CONF_ACCESS_TOKEN_EXPIRES_AT: 10000 + 28800,
        CONF_REFRESH_TOKEN: "new-refresh-token",
    }

    async def refresh(*args, **kwargs):
        await asyncio.sleep(0)
        return rotated

    refresh_mock = AsyncMock(side_effect=refresh)
    monkeypatch.setattr(module, "refresh_oauth_token", refresh_mock)
    store = module.CredentialStore(hass, entry, OAUTH_CLIENT_ID)
    return SimpleNamespace(
        store=store, entry=entry, refresh=refresh_mock, update=update
    )


@pytest.mark.parametrize("seconds_remaining", [None, -1, 60, 3600, 3660])
async def test_oauth_refreshes_before_sdk_preflight_and_persists_rotated_pair(
    credentials, seconds_remaining
):
    if seconds_remaining is None:
        credentials.entry.data.pop(CONF_ACCESS_TOKEN_EXPIRES_AT)
    else:
        credentials.entry.data[CONF_ACCESS_TOKEN_EXPIRES_AT] = 10000 + seconds_remaining
    tokens = await asyncio.gather(
        *(credentials.store.async_get_access_token() for _ in range(3))
    )
    assert tokens == ["new-access-token"] * 3
    credentials.refresh.assert_awaited_once()
    credentials.update.assert_called_once()
    assert credentials.entry.data[CONF_REFRESH_TOKEN] == "new-refresh-token"
    assert credentials.entry.data[CONF_ACCESS_TOKEN_EXPIRES_AT] == 10000 + 28800


async def test_oauth_with_enough_lifetime_is_not_refreshed(credentials):
    credentials.entry.data[CONF_ACCESS_TOKEN_EXPIRES_AT] = 10000 + 3661
    assert await credentials.store.async_get_access_token() == "old-access-token"
    credentials.refresh.assert_not_awaited()
    credentials.update.assert_not_called()


async def test_pat_is_not_refreshed_or_assigned_an_expiry(credentials):
    credentials.entry.data = {
        CONF_AUTH_METHOD: AUTH_METHOD_PAT,
        CONF_ACCESS_TOKEN: "test-pat",
    }
    assert await credentials.store.async_get_access_token() == "test-pat"
    credentials.refresh.assert_not_awaited()
    assert CONF_ACCESS_TOKEN_EXPIRES_AT not in credentials.entry.data


@pytest.mark.parametrize("payload", [{"id": 123}, {"id": "123"}, {"id": True}, {}, []])
async def test_account_identity_uses_immutable_numeric_github_id(payload):
    from custom_components.github_copilot.auth import async_get_account_id

    response = _Response(payload)
    response.status = 200
    session = SimpleNamespace(get=Mock(return_value=response))
    if payload == {"id": 123}:
        assert await async_get_account_id(session, "test-token") == "123"
    else:
        with pytest.raises(OAuthError, match="invalid account identity"):
            await async_get_account_id(session, "test-token")
    assert session.get.call_args.args == ("https://api.github.com/user",)
