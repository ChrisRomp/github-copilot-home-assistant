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

Install HA's conversation requirements listed in its
`components/conversation/manifest.json` to run:

```sh
HA_COPILOT_HA_TESTS=1 pytest tests/test_ha_contract.py
```

This checks actual HA schema conversion, tool-result handling, and final speech
without contacting GitHub or controlling devices. With an SDK runtime already
cached, run:

```sh
HA_COPILOT_NATIVE_TESTS=1 pytest tests/test_sdk_contract.py
```

This verifies prompt updates through the real runtime against a loopback-only
fake model.

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

This separation reduces prompt-injection risk; formatting and model instructions
alone cannot eliminate it. Context never grants permissions. Only Assist-exposed
tools are allowed, device access remains opt-in, and other SDK permission
requests are explicitly rejected. The integration does not give the Copilot
runtime a general Home Assistant REST token or impose a new action-confirmation
policy.

If an action result cannot be serialized, the integration reports that the
action may already have completed and does not replay it.
