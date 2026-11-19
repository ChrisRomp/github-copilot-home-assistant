"""Bounded read-only Assist history for explicitly allowed satellite requests."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from sqlalchemy.exc import SQLAlchemyError

from .const import (
    CONF_ALLOW_ALL_SATELLITES,
    CONF_ALLOWED_SATELLITES,
    HISTORY_DISCOVER_TOOL,
    HISTORY_MAX_DISCOVERY_RESULTS,
    HISTORY_MAX_ENTITIES_PER_CALL,
    HISTORY_MAX_OUTPUT_BYTES_PER_TURN,
    HISTORY_MAX_QUERIES_PER_TURN,
    HISTORY_MAX_RANGE,
    HISTORY_MAX_ROWS_PER_ENTITY,
    HISTORY_MAX_ROWS_PER_TURN,
    HISTORY_POINT_LOOKBACK_DAYS,
    HISTORY_READ_TOOL,
)

_ENTITY_ID_PATTERN = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_MAX_QUERY_LENGTH = 120
_MAX_STATE_LENGTH = 255
_MAX_UNIT_LENGTH = 64

DISCOVER_PARAMETERS = vol.Schema(
    {
        vol.Required("query"): vol.All(str, vol.Length(min=1, max=_MAX_QUERY_LENGTH)),
        vol.Optional("domain"): vol.All(str, vol.Length(min=1, max=64)),
    }
)
HISTORY_PARAMETERS = vol.Schema(
    {
        vol.Required("entity_ids"): vol.All(
            [str], vol.Length(min=1, max=HISTORY_MAX_ENTITIES_PER_CALL)
        ),
        vol.Optional("time"): str,
        vol.Optional("start_time"): str,
        vol.Optional("end_time"): str,
    }
)


@dataclass
class HistoryTurnBudget:
    """Bound Recorder work and returned history for one conversation turn."""

    queries: int = 0
    rows: int = 0
    output_bytes: int = 0


def selected_satellite_entity_ids(hass: Any, registry_ids: list[str]) -> list[str]:
    """Resolve saved stable registry IDs to current satellite entity IDs."""
    if not registry_ids:
        return []
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    return sorted(
        entry.entity_id
        for entry in registry.entities.values()
        if entry.id in registry_ids and entry.domain == "assist_satellite"
    )


def satellite_registry_id(hass: Any, satellite_id: str | None) -> str | None:
    """Return a registered Assist satellite's stable registry ID."""
    from homeassistant.helpers import entity_registry as er

    if not isinstance(satellite_id, str) or not satellite_id.startswith(
        "assist_satellite."
    ):
        return None
    entry = er.async_get(hass).async_get(satellite_id)
    if (
        entry is None
        or entry.domain != "assist_satellite"
        or getattr(entry, "disabled_by", None) is not None
    ):
        return None
    return entry.id


def satellite_history_status(
    hass: Any, options: dict[str, Any], user_input: Any
) -> str:
    """Return the capability reason without treating metadata as identity."""
    context = getattr(user_input, "context", None)
    if context is None or not hasattr(context, "user_id"):
        return "request_context_unavailable"
    if context.user_id is not None:
        return "authenticated_request"
    registry_id = satellite_registry_id(hass, getattr(user_input, "satellite_id", None))
    if registry_id is None:
        return "unrecognized_satellite"
    if not options.get(CONF_ALLOW_ALL_SATELLITES, False) and registry_id not in set(
        options.get(CONF_ALLOWED_SATELLITES, ())
    ):
        return "satellite_not_allowed"
    if "recorder" not in getattr(getattr(hass, "config", None), "components", ()):
        return "recorder_unavailable"
    return "available"


def _eligible_states(hass: Any) -> dict[str, Any]:
    """Return only current states exposed to this Assist conversation agent."""
    from homeassistant.components.conversation import DOMAIN as CONVERSATION_DOMAIN
    from homeassistant.components.homeassistant.exposed_entities import (
        async_should_expose,
    )

    return {
        state.entity_id: state
        for state in hass.states.async_all()
        if async_should_expose(hass, CONVERSATION_DOMAIN, state.entity_id)
    }


def _entity_area(hass: Any, entity_id: str) -> str:
    """Resolve an entity's explicit or device area for text discovery."""
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    if entry is None:
        return ""
    area_id = entry.area_id
    if area_id is None and entry.device_id:
        device = dr.async_get(hass).async_get(entry.device_id)
        if device is not None:
            area_id = dr.async_get_effective_area_id(hass, device)
    area = ar.async_get(hass).async_get_area(area_id) if area_id else None
    return area.name if area is not None else ""


def async_find_entities(hass: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """Find current Assist-exposed entity IDs without returning their state."""
    args = DISCOVER_PARAMETERS(arguments)
    query_text = args["query"].strip()
    if not query_text:
        return {"error": "invalid_query"}
    query = query_text.casefold().split()
    domain = args.get("domain", "").casefold()
    matches = []
    truncated = False
    for entity_id, state in _eligible_states(hass).items():
        if domain and state.domain != domain:
            continue
        area = _entity_area(hass, entity_id)
        name = str(getattr(state, "name", "") or entity_id)
        searchable = f"{entity_id} {name} {area}".casefold()
        if all(token in searchable for token in query):
            if len(matches) == HISTORY_MAX_DISCOVERY_RESULTS:
                truncated = True
                break
            matches.append(
                {
                    "entity_id": entity_id,
                    "name": name[:_MAX_QUERY_LENGTH],
                    "domain": state.domain,
                    **({"area": area[:_MAX_QUERY_LENGTH]} if area else {}),
                }
            )
    return {
        "entities": matches,
        "truncated": truncated,
    }


def _parse_aware_time(value: str) -> datetime | None:
    """Parse only explicit-offset timestamps to avoid ambiguous local times."""
    parsed = dt_util.parse_datetime(value)
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _state_record(state: Any) -> dict[str, Any]:
    """Return a compact historical state and only its stored unit attribute."""
    record: dict[str, Any] = {
        "state": str(state.state)[:_MAX_STATE_LENGTH],
        "recorded_at": dt_util.as_local(state.last_updated).isoformat(),
    }
    unit = getattr(state, "attributes", {}).get("unit_of_measurement")
    if isinstance(unit, str) and len(unit) <= _MAX_UNIT_LENGTH:
        record["unit"] = unit
    return record


async def _async_query(
    hass: Any,
    query_slots: asyncio.Semaphore,
    *,
    entity_id: str,
    start_time: datetime,
    end_time: datetime,
    descending: bool,
    limit: int,
) -> list[Any]:
    """Run one SQL-limited Recorder query and retain its slot through cancellation."""
    from homeassistant.components import recorder
    from homeassistant.components.recorder.history import state_changes_during_period

    await query_slots.acquire()
    job = asyncio.ensure_future(
        recorder.get_instance(hass).async_add_executor_job(
            partial(
                state_changes_during_period,
                hass,
                start_time,
                end_time,
                entity_id,
                False,
                descending,
                limit,
                False,
            )
        )
    )
    try:
        result = await asyncio.shield(job)
        return result.get(entity_id, [])
    except asyncio.CancelledError:
        drain = asyncio.gather(job, return_exceptions=True)
        while not drain.done():
            try:
                await asyncio.shield(drain)
            except asyncio.CancelledError:
                continue
        raise
    finally:
        query_slots.release()


async def _async_find_state_at_or_before(
    hass: Any,
    query_slots: asyncio.Semaphore,
    entity_id: str,
    point_time: datetime,
    budget: HistoryTurnBudget,
) -> Any | None:
    """Find the newest state by paging bounded windows in chronological order."""
    cursor = point_time
    while budget.queries < HISTORY_MAX_QUERIES_PER_TURN:
        if budget.rows >= HISTORY_MAX_ROWS_PER_TURN:
            return None
        window_start = cursor - timedelta(days=HISTORY_POINT_LOOKBACK_DAYS)
        page_start = window_start
        latest_state = None
        while budget.queries < HISTORY_MAX_QUERIES_PER_TURN:
            if budget.rows >= HISTORY_MAX_ROWS_PER_TURN:
                return None
            query_limit = min(
                HISTORY_MAX_ROWS_PER_ENTITY + 1,
                HISTORY_MAX_ROWS_PER_TURN - budget.rows,
            )
            if query_limit <= 0:
                return None
            budget.queries += 1
            query_start = (
                page_start - timedelta(microseconds=1)
                if page_start == window_start
                else page_start
            )
            states = await _async_query(
                hass,
                query_slots,
                entity_id=entity_id,
                start_time=query_start,
                end_time=point_time + timedelta(microseconds=1),
                descending=False,
                limit=query_limit,
            )
            budget.rows += len(states)
            states = [
                state
                for state in states
                if page_start <= dt_util.as_utc(state.last_updated) <= point_time
            ]
            if not states:
                if latest_state is not None:
                    return latest_state
                break
            latest_state = max(
                states, key=lambda state: dt_util.as_utc(state.last_updated)
            )
            if len(states) < query_limit:
                return latest_state
            next_page_start = dt_util.as_utc(latest_state.last_updated)
            if next_page_start <= page_start:
                return None
            page_start = next_page_start
        cursor = window_start - timedelta(microseconds=1)
    return None


def _size_bytes(value: dict[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def _bounded_output(
    result: dict[str, Any], budget: HistoryTurnBudget
) -> dict[str, Any]:
    """Fit the result into the remaining response budget without hiding truncation."""
    max_bytes = min(
        HISTORY_MAX_OUTPUT_BYTES_PER_TURN - budget.output_bytes,
        HISTORY_MAX_OUTPUT_BYTES_PER_TURN,
    )
    while _size_bytes(result) > max_bytes:
        trimmed = False
        for entity in reversed(result.get("entities", [])):
            records = entity.get("records", [])
            if records:
                records.pop()
                entity["complete"] = False
                entity["status"] = "incomplete"
                result["complete"] = False
                trimmed = True
                break
        if not trimmed:
            return {"error": "output_budget_exceeded"}
    for entity in result.get("entities", []):
        if "requested_start" not in entity:
            continue
        records = entity["records"]
        if entity["complete"]:
            entity["covered_end"] = entity["requested_end"]
            entity.pop("continuation_start_time", None)
        else:
            entity["covered_end"] = records[-1]["recorded_at"] if records else None
            if records:
                boundary = dt_util.parse_datetime(records[-1]["recorded_at"])
                if boundary is not None:
                    entity["continuation_start_time"] = (
                        boundary - timedelta(microseconds=1)
                    ).isoformat()
            else:
                entity["continuation_start_time"] = entity["requested_start"]
    size = _size_bytes(result)
    budget.output_bytes += size
    return result


async def async_get_history(
    hass: Any,
    options: dict[str, Any],
    user_input: Any,
    arguments: dict[str, Any],
    budget: HistoryTurnBudget,
    query_slots: asyncio.Semaphore,
) -> dict[str, Any]:
    """Read bounded retained state history for an allowed anonymous satellite."""
    from homeassistant.components import recorder

    args = HISTORY_PARAMETERS(arguments)
    if args.get("time") is not None and any(
        args.get(key) is not None for key in ("start_time", "end_time")
    ):
        return {"error": "invalid_time_range"}
    if satellite_history_status(hass, options, user_input) != "available":
        return {"error": "history_access_revoked"}
    if "recorder" not in hass.config.components:
        return {"error": "recorder_unavailable"}

    eligible = _eligible_states(hass)
    entity_ids = args["entity_ids"]
    if (
        len(set(entity_ids)) != len(entity_ids)
        or any(not _ENTITY_ID_PATTERN.fullmatch(entity_id) for entity_id in entity_ids)
        or any(entity_id not in eligible for entity_id in entity_ids)
    ):
        return {"error": "entity_not_available"}

    try:
        recording_status = {
            entity_id: recorder.is_entity_recorded(hass, entity_id)
            for entity_id in entity_ids
        }
    except (HomeAssistantError, KeyError, RuntimeError, SQLAlchemyError):
        return {"error": "recorder_unavailable"}

    now = dt_util.utcnow()
    point_time = (
        _parse_aware_time(args["time"]) if args.get("time") is not None else None
    )
    default_range = (
        args.get("time") is None
        and args.get("start_time") is None
        and args.get("end_time") is None
    )
    start_time = (
        _parse_aware_time(args["start_time"])
        if args.get("start_time") is not None
        else now - timedelta(days=1)
        if default_range
        else None
    )
    end_time = (
        _parse_aware_time(args["end_time"]) if args.get("end_time") is not None else now
    )
    if args.get("time") is not None:
        if point_time is None or point_time > now:
            return {"error": "invalid_time"}
    elif (
        start_time is None
        or end_time is None
        or start_time >= end_time
        or end_time > now + timedelta(minutes=1)
    ):
        return {"error": "invalid_time_range"}
    elif end_time - start_time > timedelta(days=HISTORY_MAX_RANGE):
        return {"error": "range_too_wide", "maximum_days": HISTORY_MAX_RANGE}

    result: dict[str, Any] = {"complete": True, "entities": []}
    for entity_id in entity_ids:
        entity_result: dict[str, Any] = {
            "entity_id": entity_id,
            "records": [],
            "complete": True,
            "status": "complete",
            "recording_enabled_now": recording_status[entity_id],
        }
        if point_time is not None:
            entity_result["requested_time"] = point_time.isoformat()
            try:
                state = await _async_find_state_at_or_before(
                    hass, query_slots, entity_id, point_time, budget
                )
            except (HomeAssistantError, KeyError, RuntimeError, SQLAlchemyError):
                return {"error": "recorder_unavailable"}
            if state is not None:
                entity_result["records"].append(_state_record(state))
                entity_result["status"] = "found"
            else:
                entity_result["status"] = "incomplete"
                entity_result["complete"] = False
                result["complete"] = False
        else:
            assert start_time is not None and end_time is not None
            entity_result.update(
                {
                    "requested_start": start_time.isoformat(),
                    "requested_end": end_time.isoformat(),
                    "covered_start": start_time.isoformat(),
                    "covered_end": end_time.isoformat(),
                }
            )
            if (
                budget.queries >= HISTORY_MAX_QUERIES_PER_TURN
                or budget.rows >= HISTORY_MAX_ROWS_PER_TURN
            ):
                entity_result["status"] = "incomplete"
                entity_result["complete"] = False
                result["complete"] = False
            else:
                budget.queries += 1
                query_limit = min(
                    HISTORY_MAX_ROWS_PER_ENTITY + 1,
                    HISTORY_MAX_ROWS_PER_TURN - budget.rows,
                )
                try:
                    states = await _async_query(
                        hass,
                        query_slots,
                        entity_id=entity_id,
                        start_time=start_time,
                        end_time=end_time,
                        descending=False,
                        limit=query_limit,
                    )
                except (HomeAssistantError, KeyError, RuntimeError, SQLAlchemyError):
                    return {"error": "recorder_unavailable"}
                budget.rows += len(states)
                result_limit = min(
                    HISTORY_MAX_ROWS_PER_ENTITY,
                    max(0, HISTORY_MAX_ROWS_PER_TURN - (budget.rows - len(states))),
                )
                records = states[:result_limit]
                entity_result["records"] = [_state_record(state) for state in records]
                if (
                    len(states) > HISTORY_MAX_ROWS_PER_ENTITY
                    or len(states) > result_limit
                ):
                    entity_result["status"] = "incomplete"
                    entity_result["complete"] = False
                    entity_result["covered_end"] = (
                        entity_result["records"][-1]["recorded_at"]
                        if entity_result["records"]
                        else None
                    )
                    if records:
                        boundary = dt_util.as_utc(records[-1].last_updated)
                        entity_result["continuation_start_time"] = (
                            boundary - timedelta(microseconds=1)
                        ).isoformat()
                    result["complete"] = False
                elif not states:
                    try:
                        state = await _async_find_state_at_or_before(
                            hass,
                            query_slots,
                            entity_id,
                            start_time,
                            budget,
                        )
                    except (
                        HomeAssistantError,
                        KeyError,
                        RuntimeError,
                        SQLAlchemyError,
                    ):
                        return {"error": "recorder_unavailable"}
                    if state is not None:
                        entity_result["state_at_start"] = _state_record(state)
                        entity_result["status"] = "no_changes_in_range"
                    else:
                        entity_result["status"] = (
                            "not_recorded"
                            if not recording_status[entity_id]
                            else "incomplete"
                        )
                        if entity_result["status"] == "incomplete":
                            entity_result["complete"] = False
                            result["complete"] = False

        result["entities"].append(entity_result)

        if satellite_history_status(hass, options, user_input) != "available":
            return {"error": "history_access_revoked"}
        if entity_id not in _eligible_states(hass):
            return {"error": "entity_not_available"}

    return _bounded_output(result, budget)


async def async_call_history_tool(
    hass: Any,
    options: dict[str, Any],
    user_input: Any,
    tool_name: str,
    arguments: dict[str, Any],
    budget: HistoryTurnBudget,
    query_slots: asyncio.Semaphore,
) -> dict[str, Any]:
    """Revalidate satellite access before dispatching a history tool."""
    if satellite_history_status(hass, options, user_input) != "available":
        return {"error": "history_access_denied"}
    if tool_name == HISTORY_DISCOVER_TOOL:
        return async_find_entities(hass, arguments)
    if tool_name == HISTORY_READ_TOOL:
        return await async_get_history(
            hass, options, user_input, arguments, budget, query_slots
        )
    raise HomeAssistantError("Unknown satellite history tool.")
