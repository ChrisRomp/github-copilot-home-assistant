"""Conversation sessions, HA tool bridging, and response streaming."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NoReturn

import voluptuous as vol
from copilot import ToolSet
from copilot.generated.rpc import PermissionDecisionReject, SessionUpdateOptionsParams
from copilot.session import SYSTEM_MESSAGE_SECTIONS
from copilot.session_events import (
    AssistantMessageData,
    AssistantMessageDeltaData,
    SessionErrorData,
)
from copilot.tools import Tool as CopilotTool
from copilot.tools import ToolInvocation
from copilot.tools import ToolResult as CopilotToolResult
from homeassistant.components import conversation
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import llm
from homeassistant.helpers.chat_session import current_session
from homeassistant.helpers.json import json_dumps

from .api_errors import is_model_unavailable_error, is_quota_error
from .auth import OAuthError, ReauthenticationRequired, async_start_reauth_flow
from .const import (
    AUTH_METHOD_PAT,
    CONF_AUTH_METHOD,
    CONF_ENABLE_DEVICE_CONTROL,
    CONF_MODEL,
    CONF_MODEL_BILLING,
    DEFAULT_SYSTEM_PROMPT,
    DOMAIN,
    ISSUE_MODEL_FALLBACK,
    ISSUE_MODEL_UNAVAILABLE,
    ISSUE_QUOTA_EXCEEDED,
    MAX_ACTIVE_SESSIONS,
    MAX_CONCURRENT_TURNS,
    MODEL_CACHE_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    SESSION_IDLE_SECONDS,
)
from .models import ModelResolution, model_budget, resolve_model, selectable_models
from .schema import convert_tool_schema

if TYPE_CHECKING:
    from copilot.client import GitHubTokenProviderArgs, GitHubTokenProviderResult
    from copilot.session import CopilotSession
    from homeassistant.components.conversation import ChatLog, ConversationInput
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)


@dataclass
class _ToolRequest:
    """A Copilot tool invocation waiting for HA to execute it."""

    sdk_name: str
    invocation: ToolInvocation
    result: asyncio.Future[CopilotToolResult]


@dataclass
class _SessionRecord:
    """A short-lived Copilot conversation session."""

    session: CopilotSession
    model_id: str
    prompt_context: dict[str, str] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.monotonic)
    current_handler: Any | None = None


class ConversationBackend:
    """Manage bounded, isolated, in-memory Assist sessions."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: Any,
        token_provider: Callable[
            [GitHubTokenProviderArgs], Awaitable[GitHubTokenProviderResult]
        ],
        model_lister: Callable[..., Awaitable[list[Any]]],
        *,
        access_token_getter: Callable[[], Awaitable[str]],
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client
        self.token_provider = token_provider
        self._access_token_getter = access_token_getter
        self._model_lister = model_lister
        self._model_resolution: ModelResolution | None = None
        self.active_model: str | None = None
        self._sessions: dict[str, _SessionRecord] = {}
        self._turn_slots = asyncio.Semaphore(MAX_CONCURRENT_TURNS)
        self._session_lock = asyncio.Lock()
        self._closed = False
        self._quota_exceeded = False
        self._rejected_models: dict[str, float] = {}
        self._available_model_ids: set[str] = set()

    async def async_handle_message(
        self,
        agent_id: str,
        user_input: ConversationInput,
        chat_log: ChatLog,
    ) -> None:
        """Stream a Copilot response into Home Assistant's chat log."""
        if self._closed:
            raise HomeAssistantError("GitHub Copilot is shutting down.")
        try:
            await asyncio.wait_for(self._turn_slots.acquire(), timeout=0.1)
        except TimeoutError as err:
            raise HomeAssistantError(
                "GitHub Copilot is busy. Please try again."
            ) from err

        try:
            await self._async_handle_message(agent_id, user_input, chat_log)
        finally:
            self._turn_slots.release()

    async def _async_handle_message(
        self,
        agent_id: str,
        user_input: ConversationInput,
        chat_log: ChatLog,
    ) -> None:
        """Serialize turns for one conversation and stream its events."""
        if chat_log.llm_api is not None and not self.entry.options.get(
            CONF_ENABLE_DEVICE_CONTROL
        ):
            raise HomeAssistantError("Device control is disabled for this agent.")
        try:
            await self._access_token_getter()
        except ReauthenticationRequired as err:
            await async_start_reauth_flow(self.hass, self.entry)
            raise HomeAssistantError(
                "GitHub sign-in has expired. Reauthenticate in Home Assistant."
            ) from err
        except OAuthError as err:
            raise HomeAssistantError(
                "Could not renew GitHub sign-in. Check the Home Assistant connection."
            ) from err

        context = user_input.as_llm_context(DOMAIN)
        session_key = self._session_key(chat_log.conversation_id, context)
        instructions = self._system_instructions(chat_log)
        record = await self._async_get_session(
            session_key, chat_log.llm_api, instructions
        )

        async with record.lock:
            if self._closed:
                raise HomeAssistantError("GitHub Copilot is shutting down.")
            record.last_used = time.monotonic()
            if record.prompt_context["instructions"] != instructions:
                # This host-injected system context is mutable; SDK section
                # transform callbacks are cached and cannot refresh live HA state.
                await record.session.rpc.options.update(
                    SessionUpdateOptionsParams(
                        organization_custom_instructions=instructions
                    )
                )
                record.prompt_context["instructions"] = instructions
            await self._async_update_session_model(record)
            queue: asyncio.Queue[dict[str, Any] | _ToolRequest] = asyncio.Queue()
            active = True
            tool_names: dict[str, str] = {}
            pending_tools: list[_ToolRequest] = []
            ha_api = chat_log.llm_api

            def on_tool(invocation: ToolInvocation) -> Any:
                if not active:
                    return CopilotToolResult(
                        text_result_for_llm="The request was cancelled.",
                        result_type="rejected",
                    )
                tool_request: _ToolRequest = _ToolRequest(
                    sdk_name=invocation.tool_name,
                    invocation=invocation,
                    result=asyncio.get_running_loop().create_future(),
                )
                pending_tools.append(tool_request)
                queue.put_nowait(tool_request)
                return tool_request.result

            sdk_tools = self._build_tools(ha_api, on_tool, tool_names)
            await record.session.set_tools(sdk_tools)
            record.current_handler = on_tool

            loop = asyncio.get_running_loop()
            session_error: SessionErrorData | None = None

            def on_event(event: Any) -> None:
                nonlocal session_error
                if getattr(event, "agent_id", None):
                    return
                if isinstance(event.data, SessionErrorData):
                    session_error = event.data
                if isinstance(event.data, AssistantMessageDeltaData):
                    loop.call_soon_threadsafe(
                        queue.put_nowait,
                        {"content": event.data.delta_content},
                    )

            unsubscribe = record.session.on(on_event)
            send_task = asyncio.create_task(
                record.session.send_and_wait(
                    user_input.text, timeout=REQUEST_TIMEOUT_SECONDS
                )
            )
            streamed_content = ""
            completed = False

            async def stream():
                """Interleave text deltas and serialized HA tool execution."""
                assistant_started = False
                while True:
                    if send_task.done() and queue.empty():
                        break
                    get_event = asyncio.create_task(queue.get())
                    done, _ = await asyncio.wait(
                        (get_event, send_task),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if get_event not in done:
                        get_event.cancel()
                        await asyncio.gather(get_event, return_exceptions=True)
                        if send_task.done() and queue.empty():
                            break
                        continue

                    item = get_event.result()
                    if isinstance(item, dict):
                        nonlocal streamed_content
                        streamed_content += item["content"]
                        if not assistant_started:
                            item = {"role": "assistant", **item}
                            assistant_started = True
                        yield item
                        continue

                    if not active:
                        if not item.result.done():
                            item.result.set_result(
                                CopilotToolResult(
                                    text_result_for_llm="The request was cancelled.",
                                    result_type="rejected",
                                )
                            )
                        continue

                    ha_name = tool_names.get(item.sdk_name)
                    arguments = item.invocation.arguments
                    if (
                        ha_name is None
                        or ha_api is None
                        or not isinstance(arguments, dict)
                    ):
                        if not item.result.done():
                            item.result.set_result(
                                CopilotToolResult(
                                    text_result_for_llm=(
                                        "This Home Assistant tool is unavailable."
                                    ),
                                    result_type="failure",
                                )
                            )
                        continue

                    ha_tool_input = llm.ToolInput(
                        tool_name=ha_name,
                        tool_args=arguments,
                        id=item.invocation.tool_call_id or uuid.uuid4().hex,
                        external=True,
                    )
                    yield {
                        "role": "assistant",
                        "tool_calls": [ha_tool_input],
                    }
                    try:
                        tool_result = await ha_api.async_call_tool(ha_tool_input)
                    except (HomeAssistantError, vol.Invalid) as err:
                        tool_result = {"error": type(err).__name__}
                        if str(err):
                            tool_result["error_text"] = str(err)
                    result_type = getattr(llm, "ToolResult", None)
                    if result_type is not None and isinstance(tool_result, result_type):
                        tool_failed = tool_result.error
                        tool_result = tool_result.data
                    else:
                        tool_failed = (
                            isinstance(tool_result, dict) and "error" in tool_result
                        )
                    try:
                        if not isinstance(tool_result, dict):
                            raise TypeError("Tool result data must be an object.")
                        serialized_result = json_dumps(tool_result)
                        tool_result = json.loads(serialized_result)
                    except (TypeError, ValueError):
                        _LOGGER.warning(
                            "Could not serialize the result of Home Assistant tool %s; "
                            "the action will not be retried",
                            ha_name,
                        )
                        tool_result = {
                            "error": "result_serialization_failed",
                            "error_text": (
                                "The action may already have completed, but its result "
                                "could not be encoded. Do not repeat this action."
                            ),
                        }
                        tool_failed = True
                        serialized_result = json.dumps(tool_result)
                    yield {
                        "role": "tool_result",
                        "tool_call_id": ha_tool_input.id,
                        "tool_name": ha_name,
                        **(
                            {"result": result_type(data=tool_result, error=tool_failed)}
                            if result_type is not None
                            else {"tool_result": tool_result}
                        ),
                    }
                    assistant_started = False
                    if not item.result.done():
                        item.result.set_result(
                            CopilotToolResult(
                                text_result_for_llm=serialized_result,
                                result_type=("failure" if tool_failed else "success"),
                            )
                        )

            try:
                async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                    async for _ in chat_log.async_add_delta_content_stream(
                        agent_id, stream()
                    ):
                        pass
                    try:
                        response = await send_task
                    except TimeoutError:
                        raise
                    except Exception as err:
                        replacement = await self._async_recover_model(
                            record.model_id, err, session_error
                        )
                        # Replaying a failed turn could repeat a device action.
                        raise HomeAssistantError(
                            f"The GitHub Copilot model '{record.model_id}' is no "
                            f"longer available. Model '{replacement}' will be used "
                            "for your next message. This request was not retried; "
                            "any actions already completed have not been undone."
                        ) from err
                completed = True
                if (
                    response is not None
                    and isinstance(response.data, AssistantMessageData)
                    and response.data.content
                    and not streamed_content
                ):
                    async for _ in chat_log.async_add_delta_content_stream(
                        agent_id,
                        _single_delta(response.data.content),
                    ):
                        pass
                record.last_used = time.monotonic()
                if streamed_content or (
                    response is not None
                    and isinstance(response.data, AssistantMessageData)
                    and response.data.content
                ):
                    self._quota_exceeded = False
                    ir.async_delete_issue(
                        self.hass,
                        DOMAIN,
                        f"{ISSUE_QUOTA_EXCEEDED}_{self.entry.entry_id}",
                    )
            except TimeoutError as err:
                self._raise_if_quota_exceeded(err, session_error)
                raise HomeAssistantError(
                    "GitHub Copilot did not respond before the request timed out."
                ) from err
            finally:
                active = False
                record.current_handler = None
                unsubscribe()
                for item in pending_tools:
                    if not item.result.done():
                        item.result.set_result(
                            CopilotToolResult(
                                text_result_for_llm="The request was cancelled.",
                                result_type="rejected",
                            )
                        )
                if not completed:
                    try:
                        await asyncio.shield(record.session.abort())
                    except Exception as err:
                        _LOGGER.warning(
                            "Unable to abort a failed Copilot turn (%s)",
                            type(err).__name__,
                        )
                    finally:
                        if not send_task.done():
                            send_task.cancel()
                        await asyncio.gather(send_task, return_exceptions=True)

    async def async_stop(self) -> None:
        """Disconnect all in-memory conversation sessions."""
        self._closed = True
        async with self._session_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        results = await asyncio.gather(
            *(record.session.disconnect() for record in sessions),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                _LOGGER.warning("Unable to disconnect a Copilot session: %s", result)

    async def _async_get_session(
        self, key: str, api: Any | None, instructions: str
    ) -> _SessionRecord:
        """Reuse an active session or create a bounded new one."""
        async with self._session_lock:
            if self._closed:
                raise HomeAssistantError("GitHub Copilot is shutting down.")
            await self._async_expire_sessions()
            if record := self._sessions.get(key):
                return record
            if len(self._sessions) >= MAX_ACTIVE_SESSIONS:
                raise HomeAssistantError(
                    "GitHub Copilot has reached its active conversation limit."
                )

            tools: dict[str, str] = {}

            async def handler(invocation: ToolInvocation) -> CopilotToolResult:
                record = self._sessions.get(key)
                if record is None:
                    return CopilotToolResult(
                        text_result_for_llm="This conversation has expired.",
                        result_type="rejected",
                    )
                current = record.current_handler
                if current is None:
                    return CopilotToolResult(
                        text_result_for_llm="No active Home Assistant request.",
                        result_type="rejected",
                    )
                return await current(invocation)

            sdk_tools = self._build_tools(api, handler, tools)
            prompt_context = {"instructions": instructions}
            model_id = await self.async_resolve_model()
            try:
                session = await self._async_create_sdk_session(
                    model_id, sdk_tools, prompt_context
                )
            except Exception as err:
                model_id = await self._async_recover_model(model_id, err)
                try:
                    session = await self._async_create_sdk_session(
                        model_id, sdk_tools, prompt_context
                    )
                except Exception as retry_err:
                    self._raise_sdk_error(model_id, retry_err)
            record = _SessionRecord(
                session=session, model_id=model_id, prompt_context=prompt_context
            )
            self._sessions[key] = record
            if ha_session := current_session.get():
                ha_session.async_on_cleanup(
                    lambda: self.hass.async_create_task(
                        self._async_release_session(key, record),
                        "GitHub Copilot chat cleanup",
                    )
                )
            return record

    async def _async_release_session(self, key: str, record: _SessionRecord) -> None:
        """Release this exact SDK session when HA expires its chat."""
        async with self._session_lock, record.lock:
            if self._sessions.get(key) is not record:
                return
            self._sessions.pop(key)
            try:
                await record.session.disconnect()
            except Exception as err:
                _LOGGER.warning(
                    "Unable to disconnect expired Copilot chat (%s)", type(err).__name__
                )

    async def _async_update_session_model(self, record: _SessionRecord) -> None:
        """Recheck every turn, switching in place while holding the turn lock."""
        model_id = await self.async_resolve_model()
        if model_id == record.model_id:
            return
        try:
            await record.session.set_model(model_id)
        except Exception as err:
            model_id = await self._async_recover_model(model_id, err)
            try:
                await record.session.set_model(model_id)
            except Exception as retry_err:
                self._raise_sdk_error(model_id, retry_err)
        record.model_id = model_id

    async def _async_recover_model(
        self,
        model_id: str,
        error: Exception,
        event: SessionErrorData | None = None,
    ) -> str:
        """Refresh discovery on SDK failure, without treating quota as model loss."""
        self._raise_if_quota_exceeded(error, event)
        replacement = await self.async_resolve_model(
            force_refresh=True,
            unavailable_model=(
                model_id if is_model_unavailable_error(error, event) else None
            ),
        )
        if replacement == model_id or (
            not is_model_unavailable_error(error, event)
            and model_id in self._available_model_ids
        ):
            self._raise_sdk_error(model_id, error)
        return replacement

    def _raise_sdk_error(self, model_id: str, error: Exception) -> NoReturn:
        """Expose an SDK failure through HA without leaking its raw payload."""
        self._raise_if_quota_exceeded(error)
        _LOGGER.warning(
            "GitHub Copilot request failed using model '%s' (%s)",
            model_id,
            type(error).__name__,
        )
        raise HomeAssistantError(
            f"GitHub Copilot could not complete the request using model '{model_id}'. "
            "Check your Copilot access and connection, then try again."
        ) from error

    def _raise_if_quota_exceeded(
        self, error: Exception, event: SessionErrorData | None = None
    ) -> None:
        """Surface exhausted Copilot quota as an error and a persistent Repair."""
        if not is_quota_error(error, event):
            return
        self.report_quota_exceeded()
        raise HomeAssistantError(
            "GitHub Copilot quota exceeded. Check your AI Credits usage and limits "
            "in GitHub, or contact your organization administrator. See Settings > "
            "Repairs for details."
        ) from error

    def report_quota_exceeded(self) -> None:
        """Record quota failures from conversation or model-discovery requests."""
        if not self._quota_exceeded:
            _LOGGER.warning("GitHub Copilot quota exceeded; requests are blocked")
        self._quota_exceeded = True
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_QUOTA_EXCEEDED}_{self.entry.entry_id}",
            is_fixable=False,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_QUOTA_EXCEEDED,
        )

    async def _async_create_sdk_session(
        self,
        model_id: str,
        sdk_tools: list[CopilotTool],
        prompt_context: dict[str, str],
    ) -> CopilotSession:
        """Create an isolated SDK session with only the provided HA tools."""
        # PATs have no known lifetime; use the SDK's static-token path rather than
        # inventing expiresIn for its expiring-token callback.
        github_token = (
            await self._access_token_getter()
            if self.entry.data.get(CONF_AUTH_METHOD) == AUTH_METHOD_PAT
            else None
        )
        return await self.client.create_session(
            model=model_id,
            system_message={
                "mode": "customize",
                "content": DEFAULT_SYSTEM_PROMPT,
                "sections": {
                    **{
                        section: {"action": "remove"}
                        for section in SYSTEM_MESSAGE_SECTIONS
                        if section not in ("safety", "custom_instructions")
                    },
                },
            },
            organization_custom_instructions=prompt_context["instructions"],
            tools=sdk_tools,
            available_tools=ToolSet().add_custom("*"),
            github_token=github_token,
            github_token_provider=None if github_token else self.token_provider,
            on_permission_request=lambda request, invocation: PermissionDecisionReject(
                feedback="Only explicitly exposed Home Assistant tools are permitted."
            ),
            streaming=True,
            enable_session_store=False,
            enable_skills=False,
            enable_file_hooks=False,
            enable_config_discovery=False,
            enable_session_telemetry=False,
            enable_mcp_apps=False,
            enable_host_git_operations=False,
            manage_schedule_enabled=False,
            coauthor_enabled=False,
            skip_custom_instructions=True,
        )

    async def async_check_model(self) -> None:
        """Verify the configured model at startup so Repairs is current."""
        try:
            await self.async_resolve_model()
        except HomeAssistantError:
            pass
        except Exception as err:  # noqa: BLE001 - startup check is best effort
            _LOGGER.warning("Unable to verify the GitHub Copilot model: %s", err)

    async def async_resolve_model(
        self, *, force_refresh: bool = False, unavailable_model: str | None = None
    ) -> str:
        """Return the configured model or a same-family fallback."""
        configured = self.entry.options[CONF_MODEL]
        try:
            models = await self._model_lister(force_refresh=force_refresh)
        except ReauthenticationRequired as err:
            await async_start_reauth_flow(self.hass, self.entry)
            raise HomeAssistantError(
                "GitHub sign-in has expired. Reauthenticate in Home Assistant."
            ) from err
        except Exception as err:
            self._raise_if_quota_exceeded(err)
            _LOGGER.warning(
                "Unable to list GitHub Copilot models (%s)", type(err).__name__
            )
            raise HomeAssistantError(
                f"Could not check availability of GitHub Copilot model '{configured}'. "
                "Check your Copilot access and connection, then try again."
            ) from err

        now = time.monotonic()
        if unavailable_model is not None:
            self._rejected_models[unavailable_model] = now + MODEL_CACHE_SECONDS
        self._rejected_models = {
            model_id: expires_at
            for model_id, expires_at in self._rejected_models.items()
            if expires_at > now
        }
        # Discovery can briefly advertise a model the inference API just rejected.
        models = [model for model in models if model.id not in self._rejected_models]
        self._available_model_ids = {model.id for model in selectable_models(models)}

        resolution = resolve_model(
            configured,
            models,
            max_multiplier=model_budget(
                configured, self.entry.data.get(CONF_MODEL_BILLING, {})
            ),
        )
        self._apply_model_resolution(resolution)
        if resolution.model_id is None:
            raise HomeAssistantError(
                f"The GitHub Copilot model '{configured}' is no longer available "
                "and no similar model within the known billing limit was found. "
                "Choose another model in the "
                "GitHub Copilot integration options."
            )
        return resolution.model_id

    def _apply_model_resolution(self, resolution: ModelResolution) -> None:
        """Log model availability changes and keep Repairs issues in sync."""
        changed = resolution != self._model_resolution
        self._model_resolution = resolution
        self.active_model = resolution.model_id
        fallback_issue = f"{ISSUE_MODEL_FALLBACK}_{self.entry.entry_id}"
        unavailable_issue = f"{ISSUE_MODEL_UNAVAILABLE}_{self.entry.entry_id}"

        if resolution.is_available:
            ir.async_delete_issue(self.hass, DOMAIN, fallback_issue)
            ir.async_delete_issue(self.hass, DOMAIN, unavailable_issue)
            return

        if resolution.is_fallback:
            if changed:
                _LOGGER.warning(
                    "GitHub Copilot model '%s' is unavailable; falling back to "
                    "same-family model '%s' (billing multiplier: %s)",
                    resolution.requested,
                    resolution.model_id,
                    resolution.multiplier
                    if resolution.multiplier is not None
                    else "unknown; usage cost may differ",
                )
            ir.async_delete_issue(self.hass, DOMAIN, unavailable_issue)
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                fallback_issue,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_MODEL_FALLBACK,
                translation_placeholders={
                    "model": resolution.requested,
                    "fallback": str(resolution.model_id),
                    "billing": (
                        f"{resolution.multiplier:g}x"
                        if resolution.multiplier is not None
                        else "unknown; usage cost may differ"
                    ),
                },
            )
            return

        if changed:
            _LOGGER.error(
                "GitHub Copilot model '%s' is unavailable and no same-family "
                "model was found",
                resolution.requested,
            )
        ir.async_delete_issue(self.hass, DOMAIN, fallback_issue)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            unavailable_issue,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_MODEL_UNAVAILABLE,
            translation_placeholders={"model": resolution.requested},
        )

    async def _async_expire_sessions(self) -> None:
        """Remove idle, unlocked sessions without evicting active turns."""
        now = time.monotonic()
        expired = [
            key
            for key, record in self._sessions.items()
            if not record.lock.locked()
            and now - record.last_used >= SESSION_IDLE_SECONDS
        ]
        for key in expired:
            record = self._sessions.pop(key)
            await record.session.disconnect()

    @staticmethod
    def _session_key(conversation_id: str, context: Any) -> str:
        """Hash conversation and HA identity without persisting user identifiers."""
        if not conversation_id:
            return uuid.uuid4().hex
        identity = "\0".join(
            (
                conversation_id,
                str(getattr(getattr(context, "context", None), "user_id", "")),
                str(getattr(context, "device_id", None) or ""),
            )
        )
        return hashlib.sha256(identity.encode()).hexdigest()

    @staticmethod
    def _system_instructions(chat_log: ChatLog) -> str:
        """Return the current HA prompt and Assist API guidance."""
        instructions = [
            item.content
            for item in chat_log.content
            if isinstance(item, conversation.SystemContent) and item.content
        ]
        if chat_log.llm_api is not None and chat_log.llm_api.api_prompt:
            instructions.append(chat_log.llm_api.api_prompt)
        return "\n\n".join(instructions)

    def _build_tools(
        self, api: Any | None, handler: Any, names: dict[str, str]
    ) -> list[CopilotTool]:
        """Convert the selected Assist API tools to SDK tool definitions."""
        if api is None:
            return []
        tools: list[CopilotTool] = []
        for ha_tool in api.tools:
            try:
                schema = convert_tool_schema(
                    ha_tool.parameters, custom_serializer=api.custom_serializer
                )
                if not isinstance(schema, dict) or schema.get("type") != "object":
                    raise ValueError("Tool parameters must be an object schema.")
            except (TypeError, ValueError) as err:
                _LOGGER.warning(
                    "Unable to convert Home Assistant tool schema for %s (%s)",
                    ha_tool.name,
                    type(err).__name__,
                )
                raise HomeAssistantError(
                    "Could not convert the Home Assistant tool schema for "
                    f"'{ha_tool.name}'."
                ) from err
            sdk_name = f"ha__{ha_tool.name}"
            names[sdk_name] = ha_tool.name
            tools.append(
                CopilotTool(
                    name=sdk_name,
                    description=ha_tool.description or ha_tool.name,
                    parameters=schema,
                    handler=handler,
                    skip_permission=True,
                )
            )
        return tools


async def _single_delta(content: str):
    """Yield a complete assistant message as one HA stream delta."""
    yield {"role": "assistant", "content": content}
