# Using GitHub Copilot

For installation, sign-in, a test conversation, and voice assistant setup, see
the [setup guide](setup.md).

## Settings

During initial setup, choose a model and optionally enable device control,
administrator tools, or **Additional instructions** on the same screen. To
change these settings later, open **Settings > Devices & services > GitHub
Copilot > Configure**.

Device control is disabled by default and is limited to entities exposed to
Home Assistant Assist. The integration does not give the Copilot runtime a
general Home Assistant REST token. Additional instructions are literal text;
Home Assistant templates are not expanded.

On Home Assistant 2026.11 or newer, **Allow Home Assistant administrator tools**
is a separate opt-in for conversations and data tasks. It enables HA's
administrator API, including tools for logs, history, configuration and
administrative actions such as calling actions or reloading integrations.
This API is not limited to Assist-exposed entities, even if device control is
disabled. Results, including potentially sensitive log entries, are sent to
GitHub Copilot.

Administrator tools require the context of a Home Assistant administrator.
Non-admin or unauthenticated callers can still chat and use enabled Assist
tools, but the reply explains that administrator tools are unavailable.
Automations and voice requests may not carry an administrator identity; data
tasks continue with their remaining tools and log the restriction without
changing their requested output structure.

The option is hidden on older HA versions. If HA is downgraded after enabling
it, the agent continues without administrator tools and reports the restriction;
disable the option or upgrade HA to remove the warning.

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
abandoned SDK sessions from that entry's `session-state` directory before
admitting new requests. A periodic pass runs after 24 hours, then waits another
24 hours after each attempt. These defaults are fixed; there is no retention
setting or extra retention period for abandoned data.

Each pass checks at most 200 directory entries and attempts at most 25 deletions
with a 10-second maintenance budget. Deletions are sequential, and maintenance
passes are serialized across entries. Scan progress carries forward between
passes so protected or failing candidates do not indefinitely hide a backlog.
An unfinished operation remains owned after a timeout; no overlapping replacement
is started. The budget cannot interrupt an operating-system filesystem call or
undo a deletion already issued to the native runtime.

Live conversations, AI Tasks, and sessions being created or disposed are
protected regardless of age. The verified native runtime also refuses deletion
of sessions owned by another process. Linked directories and unrecognized names
are not followed or removed. Cleanup does not require working authentication or
a model, and does not resume old conversations.

Failures, deferred work, and limits produce warnings without session contents or
IDs; outcome summaries include removed, protected, skipped, and failed counts.
Remaining data can be revisited on a later daily pass or startup. This is
best-effort recovery, not a guarantee of capped disk usage or a maximum retention
age. A large backlog can require multiple passes. Restart or reload resets the
daily timer and in-memory scan progress; missed passes are not replayed.

Cleanup is disabled on native runtime versions whose ownership safeguards have
not been verified. Credentials, shared runtime caches, HA traces, generated
media, and existing backups are not removed by these passes.

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
GitHub even when tool access is disabled; those settings control tools, not data
you supply. Image contents can contain prompt-injection instructions. Keep both
tool options off for tasks that should not access or modify Home Assistant.

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
its AI Task media source, storing it in `ai_task/image` under the first configured
media directory. HA does not automatically delete these images, so repeated
generation increases disk usage. Neither signed URL expiry nor SDK session
cleanup deletes stored images.

On HA 2026.11 or later, use the administrator-only
[`ai_task.clear_images`](https://www.home-assistant.io/actions/ai_task.clear_images/)
action to remove generated images. Set `days: 30`, for example, to delete images
older than 30 days; **omitting `days` deletes all images in the folder**. Run it
manually or schedule it in your own automation. This integration does not schedule
media cleanup, and the action is unavailable on our minimum supported HA version,
2026.10.0.

Cleanup covers the shared AI Task image folder across all providers, not just
GitHub Copilot, and does not protect images referenced by dashboards or
automations. Copy images you want to keep outside that folder before cleanup.
Generated images and active SDK session data may be included in backups;
deleting the originals does not remove copies from existing backups.

Image-generation usage is charged according to the native image model, not
the selected chat model's multiplier. Generation is never automatically retried,
including after a timeout, because usage may already have occurred. Reference
images/editing are not supported by this release; omit `attachments` from
generation requests.

## Model selection and fallback

If initial model discovery fails transiently, the startup check retries in the
background after 5 and 15 seconds. A successful discovery refreshes image
capabilities without requiring a text request. Unknown image capabilities remain
disabled, and confirmed authentication/quota errors or a successfully retrieved
catalog do not trigger these retries. If retries are exhausted, check the logs,
correct the connection problem, and reload the integration.

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

An abrupt exit or failed cleanup can leave files behind; startup and daily
cleanup attempt bounded recovery of abandoned SDK sessions. Backups taken while
a chat is active or residual files remain may include conversation data.
Cleanup does not rewrite existing backups. A Home Assistant restart starts fresh
conversations, not restored SDK sessions. Credentials and settings remain in HA's
config-entry storage; treat that storage and Home Assistant backups as sensitive.
Shared native binaries have a separate
[runtime cache policy](#runtime-cache-and-manual-recovery); session cleanup does
not remove them.

Home Assistant tools come only from the APIs enabled for the request. The
administrator API has broader access than the Assist device-control API.
Other SDK permission requests are rejected. Prompt-injection defenses cannot
eliminate all risk, and the integration adds no extra action-confirmation step.
For the implementation details, see
[instructions and tool permissions](development.md#instructions-and-tool-permissions).

## Runtime cache and manual recovery

The SDK-pinned native runtime is downloaded and checksum-verified into
`<config>/.storage/github_copilot/runtime/<runtime-version>/`. This cache survives
restarts and container replacement.

On supported Linux hosts, one bounded cleanup pass runs after successful
integration setup and HA startup. It keeps the current pinned version and may
remove obsolete versions, including abandoned downloads. Versions still used by
another runtime process, including an orphan from an earlier HA instance, are
retained. If process ownership cannot be checked safely, nothing uncertain is
removed. Other platforms retain the cache for manual cleanup.

There is no reserved rollback version: after cleanup, downgrading the integration
may require downloading the older runtime again. Keep access to GitHub or your
configured runtime download source available. Failed upgrades do not trigger
cleanup. Large caches, interrupted deletion, and safety deferrals may leave
versions or `.pruning-*` directories until a later restart. Cleanup affects future
backup contents, not backups already taken.

After a rollback, a running current-pin runtime can also retain leftover
`.pruning-<pin>-*` trash. Original command-line paths cannot reliably distinguish
users of the rebuilt runtime from users of its trash, so cleanup keeps both.
Restarts alone may not reclaim that trash while the version remains active; use
the manual recovery procedure below if needed.

For manual recovery:

1. Stop Home Assistant. Disabling integration entries is not enough: configuration
   dialogs can also start temporary runtime processes.
2. Confirm that native runtime and installer processes using this cache have
   stopped too. If orphaned processes cannot be ruled out, restart the owning
   container or host and keep HA stopped before proceeding.
3. Remove only the intended obsolete version or cleanup-owned
   `.pruning-<version>-<32-hex>` directory. To force a full re-download, remove the
   exact `<config>/.storage/github_copilot/runtime/` directory. Do not remove
   `.storage/github_copilot/`, entry-owned session directories, credentials, or
   generated media.
4. Start Home Assistant and allow it to download the pinned runtime again if
   necessary.

Do not share this private cache between HA instances or containers, or launch
unmanaged processes from it while cleanup is running. Implementation safeguards
and limits are described in [runtime cache retention](development.md#runtime-cache-retention).

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
over the installed integration. If you previously added this project as a custom
repository, Marketplace recognizes it as a catalog repository automatically.
Keep the existing GitHub Copilot configuration; there is no need to remove and
re-add the integration.

The conversation entity is named **GitHub Copilot**. New installations normally
use `conversation.github_copilot` (HA adds a suffix if that ID is already taken).
Existing entity IDs and user-defined names are preserved on upgrade. To change
an older generated entity ID, open the entity's settings in HA and review any
Assist pipelines, scripts, or automations that reference it.

## Troubleshooting

### GitHub sign-in errors

If task-session creation, data inference, or native image generation reports a
confirmed authentication failure, such as a revoked token, the integration
starts Home Assistant's reauthentication flow even when model discovery was
cached. Reauthenticate the integration; the failed task is not automatically
replayed. Policy denials and temporary rate limits alone do not trigger
reauthentication.

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
installation. If HACS or the pre-install Marketplace listing shows a placeholder,
this does not affect setup or operation.

### Diagnostics and bug reports

Use **Download diagnostics** from the integration's menu in Home Assistant for
authentication-method, model, device-control, and runtime status. The diagnostic
payload does not include access tokens, refresh tokens, prompts, or conversations.

[Open an issue](https://github.com/ChrisRomp/github-copilot-home-assistant/issues) for bugs.
Never include tokens or unredacted conversations in a report.
