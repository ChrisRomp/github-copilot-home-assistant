"""Copilot model availability and same-family fallback selection."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import zip_longest
from typing import Any

_VERSION_TOKEN = re.compile(r"^v?\d+(?:\.\d+)*$")


@dataclass(frozen=True)
class ModelResolution:
    """The model to use for a configured model ID."""

    requested: str
    model_id: str | None
    multiplier: float | None = None

    @property
    def is_available(self) -> bool:
        """Return whether the configured model can be used as-is."""
        return self.model_id == self.requested

    @property
    def is_fallback(self) -> bool:
        """Return whether a same-family substitute was selected."""
        return self.model_id is not None and self.model_id != self.requested


def _policy_state(model: Any) -> str | None:
    """Return a model policy state from stable or experimental SDK types."""
    state = getattr(getattr(model, "policy", None), "state", None)
    return getattr(state, "value", state)


def selectable_models(models: Iterable[Any]) -> list[Any]:
    """Return models that are not disabled by account or organization policy."""
    return [model for model in models if _policy_state(model) != "disabled"]


def model_family(model_id: str) -> str:
    """Infer a model family by removing version tokens from its ID.

    For example, ``claude-sonnet-4.5`` becomes ``claude-sonnet`` and
    ``gpt-6.1-sol`` becomes ``gpt-sol``.
    """
    tokens = model_id.lower().split("-")
    return "-".join(token for token in tokens if not _VERSION_TOKEN.match(token))


def _version_key(model_id: str) -> tuple[int, ...]:
    """Return numeric version components in ID order for newest-first sorting."""
    key: list[int] = []
    for token in model_id.lower().split("-"):
        if _VERSION_TOKEN.match(token):
            key.extend(int(part) for part in token.lstrip("v").split("."))
    return tuple(key)


def model_multiplier(model: Any) -> float | None:
    """Return valid SDK billing metadata, never infer pricing from a model name."""
    value = getattr(getattr(model, "billing", None), "multiplier", None)
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    ):
        return float(value)
    return None


def model_billing(model_id: str, models: Sequence[Any]) -> dict[str, Any]:
    """Capture the selected model's cost for when it disappears from discovery."""
    model = next((model for model in models if model.id == model_id), None)
    return {"model": model_id, "multiplier": model_multiplier(model)}


def model_budget(model_id: str, billing: dict[str, Any]) -> float | None:
    """Read a stored billing snapshot only for the model it describes."""
    return billing.get("multiplier") if billing.get("model") == model_id else None


def _version_distance(
    requested: tuple[int, ...], candidate: tuple[int, ...]
) -> tuple[int, ...]:
    """Rank adjacent versions, preferring the near edge of a different major."""
    pairs = list(zip_longest(requested, candidate, fillvalue=0))
    for index, (left, right) in enumerate(pairs):
        if left != right:
            direction = 1 if right > left else -1
            return (
                (0,) * index
                + (abs(right - left),)
                + tuple(direction * value for _, value in pairs[index + 1 :])
            )
    return (0,) * len(pairs)


def find_fallback_model(
    model_id: str, models: Sequence[Any], *, max_multiplier: float | None = None
) -> str | None:
    """Choose the closest same-family version within the known billing limit."""
    if max_multiplier is None:
        max_multiplier = model_billing(model_id, models)["multiplier"]
    family = model_family(model_id)
    candidates = [
        model.id
        for model in selectable_models(models)
        if model.id != model_id
        and model_family(model.id) == family
        and (
            max_multiplier is None
            or (
                (cost := model_multiplier(model)) is not None and cost <= max_multiplier
            )
        )
    ]
    if not candidates:
        return None
    requested_version = _version_key(model_id)
    return min(
        candidates,
        key=lambda candidate: (
            _version_distance(requested_version, _version_key(candidate)),
            _version_key(candidate),
            candidate,
        ),
    )


def resolve_model(
    model_id: str, models: Sequence[Any], *, max_multiplier: float | None = None
) -> ModelResolution:
    """Use the configured model, a same-family fallback, or no model."""
    if any(model.id == model_id for model in selectable_models(models)):
        return ModelResolution(model_id, model_id)
    fallback = find_fallback_model(model_id, models, max_multiplier=max_multiplier)
    return ModelResolution(
        model_id,
        fallback,
        model_billing(fallback, models)["multiplier"] if fallback is not None else None,
    )
