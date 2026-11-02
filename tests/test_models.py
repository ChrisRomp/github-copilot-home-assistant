"""Tests for Copilot model availability and same-family fallback."""

from __future__ import annotations

from enum import Enum
from types import SimpleNamespace

import pytest

from custom_components.github_copilot.models import (
    find_fallback_model,
    model_family,
    resolve_model,
    selectable_models,
)


class _PolicyState(Enum):
    DISABLED = "disabled"
    ENABLED = "enabled"


def _model(model_id: str, state: object | None = None) -> SimpleNamespace:
    policy = SimpleNamespace(state=state) if state is not None else None
    return SimpleNamespace(id=model_id, name=model_id, policy=policy)


@pytest.mark.parametrize(
    ("model_id", "family"),
    [
        ("claude-sonnet-4.5", "claude-sonnet"),
        ("claude-sonnet-5", "claude-sonnet"),
        ("gpt-6.1-sol", "gpt-sol"),
        ("gpt-5.6-sol-fast", "gpt-sol-fast"),
        ("gpt-5.3-codex", "gpt-codex"),
        ("gemini-3.8-flash", "gemini-flash"),
        ("grok-4.7", "grok"),
        ("gpt-4o", "gpt-4o"),
    ],
)
def test_model_family_strips_version_tokens(model_id: str, family: str) -> None:
    assert model_family(model_id) == family


def test_fallback_selects_closest_model_in_same_family() -> None:
    models = [
        _model("claude-sonnet-4.5"),
        _model("claude-sonnet-5.5"),
        _model("claude-sonnet-5"),
        _model("claude-opus-6"),
        _model("gpt-6.1-sol"),
    ]

    assert find_fallback_model("claude-sonnet-4", models) == "claude-sonnet-4.5"
    assert find_fallback_model("gpt-6-sol", models) == "gpt-6.1-sol"


def test_fallback_does_not_cross_families() -> None:
    models = [_model("gpt-5.6-sol-fast"), _model("gpt-6-luna")]

    assert find_fallback_model("gpt-6-sol", models) is None


def test_disabled_models_are_not_selectable_or_fallbacks() -> None:
    models = [
        _model("claude-sonnet-5.5", _PolicyState.DISABLED),
        _model("claude-sonnet-5", "enabled"),
        _model("gpt-6-sol", "disabled"),
        _model("gpt-6-luna"),
    ]

    assert [model.id for model in selectable_models(models)] == [
        "claude-sonnet-5",
        "gpt-6-luna",
    ]
    assert find_fallback_model("claude-sonnet-4.5", models) == "claude-sonnet-5"


def test_resolve_model_reports_available_fallback_and_missing() -> None:
    models = [_model("claude-sonnet-5"), _model("gpt-6-sol", "disabled")]

    available = resolve_model("claude-sonnet-5", models)
    assert available.is_available
    assert available.model_id == "claude-sonnet-5"

    fallback = resolve_model("claude-sonnet-4.5", models)
    assert fallback.is_fallback
    assert fallback.model_id == "claude-sonnet-5"

    missing = resolve_model("gpt-6-sol", models)
    assert not missing.is_available
    assert not missing.is_fallback
    assert missing.model_id is None


def test_fallback_never_exceeds_known_price_or_uses_unknown_price():
    cheap = _model("claude-sonnet-5")
    cheap.billing = SimpleNamespace(multiplier=1)
    expensive = _model("claude-sonnet-4.6")
    expensive.billing = SimpleNamespace(multiplier=3)
    unknown = _model("claude-sonnet-4.7")
    assert (
        find_fallback_model(
            "claude-sonnet-4.5", [cheap, expensive, unknown], max_multiplier=1
        )
        == cheap.id
    )
    assert (
        find_fallback_model("claude-sonnet-4.5", [expensive, unknown], max_multiplier=1)
        is None
    )
    resolution = resolve_model("claude-sonnet-4.5", [cheap], max_multiplier=1)
    assert resolution.multiplier == 1


def test_equal_version_distance_prefers_older_model():
    assert (
        find_fallback_model("gpt-6-sol", [_model("gpt-7-sol"), _model("gpt-5-sol")])
        == "gpt-5-sol"
    )
