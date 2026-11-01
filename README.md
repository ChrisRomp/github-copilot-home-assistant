# GitHub Copilot for Home Assistant

![GitHub Copilot for Home Assistant icon](custom_components/github_copilot/brand/icon.png)

A custom Home Assistant conversation integration powered by GitHub Copilot.
It provides a conversation agent that Assist can use for typed or spoken
interactions, not a complete voice pipeline.

**This is an unofficial, community-built project, not affiliated with GitHub or
the Home Assistant project.**

The integration and GitHub Copilot SDK run in Python inside Home Assistant; the
SDK starts its pinned Copilot runtime as a child process. Model inference
happens through GitHub's cloud service, not on the Home Assistant device.

## Features

- Conversation agent with streamed responses and multi-turn context.
- Optional control of entities exposed to Home Assistant Assist. It is disabled
  by default.
- OAuth device-code sign-in in the Home Assistant UI, with expiring access
  tokens that renew automatically.
- Fine-grained personal access token (PAT) fallback.
- Model selection from the models currently available to your Copilot account,
  with automatic same-family fallback when the selected model disappears.

AI Tasks, remote runtime hosting, and fallback to a local model, such as one
served by Ollama, are not implemented.
Existing Home Assistant speech-to-text and text-to-speech integrations continue
to provide voice input and output.

## Model selection

You choose a model during setup and can change it under **Settings > Devices &
services > GitHub Copilot > Configure**. Each time you open that dialog, the
integration asks Copilot for the current list of models. Models disabled by
account or organization policy are omitted.

The integration also checks the selected model when it starts and before every
conversation turn, including ongoing chats. It reuses the list for up to an hour
and refreshes it immediately after a model/request failure. If the selected
model is no longer available:

- It uses the closest available version in the same family. Families are inferred
  by removing version numbers from model IDs, so `claude-sonnet-4.5` can fall
  back to `claude-sonnet-5` and `gpt-6-sol` to `gpt-6.1-sol`. Variants such as
  `gpt-5.6-sol-fast` count as a different family. A fallback is logged as a
  warning and shown in **Settings > Repairs**. Your saved choice isn't changed,
  so the original model is used again if it returns. Existing sessions switch
  models without discarding conversation history.
- Known billing multipliers appear in the model picker and fallback Repair.
  Automatic fallback does not exceed the selected model's last known multiplier.
  If that baseline is unknown, the Repair warns that cost may differ; if the
  baseline is known but a replacement's cost is unknown, it is not auto-selected.
- If no model in the same family is available, conversations fail with an error
  that names the missing model, and Repairs asks you to choose another one.

When the configuration dialog opens, it never preselects an unavailable model.
It preselects the fallback, if there is one, or the first available model, and
explains what changed.

If a model disappears during a request, the error names that model and, when
available, its replacement for the next message. The failed request is not
automatically replayed, since it may already have produced output or controlled
a device.
If discovery still lists a model that the API explicitly rejected, that model
is excluded from conversation use for up to an hour before it is considered
again.

## Quota errors

If Copilot reports exhausted usage quota, the integration returns a clear error,
logs a warning, and creates a notice in **Settings > Repairs**. Check your AI
Credits usage and limits in GitHub, or contact your organization administrator.
The notice persists until a conversation response succeeds (or the integration
entry is removed). Quota failures do not trigger model fallback or automatic
retries. Temporary rate limiting alone is not treated as exhausted quota.

## Requirements and limitations

- A Home Assistant installation with a supported 64-bit OS/runtime and outbound
  access to GitHub.
- A GitHub account with GitHub Copilot access. Organization policies and model
  availability continue to apply.
- Home Assistant installs the pinned
  `github-copilot-sdk` dependency from the integration manifest.
- The SDK's native runtime has Linux ARM64 and x86-64 glibc/musl release assets.
  Local 32-bit runtime support is not available.

Conversation context is held in the running integration and released when HA
expires the chat, with a five-minute idle limit for SDK session reuse. Sessions
are isolated by conversation, user, and originating device. A Home Assistant
restart starts fresh conversations. Credentials
and settings remain in Home Assistant's config-entry storage, which should be
treated as sensitive and may be present in Home Assistant backups.

Prompts, the information needed to answer them, and tool results are sent to
GitHub Copilot. When device control is enabled, the model can request actions
only through the Home Assistant Assist API selected for that request. The
integration does not give the Copilot runtime a general Home Assistant REST
token.

HA's current instructions and entity context replace the SDK's mutable,
host-injected system instructions when they change; they are not repeated in
every user message or accumulated as stale history. Only Assist-exposed
tools are allowed; any other SDK permission request is explicitly rejected.
If an action result cannot be serialized, the integration reports that the
action may already have completed and does not replay it.

The SDK-pinned native runtime is cached under
`<config>/.storage/github_copilot/runtime/<runtime-version>/`. Its installer runs
in an isolated process and performs the SDK's checksum verification. The cache
survives Core container replacement. Network/download, checksum, storage, and
unsupported-platform failures are reported during setup. Removing an integration
entry removes only that entry's runtime state, not this shared versioned cache.

## Installation

In HACS, add [ChrisRomp/ha-copilot](https://github.com/ChrisRomp/ha-copilot) as a
custom repository with category **Integration**, then download **GitHub Copilot**.

For manual installation, copy `custom_components/github_copilot/` into
`<config>/custom_components/github_copilot/`.

Restart Home Assistant, then add **GitHub Copilot** from
**Settings → Devices & services**. Sign in with GitHub or a fine-grained PAT
and choose a model.

The conversation entity is named **GitHub Copilot**. New installations normally
use `conversation.github_copilot` (HA adds a suffix if that ID is already taken).
Existing entity IDs and user-defined names are preserved on upgrade. To change
an older generated entity ID, open the entity's settings in HA and review any
Assist pipelines, scripts, or automations that reference it.

The integration bundles its icon in `brand/`, supported by Home Assistant
2026.3 and newer without a separate Brands-repository submission. Older HA
versions may show a placeholder instead.

## Development setup

Development uses Python 3.14 and Home Assistant 2026.10.4. To run tests and lint:

```sh
python -m pip install -e '.[dev]'
pytest
ruff check .
```

Install HA's conversation requirements listed in its
`components/conversation/manifest.json` to run
`HA_COPILOT_HA_TESTS=1 pytest tests/test_ha_contract.py`. This checks actual HA
schema conversion, tool-result handling, and final speech without contacting
GitHub or controlling devices. With an SDK runtime already cached, run
`HA_COPILOT_NATIVE_TESTS=1 pytest tests/test_sdk_contract.py` to verify prompt
updates through the real runtime against a loopback-only fake model.
The validation workflow runs unit tests, the real HA contract test, and hassfest.

Keep the SDK version pins in `manifest.json` and `pyproject.toml` in sync.
The SDK wheel pins the native runtime version.

## OAuth setup for maintainers

Browser sign-in uses the project's GitHub OAuth App. Its **public Client ID**
is set as `OAUTH_CLIENT_ID` in `custom_components/github_copilot/const.py`.
The device flow uses no client secret or callback URL, so no secret is shipped.

The OAuth App must keep these settings:

- **Device flow** enabled.
- **Expiring user tokens** enabled. GitHub then issues 8-hour access tokens and
  6-month refresh tokens. The integration also requests the `offline_access`
  scope, which asks for expiring tokens even if the app setting is off.
  It requests no repository access.

The integration stores both tokens in the config entry, renews the access
token when roughly an hour remains (as required by the SDK), and saves the new
pair each time without reloading the integration or discarding active chats.
Only options changes reload automatically; completing reauthentication explicitly
reloads once. If GitHub rejects the refresh, for example because the refresh
token expired or the user revoked the app, Home Assistant starts reauthentication. OAuth responses
without a token lifetime are rejected. The `offline_access` scope requests
expiring tokens independently of the app's default setting.

The OAuth dialog polls automatically and cancels polling when closed. GitHub's
immutable account ID is saved at setup and checked during reauthentication.
Existing entries bind to their current account at startup; if old credentials
are already invalid, account identity cannot be safely inferred and the user
must explicitly remove/re-add the entry to choose an account. The same account
can still be used by multiple separately configured conversation agents.

The fallback is a user-owned fine-grained PAT with the **Copilot Requests**
account permission, supplied through the SDK's static-token option. The
integration does not renew PATs or invent an expiry time for them. Classic PATs
are not supported. Never put access tokens in source files, issue reports, or
chat messages.

## License

Licensed under the [Apache License 2.0](LICENSE).
