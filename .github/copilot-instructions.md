# Copilot instructions

Home Assistant (HA) custom integration that uses the GitHub Copilot Python SDK
as an Assist conversation agent and an AI Task provider. The integration lives
in `custom_components/github_copilot/`. `docs/development.md` is the detailed
design reference; read the relevant section before changing a subsystem.

## Build, test, and lint

Use Python 3.14.2 or newer. CI (`.github/workflows/validate.yml`) tests the HA
versions in its matrix, currently 2026.10.0 (minimum) and 2026.11.0. Use a
separate virtual environment for each HA version.

```sh
python -m pip install -e '.[dev]' 'homeassistant==2026.11.0'
ruff check .
ruff format --check .
pytest                                   # unit tests; real-HA contracts skip
HA_COPILOT_HA_TESTS=1 pytest -q -rs      # what CI runs
pytest tests/test_history.py             # one file
pytest tests/test_history.py::test_name  # one test
pytest -k "satellite and not admin"      # by keyword
HA_COPILOT_NATIVE_TESTS=1 pytest tests/test_sdk_contract.py  # cached SDK runtime + loopback fake model
```

- Before running real-HA contracts (`HA_COPILOT_HA_TESTS=1`), install the
  requirements declared in the installed HA's `conversation`, `camera`, `image`,
  `stream`, and `mcp` component manifests. Copy the script from the CI workflow. A
  green run without the flag can hide skipped contracts.
- CI also runs hassfest, HACS validation, and a check that `version` and the
  `github-copilot-sdk==` pin match between `pyproject.toml` and `manifest.json`.
- Use the `ha-verification` skill (`.github/skills/`) for verification and
  compatibility runs. Use `ha-docs-screenshots` to refresh documentation
  screenshots from a disposable real HA instance; never use mockups.

## Architecture

- `__init__.py` creates one `CopilotRuntime` (`runtime.py`) per config entry,
  stored at `hass.data[DOMAIN][entry_id]`. Then it forwards to the conversation
  and AI Task platforms. Option changes reload the entry and its sessions.
- `CopilotRuntime` owns credentials (`auth.py`), native runtime provisioning
  (`provisioning.py`, cached under `.storage/github_copilot/runtime/<version>/`),
  the SDK client, and the `ConversationBackend`. The SDK launches the pinned
  Copilot runtime as a child process.
- `backend.py`'s `ConversationBackend` is the core. Conversations and AI Tasks
  share admission, credentials, tool handling, cancellation, and cleanup.
  Conversations use a cached, expiring session per chat. AI Tasks use
  disposable sessions and keep their own model and Repair state.
- `conversation.py` and `ai_task.py` are thin HA entity adapters. `ai_task.py`
  captures caller context so that concurrent tasks cannot reuse another
  caller's provenance.
- `llm_api.py` selects HA LLM APIs: Assist device control and opt-in admin
  tools. Integration-owned tools are routed separately from the HA APIs:
  `memory.py` (shared memory) and `history.py` (satellite Recorder history).
- `models.py` resolves models and fallbacks. `api_errors.py` classifies
  quota, auth, model-unavailable, and schema errors. `schema.py` converts HA
  selectors to strict SDK output schemas, and HA validation stays authoritative.
- `runtime_cache.py` prunes the runtime cache. `session_cleanup.py` handles
  per-entry session retention. Both are bounded and fail safe; see
  `docs/development.md` before touching their deletion logic.

## Key conventions

- **Permissions are separate opt-ins, rechecked on every tool call.**
  - Device control is limited to Assist-exposed entities.
  - Admin tools require the current caller to be an HA administrator.
    Sessions are isolated by admin access.
  - Satellite history requires `context.user_id is None`, plus a registered and
    enabled `assist_satellite` that is in options or allowed by allow-all mode.
    Exposure checks use the `conversation` assistant namespace.
  - AI Tasks never receive memory or satellite-history tools.
  - Never call exposure helpers on arbitrary model-supplied entity IDs.
- **Trusted prompt channel:** Only fixed integration text, capability state,
  and the owner's literal Additional instructions enter system instructions.
  Fixed prompts are in `const.py` and use the SDK `customize` mode
  identity-section override; keep `safety` and `custom_instructions`.
  Device/request context, memory facts, and tool data stay in untrusted user or
  tool data. Native contracts must verify actual outbound messages.
- **Voice replies:** Prompts and user-facing notices are spoken. Keep them
  short and avoid contractions where practical (write "does not", not
  "doesn't"). Keep conversation notices concise; AI Task diagnostics may carry
  actionable remediation (see `llm_api.py`).
- **Bounded work everywhere:** Recorder queries, cleanup passes, image
  admission, and memory all use explicit per-call or per-turn budgets. When a
  budget is exhausted, return an explicit `incomplete` or deferred status
  instead of guessing. Logs use sanitized counts or categories, never raw
  errors, paths, PIDs, schemas, or user data.
- **Cancellation safety:** Drain in-flight work (memory writes, cleanup
  workers, Recorder executor jobs) before releasing locks or resources. Shield
  cleanup from caller cancellation.
- **HA version differences:** The code must work on every CI matrix version.
  For example, Recorder applies `descending` after the limit on 2026.10 but not
  on 2026.11, and admin APIs exist only on 2026.11 or newer. Gate behavior on
  probed capability, and expect some admin-contract skips on 2026.10.
- **Releases:** Keep `version` in `pyproject.toml` and `manifest.json` equal
  and matching the release tag. Keep `strings.json` identical to
  `translations/en.json`; a test enforces this.
- **SDK upgrades:** Update the pin in both files. Then reverify the native
  runtime contracts, image resource profile, and session-ownership gates before
  advancing them in `const.py`. Also confirm that `MCP_MAX_RESULT_BYTES` stays
  below the SDK's large-output default (20,480 bytes in the current pin), and
  rerun the native MCP tool-search contract. Never auto-merge SDK updates.
- **Tests:** `tests/conftest.py` supplies lightweight HA boundary doubles for
  unit tests. Behavior that depends on real HA objects belongs in the gated
  `tests/test_ha_contract.py` or `tests/test_ai_task.py`. Tests must never
  contact GitHub or control real devices.
- **Docs:** User docs are `README.md`, `docs/setup.md`, and `docs/usage.md`;
  maintainer docs are `docs/development.md` and `docs/oauth.md`. Update them
  with behavior or option changes. Describe Copilot data processing in one
  place, and link to https://copilot.github.trust.page rather than repeating
  per-feature warnings.
- Use each package manager's locally configured feed. Do not commit registry or
  proxy settings, credentials, or machine-specific paths.
