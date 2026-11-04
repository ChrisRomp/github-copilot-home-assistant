# Development

For user-facing setup and behavior, see the [README](../README.md) and
[user guide](usage.md).

## Environment and checks

Development uses Python 3.14 and pins Home Assistant 2026.10.0 to exercise the
minimum supported version. Run these commands from the repository root:

```sh
python -m pip install -e '.[dev]'
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
have a description, topics, and issues enabled.

## Versions and authentication

Keep the SDK version pins in `custom_components/github_copilot/manifest.json`
and `pyproject.toml` in sync. The SDK wheel pins the native runtime version.
Keep the integration version in those files synchronized with the GitHub
release tag.

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
revalidated after each process check before claiming or deleting. Partial trash remains
eligible for a future startup, including trash whose encoded version becomes
the current pin after a rollback. The post-claim process check matches both the
trash name and its original version so unchanged command-line paths still
protect a claimed runtime. When the original version is the current pin, that
path names the rebuilt current directory, so only rename-following executable,
working-directory, and mapping paths protect the trash. This lets the running
current runtime coexist with reclaiming its leftover trash. Nested mounts, including same-filesystem bind mounts,
are not traversed.

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
See [manual recovery](usage.md#runtime-cache-and-manual-recovery).

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

Only fixed integration rules and the owner's **Additional instructions** enter
the SDK's system-instruction channel. Additional instructions are literal text:
HA templates are not expanded into that privileged channel. HA's combined,
rendered system prompt is not forwarded there because it also contains device
and request data.

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
alone cannot eliminate it. Context never grants permissions. Only Assist-exposed
tools are allowed, device access remains opt-in, and other SDK permission
requests are explicitly rejected. The integration does not give the Copilot
runtime a general Home Assistant REST token or impose a new action-confirmation
policy.

If an action result cannot be serialized, the integration reports that the
action may already have completed and does not replay it.

## AI Task boundaries

`ai_task.py` adapts HA's task contract without overriding final HA methods.
The context hook captures the entity, HA chat session, asyncio task, and caller
context together. The provider hook consumes that record only for the matching
request, so interleaving, inherited task contexts, and failed preparation do not
reuse another caller. The backend provisions only Assist tools, with
`assistant="conversation"` to reuse existing exposure rules.
Rejected child callers retain their inherited record so retrying the same
invalid call cannot bypass provenance checks.

Tasks share execution admission, credentials, tool handling, cancellation, and
cleanup with conversations, but have their own model/Repair state and disposable
sessions outside the conversation-history cache. Their structured responses use
native SDK schemas plus original HA validation. Schema/refusal errors must not
be treated as model removal.

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
initial catalog is unknown; it stops on shutdown, known catalog state,
authentication rejection, or quota exhaustion.
HA time selectors request local `HH:MM:SS` strings rather than RFC 3339 `time`
values with offsets; original HA validation remains authoritative.

`images.py` uses a conservative 64 MiB admission estimate, accounting for decoded
pixels and encoded transport amplification. Input accounting reserves 32 encoded
payload copies after the first Linux profile showed that the earlier estimate
could admit dense PNG batches that exhausted a 512 MiB container. This is not a
64 MiB total-RSS guarantee. Reads are bounded and nonblocking, non-regular files
are rejected, and raster buffers are explicitly closed before inference.

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

With runtime 1.0.103, PNG/JPEG/static GIF/WebP inputs reached the fake model;
oversized inputs were rejected before inference. The latest profile, including
generated-PNG pixel decoding after integrity and budget checks, peaked below
463 MiB for successful input cases and below 497 MiB for the large valid output.
A deliberately oversized output reached the 512 MiB ceiling and was rejected,
without OOM or process death. These numbers include Python, the native runtime,
and test infrastructure, not a running HA server. Re-profile after changing the
SDK, input/output bounds, or image processing.

Native generation was verified on runtime 1.0.103, including explicit session
credentials with logged-in-user fallback disabled. The implementation uses
`session.tools.execute` with only `builtin:image_generation` offered, rather than
asking a chat model to choose a tool. Permission approval is single-use and bound
to the tool-call ID and exact prompt arguments; managed approvals are not
bypassed. It requests `save_to_disk=False`, validates the canonical PNG, and
deduplicates duplicate output representations. Referenced binary assets are
handled only within the owning task.

The image contract/resource gates are enabled for the verified runtime. A new
runtime version does not automatically inherit image support: its contracts and
resource behavior must be rechecked. Generation also requires the account's
available default image model. Reference-image editing is not enabled. Never
use blanket permission approval, another runtime profile, or unrelated built-ins
to force capabilities to appear.
