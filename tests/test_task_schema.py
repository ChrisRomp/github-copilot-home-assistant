"""Strict structured outputs preserve HA validation and optional semantics."""

import pytest
import voluptuous as vol

from custom_components.github_copilot.schema import prepare_output_schema


def test_required_optional_nested_and_empty_objects():
    schema = vol.Schema(
        {
            vol.Required("name"): str,
            vol.Optional("comment"): str,
            vol.Required("items"): [
                {
                    vol.Required("count"): int,
                    vol.Optional("note"): str,
                }
            ],
        }
    )
    output = prepare_output_schema(schema, None)
    assert output.provider["additionalProperties"] is False
    assert set(output.provider["required"]) == {"name", "comment", "items"}
    assert output.parse(
        '{"name":"example","comment":null,"items":[{"count":2,"note":null}]}'
    ) == {"name": "example", "items": [{"count": 2}]}
    assert prepare_output_schema(vol.Schema({}), None).parse("{}") == {}


def test_preserves_genuine_null_and_returns_coerced_values():
    output = prepare_output_schema(
        vol.Schema(
            {
                vol.Optional("nullable"): vol.Any(None, str),
                vol.Required("value"): vol.Coerce(float),
            }
        ),
        None,
    )
    assert output.parse('{"nullable":null,"value":2}') == {
        "nullable": None,
        "value": 2.0,
    }
    assert type(output.parse('{"value":2}')["value"]) is float


@pytest.mark.parametrize(
    "text",
    [
        "not-json",
        "[]",
        "null",
        '{"extra":1}',
        '{"count":"wrong"}',
        '{"count":NaN}',
        '{"count":Infinity}',
        '{"count":null}',
    ],
)
def test_invalid_outputs_do_not_return_success(text):
    from homeassistant.exceptions import HomeAssistantError

    output = prepare_output_schema(vol.Schema({vol.Required("count"): int}), None)
    with pytest.raises(HomeAssistantError, match="does not match"):
        output.parse(text)


@pytest.mark.parametrize("value", ["1e999", '"1e999"', '"nan"', '"inf"'])
def test_numeric_overflow_and_coercion_cannot_return_nonfinite_values(value):
    from homeassistant.exceptions import HomeAssistantError

    output = prepare_output_schema(
        vol.Schema({vol.Required("value"): vol.Coerce(float)}), None
    )
    with pytest.raises(HomeAssistantError, match="does not match"):
        output.parse('{"value":' + value + "}")


def test_open_objects_are_rejected_before_request():
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers.llm import selector_serializer
    from homeassistant.helpers.selector import ObjectSelector

    with pytest.raises(HomeAssistantError, match="cannot be represented"):
        prepare_output_schema(
            vol.Schema({vol.Required("freeform"): ObjectSelector()}),
            selector_serializer,
        )


def test_real_ha_selector_structure():
    from homeassistant.helpers.llm import selector_serializer
    from homeassistant.helpers.selector import (
        BooleanSelector,
        NumberSelector,
        TextSelector,
    )

    output = prepare_output_schema(
        vol.Schema(
            {
                vol.Required("description"): TextSelector(),
                vol.Required("count"): NumberSelector({"min": 0, "max": 10}),
                vol.Optional("occupied"): BooleanSelector(),
            }
        ),
        selector_serializer,
    )
    assert output.parse('{"description":"Kitchen","count":2,"occupied":null}') == {
        "description": "Kitchen",
        "count": 2.0,
    }
    from homeassistant.exceptions import HomeAssistantError

    with pytest.raises(HomeAssistantError):
        output.parse('{"description":"Kitchen","count":20}')


@pytest.mark.parametrize("number_type", [int, float])
def test_numeric_bounds_are_hints_but_original_validation_is_preserved(number_type):
    from homeassistant.exceptions import HomeAssistantError

    output = prepare_output_schema(
        vol.Schema(
            {
                vol.Required("count"): vol.All(
                    vol.Coerce(number_type), vol.Range(min=0, max=10)
                )
            }
        ),
        None,
    )
    field = output.provider["properties"]["count"]
    assert "minimum" not in field
    assert "maximum" not in field
    assert "at least 0" in field["description"]
    assert "at most 10" in field["description"]
    original = output.original["properties"]["count"]
    assert original["minimum"] == 0
    assert original["maximum"] == 10
    for value in (0, 5, 10):
        assert output.parse('{"count":' + str(value) + "}") == {"count": value}
    for value in (-1, 11):
        with pytest.raises(HomeAssistantError, match="does not match"):
            output.parse('{"count":' + str(value) + "}")


def test_time_selector_uses_local_time_not_rfc3339_time():
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers.llm import selector_serializer
    from homeassistant.helpers.selector import TimeSelector

    output = prepare_output_schema(
        vol.Schema({vol.Required("time"): TimeSelector()}), selector_serializer
    )
    field = output.provider["properties"]["time"]
    assert "format" not in field
    assert "HH:MM:SS" in field["description"]
    assert output.parse('{"time":"10:00:00"}') == {"time": "10:00:00"}
    for time in ("10:00:00Z", "10:00:00+01:00", "25:00:00"):
        with pytest.raises(HomeAssistantError):
            output.parse('{"time":"' + time + '"}')


def test_multi_select_removes_provider_unsupported_uniqueness_constraint():
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers.llm import selector_serializer
    from homeassistant.helpers.selector import SelectSelector

    output = prepare_output_schema(
        vol.Schema(
            {
                vol.Required("chores"): SelectSelector(
                    {"options": ["dishes", "laundry"], "multiple": True}
                )
            }
        ),
        selector_serializer,
    )
    field = output.provider["properties"]["chores"]
    assert "uniqueItems" not in field
    assert "at most once" in field["description"]
    assert output.original["properties"]["chores"]["uniqueItems"] is True
    assert output.parse('{"chores":["dishes","laundry"]}') == {
        "chores": ["dishes", "laundry"]
    }
    assert output.parse('{"chores":[]}') == {"chores": []}
    with pytest.raises(HomeAssistantError, match="does not match"):
        output.parse('{"chores":["vacuuming"]}')


def test_date_and_datetime_formats_are_not_removed():
    from homeassistant.helpers.llm import selector_serializer
    from homeassistant.helpers.selector import DateSelector, DateTimeSelector

    output = prepare_output_schema(
        vol.Schema(
            {
                vol.Required("date"): DateSelector(),
                vol.Required("when"): DateTimeSelector(),
            }
        ),
        selector_serializer,
    )
    assert output.provider["properties"]["date"]["format"] == "date"
    assert output.provider["properties"]["when"]["format"] == "date-time"
