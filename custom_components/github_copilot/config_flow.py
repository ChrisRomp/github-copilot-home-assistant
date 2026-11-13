"""Home Assistant configuration and options flows."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import CONF_LLM_HASS_API, CONF_PROMPT
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api_errors import is_quota_error
from .auth import (
    CredentialStore,
    OAuthError,
    ReauthenticationRequired,
    async_get_account_id,
    async_start_reauth_flow,
    poll_for_device_token,
    request_device_code,
    validate_pat,
)
from .const import (
    AUTH_METHOD_PAT,
    CONF_ACCESS_TOKEN,
    CONF_ACCESS_TOKEN_EXPIRES_AT,
    CONF_ACCOUNT_ID,
    CONF_AI_TASK_MODEL,
    CONF_AI_TASK_MODEL_BILLING,
    CONF_AI_TASK_TIMEOUT,
    CONF_AUTH_METHOD,
    CONF_ENABLE_DEVICE_CONTROL,
    CONF_ENABLE_HOME_ASSISTANT_API,
    CONF_MODEL,
    CONF_MODEL_BILLING,
    CONF_REFRESH_TOKEN,
    CONF_REFRESH_TOKEN_EXPIRES_AT,
    DEFAULT_AI_TASK_TIMEOUT,
    DOMAIN,
    NAME,
    OAUTH_CLIENT_ID,
    OAUTH_DEVICE_VERIFICATION_URL,
)
from .llm_api import home_assistant_api_supported, selected_llm_apis
from .models import (
    find_fallback_model,
    model_billing,
    model_budget,
    model_multiplier,
    selectable_models,
)
from .runtime import async_probe_models

_LOGGER = logging.getLogger(__name__)
_AUTH_DATA_KEYS = (
    CONF_AUTH_METHOD,
    CONF_ACCESS_TOKEN,
    CONF_ACCESS_TOKEN_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    CONF_REFRESH_TOKEN_EXPIRES_AT,
)


class GitHubCopilotConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up GitHub Copilot credentials and conversation options."""

    VERSION = 1
    MINOR_VERSION = 2

    def __init__(self) -> None:
        self._device: dict[str, Any] | None = None
        self._credentials: dict[str, Any] = {}
        self._models: list[Any] = []
        self._reauth_entry: ConfigEntry | None = None
        self._oauth_task: asyncio.Task[dict[str, Any]] | None = None
        self._oauth_lock = asyncio.Lock()
        self._removed = False

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Choose the GitHub sign-in method."""
        return self.async_show_menu(step_id="user", menu_options=["oauth", "pat"])

    async def async_step_oauth(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Sign in with GitHub's OAuth device authorization flow."""
        if not OAUTH_CLIENT_ID:
            return self.async_abort(reason="oauth_not_configured")

        async with self._oauth_lock:
            if self._removed:
                return self.async_abort(reason="oauth_failed")
            if self._oauth_task is None:
                session = async_get_clientsession(self.hass)
                try:
                    device = await request_device_code(session, OAUTH_CLIENT_ID)
                except OAuthError as err:
                    _LOGGER.warning("Unable to start GitHub OAuth device flow: %s", err)
                    return self.async_abort(reason="oauth_unavailable")
                if self._removed:
                    return self.async_abort(reason="oauth_failed")
                self._device = device
                self._oauth_task = self.hass.async_create_task(
                    poll_for_device_token(
                        session,
                        client_id=OAUTH_CLIENT_ID,
                        device_code=device["device_code"],
                        expires_in=int(device["expires_in"]),
                        interval=int(device.get("interval", 5)),
                    ),
                    "GitHub Copilot device authorization",
                )
            if self._oauth_task.done():
                return self.async_show_progress_done(next_step_id="oauth_complete")
            assert self._device is not None
            return self.async_show_progress(
                step_id="oauth",
                progress_action="authorize",
                progress_task=self._oauth_task,
                description_placeholders={
                    "verification_uri": self._device.get(
                        "verification_uri", OAUTH_DEVICE_VERIFICATION_URL
                    ),
                    "user_code": self._device["user_code"],
                },
            )

    async def async_step_oauth_complete(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Consume authorization only while the flow is still active."""
        if self._removed or self._oauth_task is None:
            return self.async_abort(reason="oauth_failed")
        if not self._oauth_task.done():
            return await self.async_step_oauth()
        try:
            self._credentials = self._oauth_task.result()
        except (OAuthError, asyncio.CancelledError) as err:
            _LOGGER.warning(
                "GitHub OAuth device flow did not complete (%s)", type(err).__name__
            )
            return self.async_abort(reason="oauth_failed")
        return await self._async_authentication_succeeded()

    @callback
    def async_remove(self) -> None:
        """Stop polling and discard secrets when HA closes the flow."""
        self._removed = True
        if self._oauth_task is not None:
            if not self._oauth_task.done():
                self._oauth_task.cancel()
            elif not self._oauth_task.cancelled():
                self._oauth_task.exception()
            self._oauth_task = None
        self._device = None
        self._credentials = {}
        super().async_remove()

    async def async_step_pat(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Set up using a fine-grained personal access token."""
        if user_input is not None:
            try:
                token = validate_pat(user_input["token"])
            except ValueError:
                return self.async_show_form(
                    step_id="pat",
                    data_schema=self._pat_schema(),
                    errors={"base": "invalid_pat"},
                )
            self._credentials = {
                CONF_AUTH_METHOD: AUTH_METHOD_PAT,
                CONF_ACCESS_TOKEN: token,
            }
            return await self._async_authentication_succeeded()

        return self.async_show_form(step_id="pat", data_schema=self._pat_schema())

    async def async_step_model(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Choose a Copilot model and optional conversation settings."""
        if self._removed:
            return self.async_abort(reason="oauth_failed")
        if user_input is not None:
            if (
                user_input.get(CONF_ENABLE_HOME_ASSISTANT_API)
                and not home_assistant_api_supported()
            ):
                return self.async_show_form(
                    step_id="model",
                    data_schema=_options_schema(self._model_options(), user_input),
                    errors={
                        CONF_ENABLE_HOME_ASSISTANT_API: "home_assistant_api_unavailable"
                    },
                )
            self._credentials[CONF_MODEL_BILLING] = model_billing(
                user_input[CONF_MODEL], self._models
            )
            task_model = user_input.get(CONF_AI_TASK_MODEL, user_input[CONF_MODEL])
            self._credentials[CONF_AI_TASK_MODEL_BILLING] = model_billing(
                task_model, self._models
            )
            device_control_enabled = user_input[CONF_ENABLE_DEVICE_CONTROL]
            options = {
                CONF_MODEL: user_input[CONF_MODEL],
                CONF_AI_TASK_MODEL: task_model,
                CONF_AI_TASK_TIMEOUT: user_input.get(
                    CONF_AI_TASK_TIMEOUT, DEFAULT_AI_TASK_TIMEOUT
                ),
                CONF_ENABLE_DEVICE_CONTROL: device_control_enabled,
                CONF_PROMPT: user_input.get(CONF_PROMPT, ""),
            }
            if CONF_ENABLE_HOME_ASSISTANT_API in user_input:
                options[CONF_ENABLE_HOME_ASSISTANT_API] = user_input[
                    CONF_ENABLE_HOME_ASSISTANT_API
                ]
            options[CONF_LLM_HASS_API] = selected_llm_apis(options)
            return self.async_create_entry(
                title=NAME,
                data=self._credentials,
                options=options,
            )

        return self.async_show_form(
            step_id="model",
            data_schema=_options_schema(self._model_options(), {}),
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> dict[str, Any]:
        """Start reauthentication for an expired config entry."""
        entry_id = self.context.get("entry_id")
        self._reauth_entry = self.hass.config_entries.async_get_entry(entry_id)
        if self._reauth_entry is None:
            return self.async_abort(reason="reauth_entry_missing")
        if not self._reauth_entry.data.get(CONF_ACCOUNT_ID):
            try:
                await CredentialStore(
                    self.hass, self._reauth_entry, OAUTH_CLIENT_ID
                ).async_ensure_account_id()
            except (OAuthError, ReauthenticationRequired):
                return self.async_abort(reason="account_identity_unknown")
        return self.async_show_menu(step_id="reauth", menu_options=["oauth", "pat"])

    async def _async_authentication_succeeded(self) -> dict[str, Any]:
        """Validate Copilot access and continue to model selection."""
        try:
            account_id = await async_get_account_id(
                async_get_clientsession(self.hass), self._credentials[CONF_ACCESS_TOKEN]
            )
            if self._removed:
                return self.async_abort(reason="oauth_failed")
            if (
                self._reauth_entry is not None
                and self._reauth_entry.data.get(CONF_ACCOUNT_ID) != account_id
            ):
                return self.async_abort(reason="account_mismatch")
            self._credentials[CONF_ACCOUNT_ID] = account_id
            self._models = selectable_models(
                await async_probe_models(
                    self._credentials[CONF_ACCESS_TOKEN],
                    flow_id=uuid.uuid4().hex,
                    hass=self.hass,
                )
            )
        except (OAuthError, ReauthenticationRequired):
            return self.async_abort(reason="cannot_connect")
        except (OSError, RuntimeError) as err:
            if is_quota_error(err):
                return self.async_abort(reason="quota_exceeded")
            _LOGGER.warning(
                "Unable to validate GitHub credentials or list Copilot models"
            )
            return self.async_abort(reason="cannot_connect")

        if self._removed:
            return self.async_abort(reason="oauth_failed")
        if not self._models:
            return self.async_abort(reason="no_models")
        if self._reauth_entry is not None:
            data = dict(self._reauth_entry.data)
            for key in _AUTH_DATA_KEYS:
                data.pop(key, None)
            return self.async_update_reload_and_abort(
                self._reauth_entry,
                data={**data, **self._credentials},
                reason="reauth_successful",
            )
        return await self.async_step_model()

    def _model_options(self) -> list[SelectOptionDict]:
        """Convert SDK model records to Home Assistant selector options."""
        return [
            SelectOptionDict(
                label=_model_label(model),
                value=model.id,
            )
            for model in self._models
        ]

    @staticmethod
    def _pat_schema() -> vol.Schema:
        """Return a password-masked fine-grained PAT field."""
        return vol.Schema(
            {
                vol.Required("token"): TextSelector(
                    TextSelectorConfig(
                        type=TextSelectorType.PASSWORD,
                        autocomplete="off",
                    )
                )
            }
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Create the options flow."""
        return GitHubCopilotOptionsFlow(config_entry)


class GitHubCopilotOptionsFlow(OptionsFlow):
    """Manage models, instructions, and Home Assistant tool access."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry
        self._models: list[Any] = []

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Show editable integration options."""
        if user_input is not None:
            options = dict(user_input)
            if (
                options.get(CONF_ENABLE_HOME_ASSISTANT_API)
                and not home_assistant_api_supported()
            ):
                return self.async_show_form(
                    step_id="init",
                    data_schema=_options_schema(self._model_options(), options),
                    errors={
                        CONF_ENABLE_HOME_ASSISTANT_API: "home_assistant_api_unavailable"
                    },
                )
            options.setdefault(
                CONF_AI_TASK_MODEL,
                self._entry.options.get(CONF_AI_TASK_MODEL, options[CONF_MODEL]),
            )
            options.setdefault(CONF_AI_TASK_TIMEOUT, DEFAULT_AI_TASK_TIMEOUT)
            device_control_enabled = options[CONF_ENABLE_DEVICE_CONTROL]
            options[CONF_ENABLE_DEVICE_CONTROL] = device_control_enabled
            options[CONF_LLM_HASS_API] = selected_llm_apis(options)
            selected_model = options[CONF_MODEL]
            billing = model_billing(selected_model, self._models)
            saved_billing = self._entry.data.get(CONF_MODEL_BILLING, {})
            if (
                selected_model == self._entry.options.get(CONF_MODEL)
                and billing["multiplier"] is None
                and model_budget(selected_model, saved_billing) is not None
            ):
                billing = saved_billing
            self.hass.config_entries.async_update_entry(
                self._entry,
                data={
                    **self._entry.data,
                    CONF_MODEL_BILLING: billing,
                    CONF_AI_TASK_MODEL_BILLING: _billing_snapshot(
                        options[CONF_AI_TASK_MODEL],
                        self._models,
                        self._entry.data.get(CONF_AI_TASK_MODEL_BILLING, {}),
                    ),
                },
            )
            return self.async_create_entry(title="", data=options)

        try:
            self._models = selectable_models(await self._async_list_models())
        except ReauthenticationRequired:
            await async_start_reauth_flow(self.hass, self._entry)
            return self.async_abort(reason="reauth_required")
        except OAuthError:
            return self.async_abort(reason="cannot_connect")
        except (OSError, RuntimeError) as err:
            if is_quota_error(err):
                return self.async_abort(reason="quota_exceeded")
            _LOGGER.warning("Unable to load Copilot models for options")
            return self.async_abort(reason="cannot_connect")

        if not self._models:
            return self.async_abort(reason="no_models")

        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        saved_model = self._entry.options.get(CONF_MODEL)
        model_ids = [model.id for model in self._models]
        if saved_model in model_ids:
            default_model = saved_model
        elif saved_model and (
            fallback := find_fallback_model(
                saved_model,
                self._models,
                max_multiplier=model_budget(
                    saved_model, self._entry.data.get(CONF_MODEL_BILLING, {})
                ),
            )
        ):
            default_model = fallback
            errors["base"] = "model_unavailable_fallback"
            placeholders = {"model": saved_model, "fallback": fallback}
        else:
            default_model = model_ids[0]
            if saved_model:
                errors["base"] = "model_unavailable"
                placeholders = {"model": saved_model}

        task_model = self._entry.options.get(CONF_AI_TASK_MODEL, saved_model)
        task_default = task_model
        if task_model not in model_ids:
            task_default = (
                find_fallback_model(
                    task_model,
                    self._models,
                    max_multiplier=model_budget(
                        task_model, self._entry.data.get(CONF_AI_TASK_MODEL_BILLING, {})
                    ),
                )
                if task_model
                else None
            )
            task_default = task_default or model_ids[0]
            errors[CONF_AI_TASK_MODEL] = "task_model_unavailable"

        return self.async_show_form(
            step_id="init",
            errors=errors,
            description_placeholders=placeholders,
            data_schema=_options_schema(
                self._model_options(),
                {
                    **self._entry.options,
                    CONF_MODEL: default_model,
                    CONF_AI_TASK_MODEL: task_default,
                },
            ),
        )

    async def _async_list_models(self) -> list[Any]:
        """Refresh models through the loaded runtime, or a temporary client."""
        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        if runtime is not None and runtime.is_started:
            return await runtime.async_list_models(force_refresh=True)
        token = await CredentialStore(
            self.hass, self._entry, OAUTH_CLIENT_ID
        ).async_get_access_token()
        return await async_probe_models(token, flow_id=uuid.uuid4().hex, hass=self.hass)

    def _model_options(self) -> list[SelectOptionDict]:
        """Convert SDK models to HA selector options."""
        return [
            SelectOptionDict(label=_model_label(model), value=model.id)
            for model in self._models
        ]


def _options_schema(
    model_options: list[SelectOptionDict], options: Mapping[str, Any]
) -> vol.Schema:
    """Build the conversation settings form for setup and later changes."""
    fields = {
        vol.Required(
            CONF_MODEL, default=options.get(CONF_MODEL, vol.UNDEFINED)
        ): SelectSelector(
            SelectSelectorConfig(options=model_options, custom_value=False)
        ),
        vol.Optional(
            CONF_AI_TASK_MODEL,
            **(
                {"default": options[CONF_AI_TASK_MODEL]}
                if CONF_AI_TASK_MODEL in options
                else {}
            ),
        ): SelectSelector(
            SelectSelectorConfig(options=model_options, custom_value=False)
        ),
        vol.Required(
            CONF_AI_TASK_TIMEOUT,
            default=options.get(CONF_AI_TASK_TIMEOUT, DEFAULT_AI_TASK_TIMEOUT),
        ): vol.All(vol.Coerce(int), vol.Range(min=1)),
        vol.Required(
            CONF_ENABLE_DEVICE_CONTROL,
            default=bool(
                options.get(CONF_ENABLE_DEVICE_CONTROL, options.get(CONF_LLM_HASS_API))
            ),
        ): BooleanSelector(),
    }
    if home_assistant_api_supported() or options.get(CONF_ENABLE_HOME_ASSISTANT_API):
        fields[
            vol.Required(
                CONF_ENABLE_HOME_ASSISTANT_API,
                default=bool(options.get(CONF_ENABLE_HOME_ASSISTANT_API, False)),
            )
        ] = BooleanSelector()
    fields[
        vol.Optional(
            CONF_PROMPT,
            description={"suggested_value": options.get(CONF_PROMPT, "")},
        )
    ] = TextSelector()
    return vol.Schema(fields)


def _model_label(model: Any) -> str:
    """Show known billing alongside the account's model choices."""
    label = getattr(model, "name", None) or model.id
    multiplier = model_multiplier(model)
    return f"{label} ({multiplier:g}x)" if multiplier is not None else label


def _billing_snapshot(
    model: str, models: list[Any], previous: dict[str, Any]
) -> dict[str, Any]:
    """Keep known billing only when it describes the unchanged model."""
    billing = model_billing(model, models)
    if billing["multiplier"] is None and model_budget(model, previous) is not None:
        return dict(previous)
    return billing
