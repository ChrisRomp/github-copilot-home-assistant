"""Satellite history authorization and Recorder boundary tests."""

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

from custom_components.github_copilot.const import (
    CONF_ALLOW_ALL_SATELLITES,
    CONF_ALLOWED_SATELLITES,
)


def history_setup(ha_boundary, monkeypatch):
    history = ha_boundary.load("history")
    satellite = SimpleNamespace(
        id="stable-bedroom-id",
        domain="assist_satellite",
        entity_id="assist_satellite.bedroom",
    )
    registry = SimpleNamespace(
        entities={"bedroom": satellite},
        async_get=Mock(return_value=satellite),
    )
    entity_registry = ModuleType("homeassistant.helpers.entity_registry")
    entity_registry.async_get = Mock(return_value=registry)
    monkeypatch.setitem(sys.modules, entity_registry.__name__, entity_registry)
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"],
        "entity_registry",
        entity_registry,
        raising=False,
    )
    recorder = ModuleType("homeassistant.components.recorder")
    recorder.is_entity_recorded = Mock(return_value=True)
    executor = SimpleNamespace(async_add_executor_job=None)
    recorder.get_instance = Mock(return_value=executor)
    monkeypatch.setitem(sys.modules, recorder.__name__, recorder)
    monkeypatch.setattr(
        sys.modules["homeassistant.components"], "recorder", recorder, raising=False
    )
    sys.modules["homeassistant.components.conversation"].DOMAIN = "conversation"
    hass = SimpleNamespace(
        config=SimpleNamespace(components={"recorder"}),
    )
    user_input = SimpleNamespace(
        context=SimpleNamespace(user_id=None),
        satellite_id=satellite.entity_id,
    )
    options = {
        CONF_ALLOWED_SATELLITES: [satellite.id],
        CONF_ALLOW_ALL_SATELLITES: False,
    }
    return SimpleNamespace(
        history=history,
        hass=hass,
        options=options,
        user_input=user_input,
        registry=registry,
        satellite=satellite,
        executor=executor,
    )


def test_satellite_history_requires_anonymous_request_and_configured_satellite(
    ha_boundary, monkeypatch
):
    setup = history_setup(ha_boundary, monkeypatch)

    assert (
        setup.history.satellite_history_status(
            setup.hass, setup.options, setup.user_input
        )
        == "available"
    )
    setup.user_input.context.user_id = "authenticated-user"
    assert (
        setup.history.satellite_history_status(
            setup.hass, setup.options, setup.user_input
        )
        == "authenticated_request"
    )

    setup.user_input.context.user_id = None
    setup.options[CONF_ALLOWED_SATELLITES] = []
    assert (
        setup.history.satellite_history_status(
            setup.hass, setup.options, setup.user_input
        )
        == "satellite_not_allowed"
    )

    setup.options[CONF_ALLOW_ALL_SATELLITES] = True
    assert (
        setup.history.satellite_history_status(
            setup.hass, setup.options, setup.user_input
        )
        == "available"
    )
    setup.satellite.disabled_by = "user"
    assert (
        setup.history.satellite_history_status(
            setup.hass, setup.options, setup.user_input
        )
        == "unrecognized_satellite"
    )


async def test_history_returns_only_exposed_current_entities_with_stored_times(
    ha_boundary, monkeypatch
):
    setup = history_setup(ha_boundary, monkeypatch)
    entity_id = "sensor.bedroom_temperature"
    recorded_at = datetime(2026, 6, 1, 15, 30, tzinfo=UTC)
    state = SimpleNamespace(
        entity_id=entity_id,
        domain="sensor",
        name="Bedroom temperature",
        state="21.5",
        last_updated=recorded_at,
        attributes={"unit_of_measurement": "°C", "secret": "not returned"},
    )
    setup.hass.states = SimpleNamespace(async_all=Mock(return_value=[state]))

    exposed_entities = ModuleType(
        "homeassistant.components.homeassistant.exposed_entities"
    )
    exposed_entities.async_should_expose = Mock(return_value=True)
    monkeypatch.setitem(sys.modules, exposed_entities.__name__, exposed_entities)

    query_calls = []

    def recorder_query(
        hass,
        start_time,
        end_time,
        requested_entity_id,
        significant_changes_only,
        descending,
        limit,
        include_start_time_state,
    ):
        query_calls.append((start_time, end_time, requested_entity_id, limit))
        return {requested_entity_id: [state]}

    recorder_history = ModuleType("homeassistant.components.recorder.history")
    recorder_history.state_changes_during_period = recorder_query
    monkeypatch.setitem(sys.modules, recorder_history.__name__, recorder_history)

    async def run_executor_job(function):
        return function()

    setup.executor.async_add_executor_job = run_executor_job
    result = await setup.history.async_get_history(
        setup.hass,
        setup.options,
        setup.user_input,
        {
            "entity_ids": [entity_id],
            "start_time": "2026-06-01T00:00:00+00:00",
            "end_time": "2026-06-02T00:00:00+00:00",
        },
        setup.history.HistoryTurnBudget(),
        asyncio.Semaphore(2),
    )

    assert result == {
        "complete": True,
        "entities": [
            {
                "entity_id": entity_id,
                "records": [
                    {
                        "state": "21.5",
                        "recorded_at": setup.history.dt_util.as_local(
                            recorded_at
                        ).isoformat(timespec="seconds"),
                        "unit": "°C",
                    }
                ],
                "complete": True,
                "status": "complete",
                "recording_enabled_now": True,
                "requested_start": "2026-06-01T00:00:00+00:00",
                "requested_end": "2026-06-02T00:00:00+00:00",
                "covered_start": "2026-06-01T00:00:00+00:00",
                "covered_end": "2026-06-02T00:00:00+00:00",
            }
        ],
    }
    assert query_calls == [
        (
            datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
            datetime(2026, 6, 2, 0, 0, tzinfo=UTC),
            entity_id,
            setup.history.HISTORY_MAX_ROWS_PER_ENTITY + 1,
        )
    ]
    assert {
        call.args[1] for call in exposed_entities.async_should_expose.call_args_list
    } == {"conversation"}


async def test_history_rejects_entities_not_currently_exposed(ha_boundary, monkeypatch):
    setup = history_setup(ha_boundary, monkeypatch)
    setup.hass.states = SimpleNamespace(async_all=Mock(return_value=[]))
    exposed_entities = ModuleType(
        "homeassistant.components.homeassistant.exposed_entities"
    )
    exposed_entities.async_should_expose = Mock(return_value=True)
    monkeypatch.setitem(sys.modules, exposed_entities.__name__, exposed_entities)
    result = await setup.history.async_get_history(
        setup.hass,
        setup.options,
        setup.user_input,
        {
            "entity_ids": ["sensor.deleted_temperature"],
            "start_time": "2026-06-01T00:00:00+00:00",
            "end_time": "2026-06-02T00:00:00+00:00",
        },
        setup.history.HistoryTurnBudget(),
        asyncio.Semaphore(2),
    )

    assert result == {"error": "entity_not_available"}


async def test_point_history_uses_actual_prior_state_even_if_current_recording_is_off(
    ha_boundary, monkeypatch
):
    setup = history_setup(ha_boundary, monkeypatch)
    entity_id = "sensor.bedroom_temperature"
    requested_time = datetime(2026, 6, 2, 16, 0, tzinfo=UTC)
    recorded_at = datetime(2026, 6, 2, 15, 45, tzinfo=UTC)
    current_state = SimpleNamespace(
        entity_id=entity_id,
        domain="sensor",
        name="Bedroom temperature",
        state="22",
        last_updated=datetime(2026, 6, 3, 0, 0, tzinfo=UTC),
        attributes={},
    )
    prior_state = SimpleNamespace(
        entity_id=entity_id,
        state="21.5",
        last_updated=recorded_at,
        attributes={"unit_of_measurement": "°C"},
    )
    setup.hass.states = SimpleNamespace(async_all=Mock(return_value=[current_state]))
    exposed_entities = ModuleType(
        "homeassistant.components.homeassistant.exposed_entities"
    )
    exposed_entities.async_should_expose = Mock(return_value=True)
    monkeypatch.setitem(sys.modules, exposed_entities.__name__, exposed_entities)

    query_calls = []

    def recorder_query(*args):
        query_calls.append(args)
        return {entity_id: [prior_state]}

    recorder_history = ModuleType("homeassistant.components.recorder.history")
    recorder_history.state_changes_during_period = recorder_query
    monkeypatch.setitem(sys.modules, recorder_history.__name__, recorder_history)

    async def run_executor_job(function):
        return function()

    setup.executor.async_add_executor_job = run_executor_job
    monkeypatch.setattr(
        setup.history.dt_util,
        "utcnow",
        lambda: datetime(2026, 6, 3, 0, 0, tzinfo=UTC),
    )
    monkeypatch.setattr(setup.history.dt_util, "as_local", lambda value: value)
    setup.registry.async_get.return_value = setup.satellite
    sys.modules[
        "homeassistant.components.recorder"
    ].is_entity_recorded.return_value = False

    result = await setup.history.async_get_history(
        setup.hass,
        setup.options,
        setup.user_input,
        {"entity_ids": [entity_id], "time": requested_time.isoformat()},
        setup.history.HistoryTurnBudget(),
        asyncio.Semaphore(2),
    )

    assert result["entities"][0]["records"] == [
        {
            "state": "21.5",
            "recorded_at": recorded_at.isoformat(timespec="seconds"),
            "unit": "°C",
        }
    ]
    assert result["entities"][0]["status"] == "found"
    assert result["entities"][0]["recording_enabled_now"] is False
    assert len(query_calls) == 1
    assert query_calls[0][5:] == (
        False,
        setup.history.HISTORY_MAX_ROWS_PER_ENTITY + 1,
        False,
    )


async def test_point_history_finds_latest_state_in_dense_sensor_window(
    ha_boundary, monkeypatch
):
    setup = history_setup(ha_boundary, monkeypatch)
    entity_id = "sensor.bedroom_temperature"
    requested_time = datetime(2026, 6, 2, 16, 0, tzinfo=UTC)
    current_state = SimpleNamespace(
        entity_id=entity_id,
        domain="sensor",
        name="Bedroom temperature",
        state="199",
        last_updated=requested_time + timedelta(days=1),
        attributes={},
    )
    states = [
        SimpleNamespace(
            state=str(index),
            last_updated=requested_time - timedelta(minutes=200 - index),
            attributes={"unit_of_measurement": "°C"},
        )
        for index in range(200)
    ]
    setup.hass.states = SimpleNamespace(async_all=Mock(return_value=[current_state]))
    exposed_entities = ModuleType(
        "homeassistant.components.homeassistant.exposed_entities"
    )
    exposed_entities.async_should_expose = Mock(return_value=True)
    monkeypatch.setitem(sys.modules, exposed_entities.__name__, exposed_entities)

    def recorder_query(
        hass,
        start_time,
        end_time,
        requested_entity_id,
        no_attributes,
        descending,
        limit,
        include_start_time_state,
    ):
        matching = [
            state for state in states if start_time < state.last_updated < end_time
        ]
        return {requested_entity_id: matching[:limit]}

    recorder_history = ModuleType("homeassistant.components.recorder.history")
    recorder_history.state_changes_during_period = recorder_query
    monkeypatch.setitem(sys.modules, recorder_history.__name__, recorder_history)

    async def run_executor_job(function):
        return function()

    setup.executor.async_add_executor_job = run_executor_job
    monkeypatch.setattr(
        setup.history.dt_util,
        "utcnow",
        lambda: requested_time + timedelta(days=1),
    )
    result = await setup.history.async_get_history(
        setup.hass,
        setup.options,
        setup.user_input,
        {"entity_ids": [entity_id], "time": requested_time.isoformat()},
        setup.history.HistoryTurnBudget(),
        asyncio.Semaphore(2),
    )

    entity_result = result["entities"][0]
    assert entity_result["status"] == "found"
    assert entity_result["records"][0]["state"] == "199"
    assert (
        entity_result["records"][0]["recorded_at"]
        == states[-1].last_updated.isoformat()
    )
    assert entity_result["records"][0]["recorded_at"] != requested_time.isoformat()
    assert entity_result["requested_time"] == requested_time.isoformat()


async def test_range_history_reports_truncation_and_overlap_boundary(
    ha_boundary, monkeypatch
):
    setup = history_setup(ha_boundary, monkeypatch)
    entity_id = "sensor.bedroom_temperature"
    range_start = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    range_end = datetime(2026, 6, 2, 0, 0, tzinfo=UTC)
    states = [
        SimpleNamespace(
            state=str(index),
            last_updated=range_start + timedelta(minutes=index),
            attributes={},
        )
        for index in range(1, 102)
    ]
    current_state = SimpleNamespace(
        entity_id=entity_id,
        domain="sensor",
        name="Bedroom temperature",
        state="101",
        last_updated=datetime(2026, 6, 3, 0, 0, tzinfo=UTC),
        attributes={},
    )
    setup.hass.states = SimpleNamespace(async_all=Mock(return_value=[current_state]))
    exposed_entities = ModuleType(
        "homeassistant.components.homeassistant.exposed_entities"
    )
    exposed_entities.async_should_expose = Mock(return_value=True)
    monkeypatch.setitem(sys.modules, exposed_entities.__name__, exposed_entities)
    recorder_history = ModuleType("homeassistant.components.recorder.history")
    recorder_history.state_changes_during_period = Mock(
        return_value={entity_id: states[:101]}
    )
    monkeypatch.setitem(sys.modules, recorder_history.__name__, recorder_history)

    async def run_executor_job(function):
        return function()

    setup.executor.async_add_executor_job = run_executor_job
    result = await setup.history.async_get_history(
        setup.hass,
        setup.options,
        setup.user_input,
        {
            "entity_ids": [entity_id],
            "start_time": range_start.isoformat(),
            "end_time": range_end.isoformat(),
        },
        setup.history.HistoryTurnBudget(),
        asyncio.Semaphore(2),
    )

    entity_result = result["entities"][0]
    assert result["complete"] is False
    assert entity_result["complete"] is False
    assert entity_result["status"] == "incomplete"
    assert len(entity_result["records"]) == 100
    last_returned = states[99].last_updated
    assert (
        entity_result["covered_end"]
        == setup.history.dt_util.as_local(last_returned).isoformat()
    )
    assert (
        entity_result["continuation_start_time"]
        == (last_returned - timedelta(microseconds=1)).isoformat()
    )


def test_output_trimming_restarts_continuation_when_all_records_are_removed(
    ha_boundary, monkeypatch
):
    history = ha_boundary.load("history")
    monkeypatch.setattr(history, "HISTORY_MAX_OUTPUT_BYTES_PER_TURN", 350)
    result = {
        "complete": True,
        "entities": [
            {
                "entity_id": "sensor.bedroom_temperature",
                "records": [
                    {"state": "x" * 500, "recorded_at": "2026-06-01T12:00:00+00:00"}
                ],
                "complete": True,
                "status": "complete",
                "requested_start": "2026-06-01T00:00:00+00:00",
                "requested_end": "2026-06-02T00:00:00+00:00",
                "covered_start": "2026-06-01T00:00:00+00:00",
                "covered_end": "2026-06-02T00:00:00+00:00",
            }
        ],
    }

    output = history._bounded_output(result, history.HistoryTurnBudget())

    entity = output["entities"][0]
    assert entity["records"] == []
    assert entity["covered_end"] is None
    assert entity["continuation_start_time"] == entity["requested_start"]
    assert entity["status"] == "incomplete"


async def test_empty_range_returns_real_state_at_start_when_unchanged(
    ha_boundary, monkeypatch
):
    setup = history_setup(ha_boundary, monkeypatch)
    entity_id = "light.bedroom"
    range_start = datetime(2026, 6, 2, 0, 0, tzinfo=UTC)
    range_end = datetime(2026, 6, 3, 0, 0, tzinfo=UTC)
    recorded_at = range_start - timedelta(hours=2)
    current_state = SimpleNamespace(
        entity_id=entity_id,
        domain="light",
        name="Bedroom light",
        state="on",
        last_updated=range_end + timedelta(days=1),
        attributes={},
    )
    prior_state = SimpleNamespace(
        state="on",
        last_updated=recorded_at,
        attributes={},
    )
    setup.hass.states = SimpleNamespace(async_all=Mock(return_value=[current_state]))
    exposed_entities = ModuleType(
        "homeassistant.components.homeassistant.exposed_entities"
    )
    exposed_entities.async_should_expose = Mock(return_value=True)
    monkeypatch.setitem(sys.modules, exposed_entities.__name__, exposed_entities)

    def recorder_query(
        hass,
        start_time,
        end_time,
        requested_entity_id,
        no_attributes,
        descending,
        limit,
        include_start_time_state,
    ):
        if end_time <= range_start + timedelta(microseconds=1):
            return {requested_entity_id: [prior_state]}
        return {}

    recorder_history = ModuleType("homeassistant.components.recorder.history")
    recorder_history.state_changes_during_period = recorder_query
    monkeypatch.setitem(sys.modules, recorder_history.__name__, recorder_history)

    async def run_executor_job(function):
        return function()

    setup.executor.async_add_executor_job = run_executor_job
    monkeypatch.setattr(
        setup.history.dt_util,
        "utcnow",
        lambda: range_end + timedelta(days=1),
    )
    result = await setup.history.async_get_history(
        setup.hass,
        setup.options,
        setup.user_input,
        {
            "entity_ids": [entity_id],
            "start_time": range_start.isoformat(),
            "end_time": range_end.isoformat(),
        },
        setup.history.HistoryTurnBudget(),
        asyncio.Semaphore(2),
    )

    entity = result["entities"][0]
    assert entity["status"] == "no_changes_in_range"
    assert entity["records"] == []
    assert entity["state_at_start"] == {
        "state": "on",
        "recorded_at": recorded_at.isoformat(),
    }


async def test_empty_range_reports_current_recording_exclusion(
    ha_boundary, monkeypatch
):
    setup = history_setup(ha_boundary, monkeypatch)
    entity_id = "sensor.bedroom_temperature"
    current_state = SimpleNamespace(
        entity_id=entity_id,
        domain="sensor",
        name="Bedroom temperature",
        state="unknown",
        last_updated=datetime(2026, 6, 3, 0, 0, tzinfo=UTC),
        attributes={},
    )
    setup.hass.states = SimpleNamespace(async_all=Mock(return_value=[current_state]))
    exposed_entities = ModuleType(
        "homeassistant.components.homeassistant.exposed_entities"
    )
    exposed_entities.async_should_expose = Mock(return_value=True)
    monkeypatch.setitem(sys.modules, exposed_entities.__name__, exposed_entities)
    recorder = sys.modules["homeassistant.components.recorder"]
    recorder.is_entity_recorded.return_value = False
    recorder_history = ModuleType("homeassistant.components.recorder.history")
    recorder_history.state_changes_during_period = Mock(return_value={})
    monkeypatch.setitem(sys.modules, recorder_history.__name__, recorder_history)

    async def run_executor_job(function):
        return function()

    setup.executor.async_add_executor_job = run_executor_job
    result = await setup.history.async_get_history(
        setup.hass,
        setup.options,
        setup.user_input,
        {
            "entity_ids": [entity_id],
            "start_time": "2026-06-01T00:00:00+00:00",
            "end_time": "2026-06-02T00:00:00+00:00",
        },
        setup.history.HistoryTurnBudget(),
        asyncio.Semaphore(2),
    )

    assert result["entities"][0]["status"] == "not_recorded"
    assert result["entities"][0]["recording_enabled_now"] is False


async def test_recorder_database_errors_return_unavailable(ha_boundary, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    setup = history_setup(ha_boundary, monkeypatch)
    entity_id = "sensor.bedroom_temperature"
    state = SimpleNamespace(
        entity_id=entity_id,
        domain="sensor",
        name="Bedroom temperature",
        state="21",
        last_updated=datetime(2026, 6, 1, tzinfo=UTC),
        attributes={},
    )
    setup.hass.states = SimpleNamespace(async_all=Mock(return_value=[state]))
    exposed_entities = ModuleType(
        "homeassistant.components.homeassistant.exposed_entities"
    )
    exposed_entities.async_should_expose = Mock(return_value=True)
    monkeypatch.setitem(sys.modules, exposed_entities.__name__, exposed_entities)
    sys.modules[
        "homeassistant.components.recorder"
    ].is_entity_recorded.side_effect = SQLAlchemyError("database unavailable")

    result = await setup.history.async_get_history(
        setup.hass,
        setup.options,
        setup.user_input,
        {
            "entity_ids": [entity_id],
            "start_time": "2026-06-01T00:00:00+00:00",
            "end_time": "2026-06-02T00:00:00+00:00",
        },
        setup.history.HistoryTurnBudget(),
        asyncio.Semaphore(2),
    )

    assert result == {"error": "recorder_unavailable"}
