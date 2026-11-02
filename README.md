# GitHub Copilot for Home Assistant

<img src="custom_components/github_copilot/brand/icon.png" alt="GitHub Copilot for Home Assistant icon" width="96" height="96">

Use GitHub Copilot as your Home Assistant Assist conversation agent, with
streamed replies, follow-up conversations, and optional control of exposed
devices.

[![Open in Home Assistant Marketplace](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ChrisRomp&repository=github-copilot-home-assistant&category=integration)

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

Use the button above to add [ChrisRomp/github-copilot-home-assistant](https://github.com/ChrisRomp/github-copilot-home-assistant)
as a custom repository with category **Integration**, then install the integration.
The project is not in the default catalog.

| Home Assistant | Where to install | Restart after first install |
| --- | --- | --- |
| 2026.11 or newer | **Settings > Marketplace** | Only if prompted |
| 2026.10 | HACS, installed separately | Yes |

For manual installation, copy `custom_components/github_copilot/` into
`<config>/custom_components/github_copilot/` and restart Home Assistant.

## Setup

1. Go to **Settings > Devices & services > Add integration** and select
   **GitHub Copilot**.
2. Sign in with GitHub and choose a model. Alternatively, use a fine-grained PAT
   with the **Copilot Requests** account permission. Classic PATs are not supported.
3. Under **Settings > Voice assistants**, add or edit an assistant and select
   **GitHub Copilot** as its conversation agent.

Device control is off by default. To enable it, open **Settings > Devices &
services > GitHub Copilot > Configure** and turn on **Allow control of
Assist-exposed devices**. Only entities you expose to Assist are available.
The same dialog lets you change the model and add your own instructions.

## Privacy and limitations

Model inference runs through GitHub's cloud service. Prompts, relevant device
information, and tool results are sent to GitHub Copilot. Credentials and
temporary conversation files can be included in Home Assistant backups; treat
those backups as sensitive.

Chats expire after five minutes of inactivity, and a Home Assistant restart
starts fresh conversations. AI Tasks, local-model fallback, and remote runtime
hosting are not supported.

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
