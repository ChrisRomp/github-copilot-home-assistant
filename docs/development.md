# Development

For user-facing setup and behavior, see the [README](../README.md) and
[user guide](usage.md).

## Environment and checks

Development requires Python 3.14.2 or newer. The validation workflow tests Home
Assistant 2026.10.0, the minimum supported version, and 2026.11.0. Run these
commands from the repository root:

```sh
python -m pip install -e '.[dev]' 'homeassistant==2026.11.0'
pytest
ruff check .
ruff format --check .
```

Install the requirements from HA's `conversation`, `camera`, `image`, and `stream`
component manifests to run the real HA contracts:

```sh
HA_COPILOT_HA_TESTS=1 pytest tests/test_ha_contract.py tests/test_ai_task.py
```

This checks actual HA schema conversion, tool-result handling, and final speech
without contacting GitHub or controlling devices. With an SDK runtime already
cached, run:

```sh
HA_COPILOT_NATIVE_TESTS=1 pytest tests/test_sdk_contract.py
```

This verifies prompt updates through the real runtime against a loopback-only
fake model, including strict task schema transport and disposable task sessions.

The validation workflow runs unit tests, the real HA contract test, hassfest,
and HACS validation. HACS validation also requires the GitHub repository to
have a description, topics, and issues enabled. Both workflows install and test
through the shared `.github/actions/ha-tests` action.

### HA release watch

`.github/workflows/ha-release-watch.yml` polls PyPI every six hours and tests
the latest stable Home Assistant release. It also tests the newest beta or
release candidate when that beta is for a newer month, such as `2026.12.0b0`
before 2026.12.0 ships. Older month lines are covered only by the CI matrix.
A release that already passed on the current default-branch commit is skipped
unless it has an open failure issue. Run the workflow manually to test one
exact version or to force a retest. Manual runs on other branches can test changes
without updating compatibility issues or contacting Copilot.

The watch installs in watch mode, which bypasses the development HA range so
betas outside the supported range can be tested, and then runs `pip check`. It
skips ruff because the validation workflow already lints every change. Reproduce
a watch failure locally in a new Python virtual environment:

```sh
python .github/scripts/ha_release_watch.py install --mode watch 2026.12.0b0
HA_COPILOT_HA_TESTS=1 python -m pytest -q -rs
```

A passing beta never changes the supported range or the CI matrix; widening
them stays a deliberate change.

Failures open issues labeled `ha-compat`. An issue groups failures by month
line and failure signature, which is the failing step plus the failing test IDs.
A later version that fails the same way updates the existing issue. The issue
closes as fixed when its newest affected version passes, or as superseded when
a newer version in the same month passes. Issue lookups are bounded; when
there are too many open `ha-compat` issues to read, reporting stops with an
error instead of acting on an incomplete list. Each issue body carries a hidden
state marker that the workflow validates before use; do not edit it by hand.

When the optional `COPILOT_ASSIGN_TOKEN` repository secret is set, the watch
assigns Copilot cloud agent to new test failures. Install failures, which
usually mean a dependency conflict, are not assigned. When a later version
fails the same way while Copilot's pull request is open, the watch comments
`@copilot` on that pull request once per version. Pending notifications for
open issues are retried on later report runs, even if the latest release has
changed; a version does not need to be retested to retry its notification.
If Copilot's pull request was closed without a fix, the watch opens a new
issue and assigns it again. Without the secret, or when assignment fails,
issues still open and assignment retries on the next failing run.
The watch records which versions were listed at
assignment time. If GitHub accepts the assignment but the final issue update
fails, a later failure can still notify Copilot's open PR about a new version.
If replacing an issue is interrupted, a later matching failure closes the
stale issue referenced by its replacement.

`GITHUB_TOKEN` cannot assign Copilot, so the secret must be a fine-grained
personal access token limited to this repository, with read and write access
to actions, contents, issues, and pull requests. Only the workflow's final
Copilot step receives it; jobs that install and run tests have read-only
repository access. Renew the token before it expires.

GitHub disables scheduled workflows after 60 days without repository activity.
Re-enable the workflow from the Actions tab if that happens.

## Versions and authentication

Keep the SDK version pins in `custom_components/github_copilot/manifest.json`
and `pyproject.toml` in sync. The SDK wheel pins the native runtime version.
Keep the integration version in those files synchronized with the GitHub
release tag.

The development HA dependency range retains the minimum supported version so
the same dependency set can be installed for both CI matrix entries.

Dependabot security updates must remain enabled in the repository settings.
`.github/dependabot.yml` groups security fixes into separate Python and GitHub
Actions pull requests; routine version-update PRs are disabled. Security updates
are not delayed until the configured version-update schedule.

Dependabot's Python updater does not maintain HA's integration manifest. Any SDK
update must also update that manifest's pin and reverify the native runtime
contracts and image resource profile before advancing the compatibility gates.
CI rejects mismatched SDK pins and integration versions; it does not replace
runtime qualification. Do not auto-merge SDK updates.

See [OAuth setup for maintainers](oauth.md) for OAuth App configuration
and token lifecycle details.

## Runtime storage

The integration and GitHub Copilot SDK run in Python inside Home Assistant.
The SDK starts its pinned Copilot runtime as a child process; model inference
uses GitHub's cloud service.

The native runtime is cached under
`<config>/.storage/github_copilot/runtime/<runtime-version>/`. Its installer runs
in an isolated process and performs the SDK's checksum verification. The cache
survives Core container replacement.

The installer's environment is limited to Python/platform, proxy, and
certificate settings rather than the full HA environment. Network/download,
checksum, storage, and unsupported-platform failures are reported during setup.
Removing an integration entry removes only that entry's runtime state, not
this shared versioned cache.

### Runtime cache retention

After an entry starts successfully and finishes setup, `runtime_cache.py`
schedules one HA-owned cleanup pass after HA has started. Other entries and
reloads share that pass; entry removal and session/media cleanup never trigger
it. The current SDK-pinned version, including any staging files inside it, is
always retained. Other recognized versions are eligible only if unused; no extra
rollback version is reserved. A subsequent downgrade may need to download its
runtime again.

Automatic pruning currently requires Linux, `renameat2`, and no-follow,
descriptor-relative filesystem operations. A bounded `/proc` inventory checks
executable/working-directory paths, command-line references, mapped files, and
process identities, including orphaned processes from an earlier HA instance. Unresolved SDK
launchers/installers, denied or incomplete metadata, PID reuse, restricted procfs,
or incompatible process namespaces defer cleanup rather than imply non-use.
Process environments, memory, credentials, and session contents are not read.
Other platforms keep their caches.

One HA Core must own the cache, with its runtimes and installers visible in the
same PID and mount namespaces. Sharing the cache between containers/HA Cores or
independently launching processes from it is unsupported. Process inspection is
not a cross-process launch lock: it protects observable existing consumers, not
arbitrary external launches after inspection. The integration itself only
provisions/launches the current pin. A matching native process is never considered
safe to delete merely because Linux allows unlinking its executable.

The shared provisioning lock covers inventory and claims. Cleanup accepts only
version-shaped directories and its exact `.pruning-<version>-<32-hex>` recovery
format. It rejects symlinked storage components and top-level candidates.
After a fresh process check, an atomic no-replace rename claims an obsolete
version. Outside the lock, another process check precedes incremental,
descriptor-relative deletion. Nested links are unlinked, never followed;
directory identities are checked around opens and claims, and the cache root is
revalidated after each process check before claiming or deleting. Partial trash
remains eligible for a future startup, including trash whose encoded version
becomes the current pin after a rollback. Both recovery and pre-deletion process
checks match the trash name and its original version so unchanged command-line
paths still protect a claimed runtime.

This deliberately retains ambiguous trash: references to a rebuilt current-pin
directory also protect leftover trash for that version. Original command-line
paths cannot reliably distinguish consumers of the rebuilt directory from
consumers of its trash. Because cleanup runs after runtime startup, restarting HA
may keep retaining that trash while the same version is active. Reclamation
requires no matching process references or the stopped-HA manual recovery
procedure below. Nested mounts, including same-filesystem bind mounts, are not
traversed.

Each pass examines at most 64 cache-root entries, attempts four version/trash
trees, and examines 1,024 tree entries at a maximum nesting depth of 32. Process
checks share limits of 4,096 PID records and 32 MiB of metadata, with a 4 MiB
per-read cap. The common 10-second orchestration budget includes waiting for the
provisioning lock. Budget exhaustion or shutdown stops new operations; an
in-flight filesystem syscall cannot be forcibly interrupted. Cancellation drains
workers before releasing the provisioning lock or directory descriptors.

Logs contain sanitized removed/failed/skipped/protected/deferred counts, budget
status, and fixed reason categories, not process arguments, PIDs, paths, or raw
errors. Benign unknown files and protected versions alone do not generate
warnings. Bounds and repeated safety deferrals can leave work for later restarts;
cleanup does not promise to reach every directory behind a persistent scan limit.
See [manual recovery](#manual-runtime-cache-recovery).

### Manual runtime cache recovery

Cache removal requires a stopped Home Assistant and no processes using the
cache. Disabling integration entries alone is not enough.

1. Stop Home Assistant. Configuration dialogs can also start temporary runtime
   processes.
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
unmanaged processes from it while cleanup is running. After a rollback, a
running current-pin runtime can retain `.pruning-<pin>-*` trash; restarts alone
may not reclaim it while that version remains active.

### Per-entry session retention

Per-entry SDK sessions are stored under
`<config>/.storage/github_copilot/<entry-id>/session-state/<session-id>/`.
`SessionCleanup` runs before backend creation and is reused by one config-entry
background task after startup. The first periodic pass waits 24 hours; subsequent
passes wait 24 hours after the preceding attempt. There is no catch-up loop or
extra age-based retention period. Both paths use the same bounds: 200 directory
entries, 25 deletion attempts, and a 10-second orchestration budget. A shared
HA-scoped lock serializes maintenance across entries, including outstanding
operations whose caller budget has expired.

Discovery only recognizes canonical SDK UUID directories. An owned `scandir`
iterator carries progress between bounded passes, including past protected or
failed candidates; it closes at the end of traversal or runtime shutdown.
A saved iterator that is already exhausted restarts once within the same pass,
without spending the scan or deletion budgets twice or skipping a daily retry.
A pending batch of at most 25 IDs preserves unattempted candidates across a
deadline, so a slow failure cannot repeatedly hide the rest of that batch.
If a pass is interrupted during a candidate's filesystem recheck, the candidate
returns to the tail of the pending batch rather than being dropped.
A changed directory identity resets the cursor. Scan state is not persisted
across restarts, and a changing directory may need another traversal.
Native `list_sessions()` omits sessions without recorded turns, so it cannot
discover all crash leftovers. No session contents are read for discovery.
Symlinked storage roots and session directories are rejected or skipped, and each
candidate is checked again before deletion.

The backend reserves a caller-generated UUID before native session creation and
protects it through cancellation-safe disposal, independently of the conversation
cache. This includes data/image tasks and sessions being created or disconnected.
Ordinary failed disposal releases protection for a later retry; unconfirmed
native termination retains guards until confirmed-stop bookkeeping runs.
Maintenance checks live protection again after filesystem awaits, immediately
before SDK deletion.

The native runtime's cross-process ownership locks additionally protect other
live runtimes. The cleanup gate is pinned to the tested runtime version; reverify
it when upgrading the SDK. Native contracts cover same-client protection,
explicit UUID creation, an active second process, disconnected/incomplete
sessions, forced-stop leftovers, entry isolation, and nested links to unrelated
files. There is no recursive filesystem-deletion fallback if SDK deletion fails.

A pass that exceeds its budget retains its outstanding operation and issues no
further deletions. Later attempts defer until it finishes. Inventory runs off the
event loop, and cancellation drains the bounded filesystem worker before closing
its iterator. Shutdown first quiesces maintenance, then drains the backend and
stops the native client, then closes maintenance resources. A filesystem syscall
cannot be forcibly interrupted, nor can a timed-out SDK deletion be undone.
Housekeeping failures do not independently force-stop a healthy runtime.
Unexpected pass failures are logged without raw errors and do not stop the daily
worker; it waits for the next daily interval before trying again.

Maintenance never calls model discovery or credential providers. It logs
sanitized removed/protected/skipped/failed counts and budget/deferred warnings,
without session summaries, IDs, paths, or raw native errors. Protected local
sessions alone do not produce warnings. Credentials, shared binaries, HA media,
traces, and backups remain outside this cleanup scope.

Session cleanup and backup implications are described in the
[user guide](usage.md#conversation-data-and-privacy).

## Instructions and tool permissions

### Shared conversation memory

`memory.py` owns one `MemoryManager` and mutation lock per entry runtime. The
version-1 HA JSON Store key is `github_copilot.memory.<entry-id>`, written to
`<config>/.storage/github_copilot.memory.<entry-id>` outside the SDK session
directory. It is HA-managed, not an editable Markdown file. It uses private file
permissions and atomic writes, not encryption. `async_save` can swallow HA
write errors, so acknowledgement requires an executor-backed, non-cached disk
read matching the full candidate collection. The normal Store load path is
deliberately not used: it can rename corrupt JSON and return an empty result.
Malformed, unsupported, and unreadable files instead fail closed without
overwriting recoverable data. Missing files represent empty collections.

The collection holds at most 50 facts of 300 characters each. Records contain
opaque stable IDs, fact text, creation/update timestamps, and a source quotation
of at most 300 characters from the actual user request. No complete transcript
or HA user identifier is stored. Diagnostics expose only the enabled flag and
aggregate count, which is unknown before loading.

With memory enabled, unreadable storage prevents startup or fails conversation
turns before sending them to Copilot. Disabling memory permits use without
recall and preserves the file. Restore a valid backup only while the integration
entry is unloaded, then reload it. Editing, replacing, or deleting the store
while the entry is running is unsupported; a restored revision could otherwise
match an active session's inventory revision despite different contents.

The schema validates IDs, timestamps, bounded fact/source strings, collection
size, and monotonic nonnegative revisions. Mutation calls serialize through the
entry lock and reject stale expected revisions with the latest snapshot.
Supporting source quotations must be exact excerpts of the unchanged actual
current user request; this establishes provenance, not semantic truth. Exact
duplicates are unchanged; explicit-ID updates work at capacity without eviction.
Updating one ID to text already held by another returns `invalid_input` with the
duplicate ID and current snapshot, without changing either fact.
Accepted writes and verification reads drain on cancellation before unlocking;
shutdown closes admission and drains memory before tearing down the runtime.
Entry removal explicitly calls `Store.async_remove`, even without a loaded
runtime. HA removes an entry even if its removal callback raises, so a failed
memory unlink is logged without raw details and reported through a persistent
Repair identifying the leftover file and manual cleanup steps. The Repair is
not tied to the removed config entry, so HA's registry cleanup retains it.
If runtime shutdown fails or is cancelled before deletion, the same Repair is
created before propagating the failure; runtime ownership and live session files
are retained. Memory writes are first drained, then a metadata-only filesystem
check suppresses the warning when storage is confirmed absent. An unreadable
path still produces the warning because absence cannot be established.
After manual cleanup, users can acknowledge the notice with HA's existing
Ignore action; there is no background filesystem monitor or custom repair flow.
After a memory unlink failure, other entry cleanup continues.
Reloads, disabling, and SDK cleanup do not delete
memory. Restarting the same runtime drains its previous stop and creates a fresh
manager before loading the retained store; old tool handlers keep their closed
manager.

The excerpt check is not proof that a proposed fact is supported or that a
deletion is authorized. Text passed through `conversation.process`, including
templated automation text, counts as the user request. Remembering decisions
still depend on model interpretation of the fixed policy; untrusted external
content must not be promoted into request instructions.
Memory tool arguments and results enter HA's ChatLog and may appear in Assist
debug traces, including fact text, source excerpts, and full snapshots.
Forgetting does not rewrite those records, already-sent requests, or backups.

`ha_memory_list`, `ha_memory_save`, and `ha_memory_forget` are integration-owned
conversation tools, routed separately from HA device/admin APIs. They recheck
the enabled option and active turn, including after waiting for the memory lock.
Native SDK memory is explicitly disabled for every session. Data and image tasks
never get these tools, the memory policy, or reference snapshots.

Only fixed memory policy enters the trusted identity override. Fact contents
are JSON reference data in the SDK user input or untrusted tool results, never
system/developer content, owner custom instructions, or tool descriptions.
HA-visible input and source provenance stay unchanged; SDK `display_prompt`
retains the actual request while the model-bound prompt carries separate
reference data. Snapshots contain IDs, text, revision and capacity, not sources
or timestamps. Reference JSON escapes `<`, `>`, and `&` so stored strings cannot
close the reference delimiter; decoding it preserves the exact saved facts.
Session revision tracking avoids unchanged copies and refreshes after other-chat
writes, compaction, context clearing, truncation, or rewind. Mutation results carry
the full new inventory and update that session's tracking; old history is not
rewritten when forgetting.

Memory-enabled conversation sessions raise the SDK large-tool-output threshold
to 256,000 bytes so even a full, JSON-escaped Unicode inventory remains inline
instead of becoming an unreadable file reference. This threshold also applies
to other tool results in those sessions, and to satellite-history sessions so
results up to the history output budget stay inline; other conversations and
AI Tasks retain the SDK default. Native contracts verify all 50 maximum-length facts
actually reach the model.

`tests/test_memory.py` uses real HA Store/disk for limits, concurrency, private
permissions, swallowed write failures, corruption preservation, cancellation,
and unloaded-entry removal. Backend tests cover tool/lifecycle boundaries.
Opt-in native contracts exercise actual outbound user/tool versus system roles,
snapshot refresh, context reset and compaction through the cached runtime and a
loopback fake model. No live GitHub requests or HA device actions are needed.
Report unavailable native/HA contracts as unverified, not passing.

### Satellite history

The history grant is specific to the original conversation input: it requires
`context.user_id is None`, a registered and enabled `assist_satellite`, and its
stable registry ID in the entry options or allow-all mode. Do not infer identity
from `device_id`, or pass the AI Task path through this authorization.

Entity discovery and reads share one eligibility rule: enumerate current HA
states, then apply `async_should_expose` with the `conversation` assistant.
Recheck the satellite grant against the entry's current options before each
tool call and after every Recorder read, and recheck exposure of every
requested entity; discard the whole result if access or exposure changed while
a query was running. Never call the exposure helper on arbitrary model-supplied
IDs. Discovery labels are normalized to single-line text without control or
format characters and are returned only as tool data.

Recorder reads use the bounded per-entity `state_changes_during_period` API on
Recorder's database executor, always ascending, because HA 2026.10 applies
`descending` after the database limit. Point lookups search backward in growing
windows (1 minute, 30 minutes, 12 hours, 7 days, then 31 days) and page forward
only inside the first non-empty window, so typical sensors need one or two
indexed queries and return the real observation time. Recorder's start-time
state uses a synthetic timestamp, so it is only used after the windows are
empty, to prove that nothing older is retained (`no_retained_state`). Any query
or row budget exhaustion returns `incomplete` rather than guessing. A full page
is incomplete even when the remaining turn budget, not the per-entity limit,
set its size. A range with no changes includes the latest real observation at
or before its start when found.

Per-turn query, row, and output limits and a shared in-flight query semaphore
bound work even when a caller is cancelled; the permit is released even if
Recorder lookup fails before a job is submitted. Discovery and reads share the
output budget, and the size check runs after continuation metadata is added.
When the turn's budget is spent, results carry `turn_budget_exhausted` so the
model answers from partial data instead of retrying in the same turn. The
grant and exposure recheck runs after every Recorder query, including each
point-lookup window. The request context includes HA's configured IANA time
zone so relative times on past dates use that date's offset.
Current Recorder exclusion is reported separately and must not hide retained
rows. Unit tests live in `tests/test_history.py`; `tests/test_ha_contract.py`
checks the Assist exposure namespace, device-inherited areas, and Recorder API
behavior on supported versions.

### Trusted instructions and live context

Only fixed integration rules, capability state derived from integration options
and HA eligibility checks, and the owner's **Additional instructions** enter the
SDK's system-instruction channel. Additional instructions are literal text:
HA templates are not expanded into that privileged channel. HA's combined,
rendered system prompt is not forwarded there because it also contains device
and request data.

Conversation instructions describe shared memory, Assist device control, and
administrator tools even when disabled, with their settings labels and
configuration path. Administrator guidance distinguishes a disabled setting from
an unsupported HA version or a caller without administrator access. This
awareness does not expose disabled tools or read disabled memory, and is omitted
from AI Task instructions. Option changes reload the integration and its sessions.

For current entity information, the assistant is instructed to use HA's
`GetLiveContext` tool with relevant name, domain, or area filters when available.
Supplementary API context and caller-supplied context are available on demand
through a separate read-only context tool. This also preserves access to context
on HA APIs without a targeted context tool. Generated device and request context
is returned as tool data instead of being forwarded in HA's combined system
prompt; existing HA tool definitions still describe the available capabilities.

Fixed integration instructions replace the SDK's `identity` prompt section in
`customize` mode, while `safety` and `custom_instructions` remain intact. Do not
rely on top-level `content` alongside section removals: the pinned runtime omitted
that content from the model request. Native contract tests must verify the full
conversation/task instructions, SDK safety text, and owner instructions in the
actual outbound messages, not just the `create_session` arguments.

This separation reduces prompt-injection risk; formatting and model instructions
alone cannot eliminate it. Context never grants permissions. Device control and
the broader administrator API are separate opt-ins; the latter requires an
authenticated administrator and is not limited to Assist-exposed entities.
Other SDK permission requests are explicitly rejected. The integration does not
give the Copilot runtime a general Home Assistant REST token or impose a new
action-confirmation policy.

If an action result cannot be serialized, the integration reports that the
action may already have completed and does not replay it.

## AI Task boundaries

`ai_task.py` adapts HA's task contract without overriding final HA methods.
The context hook captures the entity, HA chat session, asyncio task, and caller
context together. The provider hook consumes that record only for the matching
request, so interleaving, inherited task contexts, and failed preparation do not
reuse another caller. The backend provisions only the selected HA APIs, with
`assistant="conversation"` for Assist exposure rules. Administrator access
uses the captured caller context and is checked before each tool call.
Rejected child callers retain their inherited record so retrying the same
invalid call cannot bypass provenance checks.

Tasks share execution admission, credentials, tool handling, cancellation, and
cleanup with conversations, but have their own model/Repair state and disposable
sessions outside the conversation-history cache. Their structured responses use
native SDK schemas plus original HA validation. Schema/refusal errors must not
be treated as model removal.

Open-ended object selectors are not supported by the strict output adapter.
Multi-select fields, bounded numbers, and fixed-length arrays such as RGB colors
are supported. Provider schemas use textual hints for uniqueness, numeric
constraints, and array lengths unsupported by some models' strict output APIs;
the original HA selectors still validate these constraints on the result.

Provider schema errors are classified from both OpenAI-style `response_format`
and Anthropic-style `output_config.format.schema` messages, SDK error codes, and
wrapped exceptions. Diagnostic summaries log the operation, active model, and
failure category; unsupported keywords are extracted only from an allowlist and
explicit rejection phrases. Do not log full schemas, field names, response data,
or raw provider errors in these summaries. SDK and HA logs are separate from
these integration-owned summaries.

One-shot session disposal runs in the admitting task after execution deadlines
have ended. Data tasks use one outer execution deadline so a second timer cannot
interrupt an already-draining HA action. Abort, disconnect, delete, and graceful
client stop each have a 10-second cleanup budget; native force-stop has a separate
10-second budget.
Cleanup workers are shielded from caller cancellation and deletion is attempted
even if disconnect is interrupted. A wedged native client is closed to new
requests, terminated through a single-flight stop, and reloaded asynchronously
without awaiting reload from the turn being drained. If termination fails,
retain the client/backend and quarantine image admission for a later stop retry.
Do not apply native cleanup deadlines to local image IO or HA device actions.
An abort RPC error also requires confirmed native termination before releasing
image admission; ordinary deletion failures after completed work remain
best-effort and are logged.
Confirmed-stop bookkeeping runs in an owned cleanup worker, so caller
cancellation cannot skip releasing quarantined image admission after a
successful force-stop.

Task Repair content is derived from both text and vision resolutions from the
same catalog snapshot, while diagnostics retain the actual selected task model.
The existing startup model check performs at most two delayed retries while the
initial catalog is unknown, after 5 and 15 seconds; it stops on shutdown, known
catalog state, authentication rejection, or quota exhaustion.
HA time selectors request local `HH:MM:SS` strings rather than RFC 3339 `time`
values with offsets; original HA validation remains authoritative.

`images.py` uses a conservative 64 MiB admission estimate, accounting for decoded
pixels and encoded transport amplification. Input accounting reserves 32 encoded
payload copies after the first Linux profile showed that the earlier estimate
could admit dense PNG batches that exhausted a 512 MiB container. This is not a
64 MiB total-RSS guarantee. Reads are bounded and nonblocking, non-regular files
are rejected, and raster buffers are explicitly closed before inference.

Input processing is serialized across integration entries. As examples of the
current admission estimate, a 1920x1080 JPEG around 400 KB fits, a 2560x1440
snapshot leaves room for roughly 175 KiB of compressed data for the first image,
and 3840x2160 exceeds the pixel allowance regardless of compression. Multiple
images share the payload budget; these examples are not a fixed 2 MP limit.

`tests/image_resource_probe.py` exercises the production image helper and native
SDK with a loopback-only fake model. It refuses to run unless the environment is
Linux ARM64, limited to one CPU and at most 512 MiB. Run it in a disposable
container with networking disabled, read-only source/dependency mounts, and a
writable scratch directory. Generate bounded fixtures separately with
`python tests/image_resource_probe.py --make-fixtures <directory>`, then supply
`--fixtures` and `--runtime` inside the container. The Linux SDK/Pillow/aiohttp
dependencies and checksum-verified native runtime must already be available.
Add `--asset-representations` to retain an additional asset payload and repeat it
by reference alongside the native output, exercising representation-independent
admission while accounting for other held assets.

With runtime 1.0.109 (SDK 1.0.31), PNG/JPEG/static GIF/WebP inputs reached the
fake model; oversized inputs were rejected before inference. The latest profile, including
generated-PNG pixel decoding and duplicate asset representations, peaked below
441 MiB for successful input cases and below 458 MiB for the large valid output.
A deliberately oversized output was rejected with a peak below 470 MiB,
without OOM or process death. These numbers include Python, the native runtime,
and test infrastructure, not a running HA server. Re-profile after changing the
SDK, input/output bounds, or image processing.

Native generation was reverified on runtime 1.0.109, including explicit session
credentials with logged-in-user fallback disabled. The implementation uses
`session.tools.execute` with only `builtin:image_generation` offered, rather than
asking a chat model to choose a tool. Permission approval is single-use and bound
to the tool-call ID and exact prompt arguments; managed approvals are not
bypassed. It requests `save_to_disk=False`, validates the canonical PNG, and
deduplicates duplicate output representations. Referenced binary assets are
handled only within the owning task.

HA publishes generated PNGs through its AI Task media source and stores them in
`ai_task/image` under the first configured media directory. Signed URL expiry and
SDK session cleanup do not remove these files. HA 2026.11's `ai_task.clear_images`
action manages that shared folder across providers, independently of this
integration's session and runtime-cache cleanup.

The live requalification used the existing GitHub CLI sign-in, passed its token
only in memory as an explicit session credential, and made one synthetic image
request with no chat model selected. The production image collector accepted
the returned PNG, no image file was saved by the runtime, and the disposable
session was removed. This live check requires separate authorization because it
consumes account usage; the loopback-only resource probe cannot replace it.

The image contract/resource gates are enabled for the verified runtime. A new
runtime version does not automatically inherit image support: its contracts and
resource behavior must be rechecked. Generation also requires the account's
available default image model. Reference-image editing is not enabled. Never
use blanket permission approval, another runtime profile, or unrelated built-ins
to force capabilities to appear.
