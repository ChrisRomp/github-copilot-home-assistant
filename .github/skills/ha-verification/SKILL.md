---
name: ha-verification
description: 'Verify this Home Assistant integration using the supported Python and HA versions, focused regression tests, and real-HA contracts. Use for verification testing, compatibility checks, validating an HA release, or investigating test failures after integration changes. Distinguish environment failures, skipped contracts, and actual regressions.'
license: Apache-2.0
---

# Home Assistant verification

Exercise the integration against the actual supported HA APIs, not just mocked
unit boundaries. Run commands from the repository root.

## Establish the environment

Read `pyproject.toml`, `custom_components/github_copilot/manifest.json`,
`.github/workflows/validate.yml`, and [the development guide](../../../docs/development.md).
The workflow is the source of truth for the HA version matrix. Dependency
installation lives in `.github/scripts/ha_release_watch.py`, which CI runs
through `.github/actions/ha-tests`. Do not freeze those versions into this skill.

Inspect `git status --short --branch` and the relevant diff first. Preserve
existing edits; verification does not authorize checkout changes, commits,
pushes, deployment, or calls to a live Home Assistant instance.

Use the chosen environment's interpreter explicitly for every command:

```sh
"$VENV/bin/python" -c 'import sys; from importlib.metadata import version; print(sys.version); print("Home Assistant", version("homeassistant"))'
```

Compare the output with the requested HA release and `requires-python`.
`homeassistant.__version__` is not a reliable version source. An old global
interpreter can produce misleading failures or hang collection; do not treat
that as an integration regression.

Reuse a verified compatible environment when available. Otherwise create an
isolated virtual environment using a supported interpreter in a platform
temporary directory outside the repository. Probe for the needed tooling
before installing dependencies. For missing dependencies or changed manifests:

```sh
"$VENV/bin/python" -m pip install -e '.[dev]' "homeassistant==$HA_VERSION"
```

Set `HA_VERSION` to the exact requested or CI-matrix version, and confirm the
installed version afterward. Keep separate environments for matrix entries.
Use the machine's configured package feed unchanged; do not add registry or
proxy configuration to the repository.

## Prepare real-HA contracts

Installing the `homeassistant` distribution alone does not install every
component dependency. Use CI's install script, which installs the dev
dependencies and the requirements of the **installed target HA version**'s
contract components, rather than guessing package names or installing their
latest releases:

```sh
"$VENV/bin/python" .github/scripts/ha_release_watch.py install "$HA_VERSION"
```

For an HA version outside the supported range, such as a monthly beta or an
`ha-compat` issue from the release watch, add `--mode watch`. Watch mode
bypasses the development HA range and runs `pip check`.

Run this installation only when requirements are missing/mismatched or the
dependency manifests changed. Recheck the component list against CI if it
changes. A feed metadata/download error is an environment blocker, not a test
failure: retry the exact supported requirement through the configured feed or
an available local cache. Do not silently substitute unrelated versions or
bypass the feed.

## Select and run the checks

For a narrow change, start with the affected tests in one pytest invocation.
For an explicit verification request, run the full suite against the requested
version, or each supported matrix entry when compatibility coverage is requested:

```sh
HA_COPILOT_HA_TESTS=1 "$VENV/bin/python" -m pytest -q -rs
"$VENV/bin/python" -m ruff check .
"$VENV/bin/python" -m ruff format --check .
git diff --check
```

The full gated run already includes `tests/test_ha_contract.py` and
`tests/test_ai_task.py`; do not rerun them separately just to obtain a second
count. These tests use actual HA objects without contacting GitHub or operating
real devices. Without `HA_COPILOT_HA_TESTS=1`, a green suite can conceal skipped
HA contracts.

Check integration-version and SDK-pin consistency using the checks in CI.
Local pytest does not replace hassfest or HACS validation; report those as
unrun unless their results were actually inspected.

When SDK/native-runtime behavior is relevant, inspect
`tests/test_sdk_contract.py` and use its opt-in cached-runtime checks:

```sh
HA_COPILOT_NATIVE_TESTS=1 "$VENV/bin/python" -m pytest -q -rs tests/test_sdk_contract.py
```

These use a loopback fake model, not a live Copilot request. Missing cached
runtime skips are not a successful runtime qualification. Live image/model
qualification is separate and needs explicit authorization; do not extract
credentials from a real HA config to turn it on.

## Diagnose failures and account for skips

- Separate dependency/import failures from behavioral assertions before editing
  application code.
- Update stale expected defaults or prompt assertions only when the approved
  behavior explains the mismatch. Do not weaken assertions to make a run green.
- Read `-rs` output. Older HA releases can legitimately skip newer admin APIs;
  macOS cannot cover Linux procfs contracts. Report those limits rather than
  claiming every platform or contract passed.
- Do not conflate upstream deprecation warnings with failures. A missing native
  library may be irrelevant to a form-only check but blocks claims about the
  library-dependent feature.
- After a fix, rerun the affected tests, then the requested verification scope.

## Record the outcome

Report the actual Python/HA versions, the command scope, pass/fail/skip counts,
material skip reasons, and unresolved blockers. Do not reuse counts from an
earlier code revision or add overlapping suite counts together.

Stop only helper processes started for this task, using their recorded process
or tool-session IDs. Remove only identified task-created artifacts. Keep logs
and scratch outputs outside the tracked repository; do not save credentials,
machine-specific paths, or private hostnames in reusable instructions.
