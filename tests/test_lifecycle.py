"""Regressions for credential updates versus integration reloads."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.github_copilot import _async_update_listener
from custom_components.github_copilot.auth import (
    CredentialStore,
    async_start_reauth_flow,
)
from custom_components.github_copilot.const import DOMAIN, OAUTH_CLIENT_ID
from custom_components.github_copilot.runtime import remove_entry_directory


async def test_refresh_does_not_reload_or_stop_active_runtime(ha_boundary, monkeypatch):
    from custom_components.github_copilot import auth

    entry = SimpleNamespace(
        entry_id="entry",
        options={"model": "model"},
        data={
            "auth_method": "oauth",
            "access_token": "old",
            "access_token_expires_at": 0,
            "refresh_token": "refresh",
        },
    )
    runtime = SimpleNamespace(options=dict(entry.options), async_stop=AsyncMock())
    callbacks = []

    def update(entry, *, data):
        entry.data = data
        callbacks.append(asyncio.create_task(_async_update_listener(hass, entry)))

    hass = SimpleNamespace(
        data={DOMAIN: {"entry": runtime}},
        config_entries=SimpleNamespace(
            async_update_entry=update, async_reload=AsyncMock()
        ),
    )
    monkeypatch.setattr(
        auth,
        "refresh_oauth_token",
        AsyncMock(
            return_value={
                "access_token": "new",
                "access_token_expires_at": 10**12,
                "refresh_token": "rotated",
            }
        ),
    )
    first = CredentialStore(hass, entry, OAUTH_CLIENT_ID)
    second = CredentialStore(hass, entry, OAUTH_CLIENT_ID)
    assert first._refresh_lock is second._refresh_lock
    assert await asyncio.gather(
        first.async_get_access_token(), second.async_get_access_token()
    ) == ["new", "new"]
    await asyncio.gather(*callbacks)
    auth.refresh_oauth_token.assert_awaited_once()
    hass.config_entries.async_reload.assert_not_awaited()
    runtime.async_stop.assert_not_awaited()

    entry.options = {"model": "another"}
    await asyncio.gather(
        _async_update_listener(hass, entry), _async_update_listener(hass, entry)
    )
    hass.config_entries.async_reload.assert_awaited_once_with("entry")


async def test_reauth_uses_ha_deduplicating_entry_api():
    entry = SimpleNamespace(async_start_reauth=Mock())
    hass = SimpleNamespace()
    await async_start_reauth_flow(hass, entry)
    entry.async_start_reauth.assert_called_once_with(hass)


def test_entry_removal_deletes_only_owned_directory(tmp_path):
    hass = SimpleNamespace(
        config=SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts)))
    )
    root = tmp_path / ".storage" / DOMAIN
    for name in ("entry", "other", "runtime"):
        (root / name).mkdir(parents=True)
        (root / name / "state").write_text("data")
    remove_entry_directory(hass, "entry")
    assert not (root / "entry").exists()
    assert (root / "other" / "state").exists()
    assert (root / "runtime" / "state").exists()
    remove_entry_directory(hass, "entry")
    for invalid in ("runtime", "..", "../other", "", "/"):
        with pytest.raises(ValueError):
            remove_entry_directory(hass, invalid)


def test_removal_does_not_follow_entry_symlinks(tmp_path):
    hass = SimpleNamespace(
        config=SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts)))
    )
    root = tmp_path / ".storage" / DOMAIN
    root.mkdir(parents=True)
    target = tmp_path / "outside"
    target.mkdir()
    (target / "keep").write_text("keep")
    (root / "entry").symlink_to(target)
    remove_entry_directory(hass, "entry")
    assert (target / "keep").exists()
    assert not (root / "entry").is_symlink()
