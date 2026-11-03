# Using GitHub Copilot

For installation, sign-in, a test conversation, and voice assistant setup, see
the [setup guide](setup.md).

## Settings

During initial setup, choose a model and optionally enable device control or
set **Additional instructions** on the same screen. To change these settings
later, open **Settings > Devices & services > GitHub Copilot > Configure**.

Device control is disabled by default and is limited to entities exposed to
Home Assistant Assist. The integration does not give the Copilot runtime a
general Home Assistant REST token. Additional instructions are literal text;
Home Assistant templates are not expanded.

## AI Tasks

Home Assistant's [AI Task documentation](https://www.home-assistant.io/integrations/ai_task/)
is the authoritative reference for task behavior. See its
[Generate data](https://www.home-assistant.io/actions/ai_task.generate_data/) and
[Generate image](https://www.home-assistant.io/actions/ai_task.generate_image/)
references for action parameters. This guide adds Copilot-specific settings,
examples, and limitations.

The integration exposes an AI Task entity alongside the conversation entity.
Use `ai_task.generate_data` to generate text or structured data in an automation
or script. Select the entity explicitly, or choose it as your preferred provider
under **Settings > System > AI** before omitting `entity_id`. This integration
does not change that preference automatically. A voice assistant and device
control are not required to generate text, analyze attachments, or generate images.

The **AI Task model** is independent of the conversation model. Existing entries
initially copy their saved conversation model and billing snapshot once.
**Additional instructions** apply only to conversations; each task supplies its
own instructions. The task timeout defaults to 180 seconds and is configurable.
Assist retains its 60-second timeout.

### Try a text task

Open **Developer tools > Actions**, switch to YAML mode, and paste this action.
Replace the entity ID if yours differs.

```yaml
action: ai_task.generate_data
data:
  entity_id: ai_task.github_copilot
  task_name: recycling reminder
  instructions: >-
    Write a one-sentence reminder to take out the recycling.
    Do not control any devices.
```

Select **Perform action**. The response should contain the reminder in `data`
and a `conversation_id`. This request uses your Copilot allowance.

In a script or automation, add `response_variable: reminder` to the action and
use `{{ reminder.data }}` in a later step. Task instructions can use Home
Assistant templates; the conversation's **Additional instructions** cannot.

### Structured output

Add a `structure` to request typed fields, as in this script or automation action:

```yaml
action: ai_task.generate_data
data:
  entity_id: ai_task.github_copilot
  task_name: indoor comfort
  instructions: >-
    Assess indoor comfort at {{ states('sensor.living_room_temperature') }} degrees
    and {{ states('sensor.living_room_humidity') }} percent humidity.
  structure:
    summary:
      required: true
      selector:
        text:
    comfortable:
      required: true
      selector:
        boolean:
response_variable: comfort
```

The response contains `data` and `conversation_id`. In this example,
`{{ comfort.data.summary }}` is the generated summary. Structured results are
validated against HA's selector schema. Unsupported structures, invalid JSON,
missing required values, and invalid results fail explicitly; the integration
does not substitute unstructured text. Open-ended object selectors are not
supported by the strict output adapter.

Multi-select fields, bounded numbers, and fixed-length arrays such as RGB colors
are supported. Provider schemas use textual hints for uniqueness, numeric
constraints, and array lengths that are not accepted by all models' strict output
APIs. The original HA selectors still validate the returned selections, numeric
bounds, and array lengths.

If a model rejects the structured response schema, the error names the model
and recommends simplifying the structure or selecting another AI Task model.
This is different from HA rejecting an invalid generated result. Integration
warning summaries distinguish `provider_schema_rejected` from
`output_validation_failed`, identify the active model, and include recognized
unsupported schema keywords when available. These summaries omit prompts,
schemas, generated values, and raw provider error payloads. Neither failure
automatically retries the task or switches models.

### Device access and session lifecycle

Enabling **Allow control of Assist-exposed devices** also enables those tools
for data tasks. Disabled means no device tools, not merely an instruction to
avoid them. Tasks use the existing Assist exposure choices and the action's
caller context. No extra confirmation step is added. A task may have completed
device actions before a timeout or output-validation failure; such requests are
never automatically replayed. Caller-supplied custom LLM APIs are not supported.

At most two requests run per entry, with at most one AI Task at a time, so tasks
cannot occupy both conversation slots. Busy requests fail promptly. Use HA's
queued automation/script mode when that caller should wait rather than overlap;
there is no integration-owned queue.

Each task uses a fresh SDK session that is deleted on completion or failure.
HA still owns its automation traces, chat-log retention, and input media.
Repeated task names do not share SDK history.

Session disposal has its own cleanup deadlines; a response completed within the
task timeout is not discarded merely because disconnecting takes longer.
Cancellation still attempts deletion. If abort fails or native cleanup stops
responding, the integration force-stops that runtime and schedules a reload without replaying
requests. This can interrupt other conversations on the same entry. Forced
termination or failed deletion can leave SDK files behind; backups remain
sensitive. If termination cannot be confirmed, image work stays blocked until
the runtime can be stopped safely.

On integration startup or reload, a bounded cleanup pass attempts to remove
abandoned SDK sessions from that entry's `session-state` directory. It checks at
most 200 directory entries and attempts at most 25 deletions within a total
10-second budget, before admitting new requests. The verified native runtime
refuses deletion of sessions still owned by another process. Linked directories
and unrecognized names are not followed or removed.

Cleanup failures and limits produce a warning with counts, not session contents.
Remaining data can be revisited on a later startup; this is not a periodic
retention service or a guarantee of capped disk usage. Cleanup is disabled on
native runtime versions whose ownership safeguards have not been verified.
Credentials, shared runtime caches, HA traces, generated media, and backups are
not removed by this pass.

### Camera and image analysis

Choose an AI Task model that reports vision support and image limits. Static
PNG, JPEG, WebP, and GIF inputs are supported when the selected model accepts the
format. Animated images, arbitrary files, and remote URLs are not supported.
If limits are unavailable after refreshing model discovery, the task fails
explicitly rather than guessing a safe default.

```yaml
action: ai_task.generate_data
data:
  entity_id: ai_task.github_copilot
  task_name: driveway summary
  instructions: Describe the driveway and mention any visible vehicles.
  attachments:
    - media_content_id: media-source://camera/camera.driveway
      media_content_type: image/jpeg
response_variable: driveway
```

Use your camera's actual entity ID. Explicitly attached images are sent to
GitHub even when device control is disabled; that setting controls tools, not
data you supply. Image contents can contain prompt-injection instructions.
Keep device control off for read-only use, and expose only devices you intend
AI-assisted tasks to access.

Image processing, including SDK resizing, runs on the HA host. Processing is
serialized across integration entries. Regular-file, MIME, dimension, and payload
checks happen before expensive decoding or inference. A conservative admission
budget accounts for decoded pixels and the SDK's multiple encoded-payload copies;
it is not a fixed image-count allowance or a promise of 64 MiB total process RAM.
The model's limits also apply.

Structured validation checks the response shape, not whether descriptions or
counts are factually correct.

Large or numerous images can exceed the host budget even when the model accepts
them. Use a lower-resolution camera/image source, compressed JPEGs, or fewer
attachments rather than retrying unchanged input. HA resolves media before this
integration receives it, so lower-resolution source configuration matters too.
The bounded Linux ARM64 profile is described in the [development guide](development.md#ai-task-boundaries);
it is not a performance guarantee for every Green, Yellow, or Raspberry Pi installation.

For camera snapshots, start with a **720p or 1080p JPEG sub-stream entity** when
your camera integration provides one. A 1920x1080 JPEG around 400 KB fits the
current host estimate. A 2560x1440 snapshot leaves room for only a very small
compressed file (roughly 175 KiB for the first image), and 3840x2160 / 4K exceeds
the pixel allowance regardless of compression. These are examples, not a fixed
2 MP limit; multiple images share the payload budget. Shrinking a camera card
on the dashboard does not reduce the snapshot attached by `media-source://camera/`.

### Image generation

`ai_task.generate_image` is available only when the verified runtime reports an
authorized default image model for your account. It is independent of the
conversation and AI Task chat-model choices. No chat-model turn or HA device tools
are used; only the explicitly requested native image operation is permitted.

```yaml
action: ai_task.generate_image
data:
  entity_id: ai_task.github_copilot
  task_name: sunny weather illustration
  instructions: Create a simple cheerful illustration of a sunny garden.
response_variable: illustration
```

The response includes `url`, `media_source_id`, `conversation_id`, dimensions,
and the image model when reported. Home Assistant publishes the PNG through
its AI Task media source. Expiration of a signed image URL does not itself delete
stored media; HA media and backup retention still apply. Generated images and
active SDK session data may be included in backups.

Image-generation usage is charged according to the native image model, not
the selected chat model's multiplier. Generation is never automatically retried,
including after a timeout, because usage may already have occurred. Reference
images/editing are not supported by this release; omit `attachments` from
generation requests.

## Model selection and fallback

Each time you open the configuration dialog, the integration asks Copilot for
the current list of models. Models disabled by account or organization policy
are omitted.

The integration also checks the selected model when it starts and before every
conversation turn and AI Task, including ongoing chats. It reuses the list for up to an hour
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

Conversation and task model fallbacks and Repairs are tracked separately.
One role succeeding does not clear the other role's model-availability notice.
The task fallback notice lists both text/structured-data and image-analysis
choices, since vision requirements can select a different model. Alternating
request types does not replace one notice with the other.

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

An abrupt exit or failed cleanup can leave files behind; startup cleanup attempts
bounded recovery of abandoned SDK sessions. Backups taken while a chat is active
may include conversation data. A Home Assistant restart starts fresh
conversations. Credentials and settings remain in Home Assistant's
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

Upgrading from 0.2.x to 0.3.0 adds an AI Task entity to each existing entry without
another sign-in. Its model is initially copied from the saved conversation model
and then remains independently configurable; its timeout defaults to 180 seconds.
Existing conversation settings and Assist exposure choices are preserved. The
device-control setting also applies to data tasks, so review it before adding
automations that should only generate text or analyze images. Your preferred
AI Task provider is not changed automatically.

When upgrading Home Assistant from HACS to Marketplace, Home Assistant carries
over the custom repository. Keep the existing GitHub Copilot configuration;
there is no need to remove and re-add the integration.

The conversation entity is named **GitHub Copilot**. New installations normally
use `conversation.github_copilot` (HA adds a suffix if that ID is already taken).
Existing entity IDs and user-defined names are preserved on upgrade. To change
an older generated entity ID, open the entity's settings in HA and review any
Assist pipelines, scripts, or automations that reference it.

## Troubleshooting

### GitHub sign-in errors

If data-task inference reports a confirmed authentication failure, such as a
revoked token, the integration starts Home Assistant's reauthentication flow
even when model discovery was cached. Reauthenticate the integration; the failed
task is not automatically replayed.

### Quota errors

If Copilot reports exhausted usage quota, the integration returns an error,
logs a warning, and creates a notice in **Settings > Repairs**. Check your AI
Credits usage and limits in GitHub, or contact your organization administrator.
The notice persists until a conversation or validated AI Task response succeeds or the integration
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
