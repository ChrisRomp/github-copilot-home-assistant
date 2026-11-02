# Using GitHub Copilot

For installation and initial setup, see the [README](../README.md#installation).

## Settings

During initial setup, choose a model and optionally enable device control or
set **Additional instructions** on the same screen. To change these settings
later, open **Settings > Devices & services > GitHub Copilot > Configure**.

Device control is disabled by default and is limited to entities exposed to
Home Assistant Assist. The integration does not give the Copilot runtime a
general Home Assistant REST token. Additional instructions are literal text;
Home Assistant templates are not expanded.

## Model selection and fallback

Each time you open the configuration dialog, the integration asks Copilot for
the current list of models. Models disabled by account or organization policy
are omitted.

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
- If no eligible model in the same family is available, conversations fail with
  an error that names the missing model, and Repairs asks you to choose another.

When the configuration dialog opens, it never preselects an unavailable model.
It preselects the fallback, if there is one, or the first available model, and
explains what changed.

If a model disappears during a request, the error names that model and, when
available, its replacement for the next message. The failed request is not
automatically replayed, since it may already have produced output or controlled
a device. If discovery still lists a model that the API explicitly rejected,
that model is excluded from conversation use for up to an hour before it is
considered again.

## Conversation data and privacy

Prompts, the information needed to answer them, and tool results are sent to
GitHub Copilot. Model inference runs in GitHub's cloud service, not on your
Home Assistant device.

Conversations have a five-minute idle limit and are isolated by conversation,
user, and originating device. The SDK can write session files while a chat is
active. The integration deletes its SDK sessions when HA releases a chat, when
an idle session is cleaned up, and during normal unload or shutdown.

An abrupt exit or failed cleanup can leave files behind, and backups taken
while a chat is active may include conversation data. A Home Assistant restart
starts fresh conversations. Credentials and settings remain in Home Assistant's
config-entry storage; treat that storage and Home Assistant backups as sensitive.

Device control only uses the Home Assistant Assist API selected for the request.
Other SDK permission requests are rejected. Prompt-injection defenses cannot
eliminate all risk, and the integration adds no extra action-confirmation step.
For the implementation details, see
[instructions and tool permissions](development.md#instructions-and-tool-permissions).

## Updates and existing installations

On Home Assistant 2026.11 or newer, updates appear under **Settings > Updates**.
On 2026.10, use HACS to update. Restart Home Assistant after updating an
already-loaded integration.

When upgrading Home Assistant from HACS to Marketplace, Home Assistant carries
over the custom repository. Keep the existing GitHub Copilot configuration;
there is no need to remove and re-add the integration.

The conversation entity is named **GitHub Copilot**. New installations normally
use `conversation.github_copilot` (HA adds a suffix if that ID is already taken).
Existing entity IDs and user-defined names are preserved on upgrade. To change
an older generated entity ID, open the entity's settings in HA and review any
Assist pipelines, scripts, or automations that reference it.

## Troubleshooting

### Quota errors

If Copilot reports exhausted usage quota, the integration returns an error,
logs a warning, and creates a notice in **Settings > Repairs**. Check your AI
Credits usage and limits in GitHub, or contact your organization administrator.
The notice persists until a conversation response succeeds or the integration
entry is removed. Quota failures do not trigger model fallback or automatic
retries. Temporary rate limiting alone is not treated as exhausted quota.

### Setup failures

Home Assistant installs the pinned GitHub Copilot SDK, which downloads its
native runtime. Linux ARM64 and x86-64 glibc/musl release assets are available;
32-bit runtime support is not.

Network/download, checksum, storage, and unsupported-platform failures are
reported during setup. Check outbound access to GitHub and whether Home
Assistant can write to its configuration directory. The runtime cache survives
Core container replacement; see [runtime storage](development.md#runtime-storage).

### Missing icon

The integration bundles its icon in `brand/` for Home Assistant to show after
installation. Legacy HACS and the pre-install Marketplace listing may still
show a placeholder for this unlisted repository. This does not affect setup
or operation.

### Diagnostics and bug reports

Use **Download diagnostics** from the integration's menu in Home Assistant for
authentication-method, model, device-control, and runtime status. The diagnostic
payload does not include access tokens, refresh tokens, prompts, or conversations.

[Open an issue](https://github.com/ChrisRomp/github-copilot-home-assistant/issues) for bugs.
Never include tokens or unredacted conversations in a report.
