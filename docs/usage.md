# Using GitHub Copilot

For installation, sign-in, a test conversation, and voice assistant setup, see
the [setup guide](setup.md).

## Settings

Choose your models, optional device control, administrator tools, MCP servers,
shared memory, and **Additional instructions** under **Settings > Devices & services > GitHub
Copilot > Configure**. These options are also available during initial setup.

You can ask Copilot about shared memory, Assist device control, or administrator
tools even when they are disabled. It can explain which settings are enabled
and where to enable a disabled feature, or why enabled administrator tools are
unavailable to the current caller or HA version. Copilot does not turn these
settings on for you.

Device control is disabled by default and is limited to entities exposed to
Home Assistant Assist. Additional instructions are literal text;
Home Assistant templates are not expanded.

On Home Assistant 2026.11 or newer, **Allow Home Assistant administrator tools**
is a separate opt-in for conversations and data tasks. It allows access to logs,
history, configuration, and administrative actions such as reloading integrations.
This access is not limited to Assist-exposed entities, even if device control is
disabled.

Administrator tools require an authenticated Home Assistant administrator.
Other users can still chat and use enabled Assist tools, but administrator tools
are unavailable to them. Automations and voice requests may not identify an
administrator, so these tools may be unavailable there too.

The option is hidden on older HA versions. If HA is downgraded after enabling
it, the agent continues without administrator tools and reports the restriction;
disable the option or upgrade HA to remove the warning.

## Voice satellite history

**Assist satellites allowed to read history** grants selected satellite endpoints
read-only access to retained Recorder state for entities that are currently
exposed to Assist. It works independently of **Allow control of Assist-exposed
devices** and does not grant device control, administrator APIs, or access to
unexposed entities. Requests without an authenticated Home Assistant user can
receive this access only when they come through an allowed, registered Assist
satellite. Other requests are unchanged.

Selecting a satellite trusts the endpoint, not the speaker. Anyone or any
integration able to send a request through that endpoint may ask about the
eligible entities' history. This also applies to software satellites that can be
used remotely or by automations. Use **Allow history from all Assist satellites**
only if every current and future satellite endpoint should receive the same
access; otherwise leave it off and select only the endpoints you trust. Both
options are off by default.

The history tool can look up current Assist-exposed entities and read a bounded
time range or the latest retained value at or before a specified time. It
reports recorded values, timestamps, requested and covered intervals, and
whether a range is complete. Each call accepts up to four entities and a range
of at most 31 days; per-turn query, row, and output limits can make results
incomplete, including a point lookup when the bounded search cannot establish
the latest state. Point lookups report the actual time of the value they find,
or that no value is retained. An unchanged range includes its latest retained
observation at or before the range start when one is found. Recorder's current
inclusion setting is reported separately and does not prove older retained rows are
absent. Exposure is checked again when history is read, so removing an entity
from Assist exposure prevents new reads. Disabling satellite access or removing
an endpoint also prevents new reads; it does not erase results already delivered
in earlier conversation history.

## Shared memory

**Enable shared memory** is off by default. Turn it on to let Copilot remember
lasting household preferences across conversations, such as how you like your
reading lights. It may remember clearly stated facts automatically; you do not
have to say "remember this."

Everyone using the same GitHub Copilot integration entry shares its memories
and can change or forget them. Separate entries keep separate memories. Copilot
does not identify individual voices or keep private memories for each person.

Memory works without enabling device control or administrator tools. Remembering
a preference does not create an automation or give Copilot permission to act.
AI Tasks do not use shared memory.

| Say | What to expect |
| --- | --- |
| "We prefer warm lighting for reading." | Remember the preference. |
| "Set the lights to warm tonight." | Treat it as a request for tonight, not something to remember. |
| "What do you remember?" | Show the saved memories. |
| "Actually, use brighter reading lights from now on." | Replace the old preference. |
| "Forget our reading-light preference." | Forget that preference. |
| "Forget everything you have remembered." | Clear all saved memories. |

Copilot can remember up to **50 facts**, each up to **300 characters**. When
memory is full, it can update a preference you have changed or ask what to
forget. It should not discard unrelated facts to make room, and memories do not
expire with age. Changes made in another chat are available on your next turn.
Copilot can make mistakes, so ask what it remembers and correct anything wrong.

Memories are stored locally in Home Assistant, not in GitHub's native memory
service. Do not store secrets or information intended for only one household member.
Memories survive restarts and integration reloads. Turning memory off stops its
use but keeps your saved facts; turning it back on restores them. Removing the
integration entry deletes its saved memories.

Forgetting a fact does **not** erase it from earlier chats, requests already sent
to GitHub, Home Assistant debug traces, or backups. See
[conversation data and privacy](#conversation-data-and-privacy).
If Copilot reports that memory is unavailable, see
[shared memory errors](#shared-memory-errors).

## Remote MCP tools

Home Assistant's
[Model Context Protocol](https://www.home-assistant.io/integrations/mcp/)
integration connects Home Assistant to remote MCP servers, such as a notes,
calendar, or search service. To let Copilot use a server's tools:

1. Set up the server with the Model Context Protocol integration.
2. Go to **Settings > Devices & services > GitHub Copilot > Configure**.
3. Select the server in **MCP servers available to Copilot**.
4. Optional: select the server in **MCP servers allowed to use write tools**.
   A server must also be selected in step 3; otherwise its write permission is
   not saved.

Copilot does not get MCP tools automatically, and it does not use HA's other
LLM API settings for them. The settings apply to conversations from any caller,
including Assist satellites without a signed-in user, and to AI Tasks.

By default, Copilot gets only the tools that the server marks as read-only. Write
tools can change data on the remote service. The read-only label comes from the
server and is not a guarantee, so select only servers that you trust.

The integration checks the settings again before each tool call. If you remove a
server or its write permission, or the MCP integration unloads, new calls fail.
A server's tool list can change while Home Assistant runs; each new request uses
the current list. At most 64 MCP tools are provided for each request. Tools with
very large parameter definitions are skipped, and a warning is logged. Tool
results larger than 16 KB are cut short and marked as incomplete.

Tool descriptions and results come from the remote server. Copilot is told to
treat them as untrusted data, not as instructions, but this cannot remove all
risk. If a server needs you to sign in again, Copilot reports that the server
needs to be signed in again; reauthenticate the server in Home Assistant. See [conversation data and privacy](#conversation-data-and-privacy).

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

The **AI Task model** is independent of the conversation model.
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
`{{ comfort.data.summary }}` is the generated summary. Results must match the
fields you requested; otherwise the task returns an error rather than
substituting unstructured text. Open-ended object selectors are not supported.

If the model rejects your structure, simplify it or choose another **AI Task
model**. The integration does not automatically retry failed tasks or switch
models for invalid output. A correctly formatted response can still contain
factual mistakes.

### Device access and session lifecycle

Enabling **Allow control of Assist-exposed devices** also enables those tools
for data tasks, using your existing Assist exposure choices. No extra
confirmation step is added. A task may have completed device actions before
timing out or returning an error, so failed requests are not automatically
repeated.

At most two requests run per entry, with at most one AI Task at a time, so tasks
cannot occupy both conversation slots. If tasks overlap, you may get a busy
error. Use HA's queued automation/script mode when tasks should wait their turn.

Each task starts fresh, even if you reuse its name. Home Assistant may still
retain automation traces and input media. See
[conversation data and privacy](#conversation-data-and-privacy).

If a request gets stuck, the integration may reload to recover. This can
interrupt other conversations using the same entry; requests are not replayed
after recovery.

### Camera and image analysis

Choose an AI Task model that supports image analysis. Static
PNG, JPEG, WebP, and GIF inputs are supported when the selected model accepts the
format. Animated images, arbitrary files, and remote URLs are not supported.

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

Use your camera's actual entity ID. Image analysis works even when tool access
is disabled; those settings control tools, not attachments you supply.
Image contents can contain prompt-injection instructions. Keep both
tool options off for tasks that should not access or modify Home Assistant.

Large or numerous images may be rejected to protect your Home Assistant device,
even when the model itself accepts them. Start with a **720p or 1080p JPEG
sub-stream entity** if your camera integration provides one. If an image is too
large, reduce its resolution, use a compressed JPEG, or attach fewer images.
4K snapshots exceed the current limits.

Change the camera source itself: shrinking a camera card on the dashboard does
not reduce the snapshot attached to a task.

### Image generation

`ai_task.generate_image` is available when your Copilot account has access to
image generation. It uses a separate image model, not your selected conversation
or AI Task chat model, and does not use HA device tools.

```yaml
action: ai_task.generate_image
data:
  entity_id: ai_task.github_copilot
  task_name: sunny weather illustration
  instructions: Create a simple cheerful illustration of a sunny garden.
response_variable: illustration
```

The response includes `url`, `media_source_id`, `conversation_id`, dimensions,
and the image model when reported. Home Assistant keeps generated images until
you delete them, so repeated generation increases disk usage. An expired image
URL does not delete the saved image.

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
Generated images and conversation data may be included in backups;
deleting the originals does not remove copies from existing backups.

Image-generation usage is charged according to the image model, not
the selected chat model's multiplier. Generation is never automatically retried,
including after a timeout, because usage may already have occurred. Reference
images/editing are not supported by this release; omit `attachments` from
generation requests.

## Model selection and fallback

The model picker shows the models currently available to your account, including
any organization restrictions and known billing multipliers.

If your chosen model becomes unavailable, Copilot tries the closest available
version in the same family and shows a notice in **Settings > Repairs**. Your
saved choice is preserved and will be used again if it becomes available.
If no suitable replacement exists, choose another model in **Configure**.

Automatic fallback does not exceed your selected model's last known billing
multiplier. If that cost is unknown, the Repair notice warns that cost may
differ. A replacement with unknown cost is not automatically selected when the
original cost is known.

Conversation and AI Task models have separate fallback notices. Image-analysis
tasks may need a different replacement because they require vision support.
If a model becomes unavailable during a request, the replacement is used for
the next request; the failed request is not repeated because it may already
have controlled a device or consumed usage.

## Conversation data and privacy

Prompts, the information needed to answer them, attached images, and tool results
are sent to GitHub Copilot. Model inference runs in GitHub's cloud service, not on your
Home Assistant device. For details about how GitHub Copilot processes data, see the
[GitHub Copilot Trust Center](https://copilot.github.trust.page).

Conversations expire after five minutes of inactivity. Restarting Home Assistant
starts fresh chats, but any enabled shared memories remain available.

Temporary conversation data can be saved on your Home Assistant device. The
integration cleans up its expired chats and retries leftover files at startup
and daily, but an unexpected shutdown or cleanup failure can leave data behind.
Home Assistant can also retain debug traces, automation traces, and backups
containing conversation data. Deleting a chat or memory does not erase those
copies or requests already sent to GitHub. Treat backups as sensitive: they may
also contain credentials, settings, and generated images.

Copilot can only use the Home Assistant tools you enable. When Copilot calls an
MCP tool, the integration sends the tool arguments to that MCP server, and the
result is sent to GitHub Copilot. Administrator tools
have broader access than Assist device control. There is no extra confirmation
step before actions, and protections against misleading instructions in device
or external data cannot eliminate all risk. Keep tool access off for tasks that
should only generate text or analyze images. Do not turn untrusted external
content into instructions in your automations.

## Runtime cache and manual recovery

The integration keeps downloaded runtime files so it does not need to download
them on every restart. On supported Linux hosts, it removes unused older
versions when it can do so safely. Downgrading may require another download.

You normally do not need to manage these files. If disk usage or a damaged
download requires manual cleanup, follow the
[advanced recovery procedure](development.md#manual-runtime-cache-recovery).
Do not delete Home Assistant storage folders while it is running.

## Updates and existing installations

On Home Assistant 2026.11 or newer, updates appear under **Settings > Updates**.
On 2026.10, use HACS to update. Restart Home Assistant after updating an
already-loaded integration.

Version 0.5.0 adds [shared conversation memory](#shared-memory). It remains
disabled for existing entries unless **Enable shared memory** is turned on in
the integration's options. Existing conversation, AI Task, and tool settings
are preserved.

Version 0.4.0 adds optional administrator tools on Home Assistant 2026.11 or
newer. They remain disabled for existing entries unless enabled in the
integration's options, and each request requires an authenticated administrator.
Existing conversation, AI Task, and Assist device-control settings are preserved.
Home Assistant 2026.10 remains supported without administrator tools.

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

If your token expires or is revoked, Home Assistant asks you to reauthenticate
the integration. Complete that flow, then retry your request if appropriate.
Failed tasks are not automatically repeated. Organization policy restrictions
and temporary rate limits are separate problems; signing in again may not fix
them.

### Quota errors

If Copilot reports exhausted usage quota, the integration returns an error,
logs a warning, and creates a notice in **Settings > Repairs**. Check your AI
Credits usage and limits in GitHub, or contact your organization administrator.
Quota failures do not trigger model fallback or automatic retries. Temporary
rate limiting alone is not treated as exhausted quota.

### Shared memory errors

If saved memories cannot be read, Copilot reports an error rather than erasing
them. This can prevent conversations or integration startup while memory is
enabled. Turn off **Enable shared memory** in **Configure** to continue without
memory.

For restoring a backup or investigating storage errors, see
[memory storage and recovery](development.md#shared-conversation-memory).
If memories could not be deleted when you removed the integration, check
**Settings > Repairs** for instructions to remove the remaining data.

### Setup failures

Check that Home Assistant is running on a supported 64-bit Linux system, can
reach GitHub, and can write to its configuration directory. Setup reports
download and storage failures. If a download needs to be replaced, see
[runtime cache and manual recovery](#runtime-cache-and-manual-recovery).

### Missing icon

If HACS or the pre-install Marketplace listing shows a placeholder icon, this
does not affect setup or operation. Home Assistant should show the integration's
icon after installation.

### Diagnostics and bug reports

Use **Download diagnostics** from the integration's menu in Home Assistant for
authentication-method, model, device-control, and runtime status. The diagnostic
payload does not include access tokens, refresh tokens, prompts, or conversations.

[Open an issue](https://github.com/ChrisRomp/github-copilot-home-assistant/issues) for bugs.
Never include tokens or unredacted conversations in a report.
