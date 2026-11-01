"""Use the schema converter matching Home Assistant's validator generation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

try:
    from probatio import UNSUPPORTED, to_openapi
except ModuleNotFoundError as err:
    if err.name != "probatio":
        raise
    from voluptuous_openapi import UNSUPPORTED
    from voluptuous_openapi import convert as to_openapi

__all__ = ["UNSUPPORTED", "convert_tool_schema"]


def convert_tool_schema(
    parameters: Any, custom_serializer: Callable[[Any], Any] | None
) -> dict[str, Any]:
    """Convert native/legacy HA tool schemas with their selector hook."""
    return to_openapi(
        parameters, custom_serializer=custom_serializer, openapi_version="3.1.0"
    )
