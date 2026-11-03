"""Use the schema converter matching Home Assistant's validator generation."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

from homeassistant.exceptions import HomeAssistantError

try:
    from probatio import UNSUPPORTED, Invalid, to_openapi
except ModuleNotFoundError as err:
    if err.name != "probatio":
        raise
    from voluptuous import Invalid
    from voluptuous_openapi import UNSUPPORTED
    from voluptuous_openapi import convert as to_openapi

__all__ = [
    "UNSUPPORTED",
    "OutputSchema",
    "convert_tool_schema",
    "prepare_output_schema",
]


def convert_tool_schema(
    parameters: Any, custom_serializer: Callable[[Any], Any] | None
) -> dict[str, Any]:
    """Convert native/legacy HA tool schemas with their selector hook."""
    return to_openapi(
        parameters, custom_serializer=custom_serializer, openapi_version="3.1.0"
    )


@dataclass(frozen=True)
class OutputSchema:
    """Keep the original validation contract separate from provider normalization."""

    provider: dict[str, Any]
    original: dict[str, Any]
    validate: Callable[[Any], Any]

    def parse(self, text: str) -> dict[str, Any]:
        """Return HA-validated data, never malformed or merely plausible JSON."""
        try:
            data = json.loads(
                text, parse_constant=_reject_constant, parse_float=_finite_float
            )
            if not isinstance(data, dict):
                raise ValueError("Expected an object")
            data = self.validate(_restore_optional_fields(data, self.original))
            if not isinstance(data, dict):
                raise ValueError("Expected a validated object")
            _check_finite(data)
        except (Invalid, ValueError, TypeError, OverflowError, RecursionError) as err:
            raise HomeAssistantError(
                "GitHub Copilot returned data that does not match the requested "
                "structure. The task was not retried; any completed actions "
                "have not been undone."
            ) from err
        return data


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number")
    return number


def _check_finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Non-finite validated number")
    if isinstance(value, dict):
        for child in value.values():
            _check_finite(child)
    elif isinstance(value, list):
        for child in value:
            _check_finite(child)


def prepare_output_schema(
    schema: Callable[[Any], Any], serializer: Callable[[Any], Any] | None
) -> OutputSchema:
    """Convert HA selectors without importing another integration's private code."""
    try:
        converted = convert_tool_schema(schema, serializer)
        original = _expand_schema(converted, converted, frozenset())
        if original.get("type") != "object":
            raise ValueError("Expected object root")
        provider = _strict_schema(original)
    except (Invalid, ValueError, TypeError, KeyError, RecursionError) as err:
        raise HomeAssistantError(
            "The requested AI Task structure cannot be represented as a strict "
            "output schema. Use explicitly defined fields and supported selectors."
        ) from err
    return OutputSchema(provider, original, schema)


def _expand_schema(
    node: dict[str, Any], root: dict[str, Any], references: frozenset[str]
) -> dict[str, Any]:
    if not isinstance(node, dict):
        raise ValueError("Unsupported schema")
    node = deepcopy(node)
    if reference := node.pop("$ref", None):
        if (
            reference in references
            or not reference.startswith("#/")
            or node.keys() - {"description", "title"}
        ):
            raise ValueError("Unsupported schema reference")
        target = root
        for part in unquote(reference[2:]).split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        node = {**_expand_schema(target, root, references | {reference}), **node}
    for key in ("$defs", "definitions"):
        node.pop(key, None)
    if "allOf" in node:
        branches = node.pop("allOf")
        if len(branches) != 1:
            raise ValueError("Unsupported intersection")
        branch = _expand_schema(branches[0], root, references)
        if any(
            node[key] != branch[key]
            for key in node.keys() & branch.keys()
            if key not in ("description", "title")
        ):
            raise ValueError("Conflicting intersection")
        node = {**branch, **node}
    if "properties" in node:
        node["properties"] = {
            key: _expand_schema(child, root, references)
            for key, child in node["properties"].items()
        }
    if "items" in node:
        node["items"] = _expand_schema(node["items"], root, references)
    if "anyOf" in node:
        node["anyOf"] = [
            _expand_schema(child, root, references) for child in node["anyOf"]
        ]
    return node


def _strict_schema(original: dict[str, Any]) -> dict[str, Any]:
    node = deepcopy(original)
    if not node or node.keys() & {
        "oneOf",
        "not",
        "if",
        "then",
        "else",
        "dependentRequired",
        "dependentSchemas",
        "patternProperties",
        "prefixItems",
        "contains",
    }:
        raise ValueError("Unsupported schema constraints")
    for annotation in (
        "default",
        "examples",
        "$comment",
        "deprecated",
        "readOnly",
        "writeOnly",
    ):
        node.pop(annotation, None)
    if node.get("format") == "time":
        node.pop("format")
        hint = "Use local time as HH:MM:SS without a timezone offset."
        node["description"] = (
            f"{node['description']} {hint}" if node.get("description") else hint
        )
    if node.get("format") in {
        "entity_id",
        "jinja2",
        "RFC 5646",
        "ISO 3166-1 alpha-2",
        "RGB",
    }:
        node.pop("format")
    types = node.get("type", [])
    types = [types] if isinstance(types, str) else types
    if "object" in types:
        if node.get("additionalProperties", False) is not False or (
            "properties" not in node and node.get("additionalProperties") is not False
        ):
            raise ValueError("Open object")
        required = node.get("required", [])
        properties = {}
        for key, child in node.get("properties", {}).items():
            strict_child = _strict_schema(child)
            if key not in required and not _allows_null(child):
                strict_child = {"anyOf": [strict_child, {"type": "null"}]}
            properties[key] = strict_child
        node.update(
            properties=properties,
            required=list(properties),
            additionalProperties=False,
        )
    if "array" in types:
        if "items" not in node:
            raise ValueError("Untyped array")
        if node.pop("uniqueItems", False):
            hint = "Return each selected value at most once."
            node["description"] = (
                f"{node['description']} {hint}" if node.get("description") else hint
            )
        node["items"] = _strict_schema(node["items"])
    if "anyOf" in node:
        if sum(child.get("type") in ("object", "array") for child in node["anyOf"]) > 1:
            raise ValueError("Ambiguous container alternatives")
        node["anyOf"] = [_strict_schema(child) for child in node["anyOf"]]
    return node


def _allows_null(schema: dict[str, Any]) -> bool:
    types = schema.get("type", [])
    return (
        types == "null"
        or isinstance(types, list)
        and "null" in types
        or "const" in schema
        and schema["const"] is None
        or "enum" in schema
        and None in schema["enum"]
        or any(_allows_null(child) for child in schema.get("anyOf", []))
    )


def _restore_optional_fields(value: Any, schema: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if "anyOf" in schema:
            object_branches = [
                child for child in schema["anyOf"] if child.get("type") == "object"
            ]
            if len(object_branches) > 1:
                raise ValueError("Ambiguous object alternatives")
            if object_branches:
                return _restore_optional_fields(value, object_branches[0])
        required = schema.get("required", [])
        return {
            key: _restore_optional_fields(item, properties.get(key, {}))
            for key, item in value.items()
            if not (
                item is None
                and key in properties
                and key not in required
                and not _allows_null(properties[key])
            )
        }
    if isinstance(value, list):
        branches = [
            child for child in schema.get("anyOf", []) if child.get("type") == "array"
        ]
        if branches:
            return _restore_optional_fields(value, branches[0])
        return [
            _restore_optional_fields(item, schema.get("items", {})) for item in value
        ]
    return value
