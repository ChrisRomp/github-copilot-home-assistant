# GitHub Copilot for Home Assistant

<img src="custom_components/github_copilot/brand/icon.png" alt="GitHub Copilot for Home Assistant icon" width="96" height="96">

Use GitHub Copilot as your Home Assistant Assist conversation agent, with
streamed replies, follow-up conversations, and optional control of exposed
devices.

The integration also provides an AI Task entity for text, structured data, camera
analysis, and native image generation when available to your account. See
[AI Tasks](docs/usage.md#ai-tasks).

[![Open in Home Assistant Marketplace](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ChrisRomp&repository=github-copilot-home-assistant&category=integration)

**[Get started: setup guide with screenshots](docs/setup.md)**

Walk through installation, sign-in, a test conversation, and optional voice setup.

**This is an unofficial, community-built project, not affiliated with GitHub or
the Home Assistant project.**

## Requirements

- Home Assistant 2026.10.0 or newer, with a supported 64-bit OS/runtime and
  outbound access to GitHub. Linux ARM64 and x86-64 are supported; 32-bit is not.
- A GitHub account with GitHub Copilot access. Organization policies and model
  availability apply, as do your plan's usage limits.

For voice, keep your existing speech-to-text and text-to-speech integrations.
This integration supplies the conversation agent, not a complete voice pipeline.

## Installation

Search for **GitHub Copilot** in **Settings > Marketplace** (Home Assistant
2026.11 or newer) or HACS (2026.10), then select the integration and download or
install it. It is included in the default catalog; no custom repository is needed.

The button above opens the repository directly in Marketplace or HACS.
Review and confirm the installation there, then **restart Home Assistant**
before continuing to setup.

| Home Assistant | Where to install | Restart after first install |
| --- | --- | --- |
| 2026.11 or newer | **Settings > Marketplace** | If instructed |
| 2026.10 | HACS, installed separately | Yes |

For manual installation, copy `custom_components/github_copilot/` into
`<config>/custom_components/github_copilot/` and restart Home Assistant.

## Setup

For a step-by-step walkthrough with screenshots, see the
[setup guide](docs/setup.md), including sign-in, a test chat, and voice setup.

1. Go to **Settings > Devices & services > Integrations > Add integration** and select
   **GitHub Copilot**.
2. Sign in with GitHub (recommended). Alternatively, use a fine-grained PAT
   with the **Copilot Requests** account permission. Classic PATs are not supported.
3. Choose a **Conversation model** and, optionally, a different **AI Task model**.
   The task timeout defaults to 180 seconds. You can also enable
   **Allow control of Assist-exposed devices**, optionally enable administrator
   tools on Home Assistant 2026.11 or newer, and provide conversation-only
   **Additional instructions**.
4. Under **Settings > Voice assistants**, add or edit an assistant and select
   **GitHub Copilot** as its conversation agent.

Device control is off by default and applies to both conversations and AI Tasks.
It is limited to entities you expose to Assist. The separate administrator-tools
option is also off by default and requires an authenticated Home Assistant
administrator for each request. It provides access to logs, history and
configuration beyond Assist-exposed entities. Action calls, integration
reloads and media playback are blocked. Change these settings under
**Settings > Devices & services > GitHub Copilot > Configure**.

An AI Task entity is created alongside the conversation entity without another
sign-in. Use it in scripts and automations, or choose it as your preferred provider
under **Settings > System > AI**. See the [AI Task usage guide](docs/usage.md#ai-tasks)
for a first task, structured data, camera analysis, and image-generation examples,
with links to Home Assistant's authoritative documentation.

## Privacy and limitations

Model inference runs through GitHub's cloud service. Prompts, relevant device
information, and tool results are sent to GitHub Copilot. Credentials and
temporary conversation files can be included in Home Assistant backups; treat
those backups as sensitive. Logs and configuration may contain sensitive data;
review the administrator-tools option before enabling it.

Chats expire after five minutes of inactivity, and a Home Assistant restart
starts fresh conversations. AI Tasks use disposable sessions. Image requests are
subject to model limits and conservative host resource guards; oversized inputs
fail rather than being silently omitted. Native image generation depends on
account availability. Reference-image editing, local-model fallback, and remote
runtime hosting are not supported.

Abandoned SDK session data is cleaned up at startup and once daily, with bounded
work and protection for live sessions. Cleanup is best-effort, not a disk-usage
cap, and does not remove data from existing backups.

See the [user guide](docs/usage.md) for model fallback, data handling, and
troubleshooting.

## Updates and help

Install updates from **Settings > Updates** on 2026.11 or newer, or through HACS
on 2026.10. Restart after updating an already-loaded integration.

For model or quota errors, check **Settings > Repairs**. For other problems,
see [troubleshooting](docs/usage.md#troubleshooting) or
[open an issue](https://github.com/ChrisRomp/github-copilot-home-assistant/issues).

Developer setup and implementation notes are in the
[development guide](docs/development.md).

## License

Licensed under the [Apache License 2.0](LICENSE).
