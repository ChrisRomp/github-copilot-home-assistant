---
name: ha-docs-screenshots
description: 'Capture or refresh real Home Assistant UI screenshots used in this repository documentation. Use when asked to update setup/configuration screenshots, when documented UI fields change, or when an existing docs image may be stale. Inspect the existing image, ask whether to refresh it when uncertain, and capture a disposable local HA instance rather than a recreated mockup.'
license: Apache-2.0
---

# Documentation UI screenshots

Refresh documentation images using the current integration's real Home
Assistant forms, with synthetic local data and no live household connection.

## Decide what needs updating

Find the affected documentation and image references, starting with
`docs/setup.md` and `docs/images/setup/`. Read the surrounding instructions and
view the existing image before choosing a replacement.

If a UI change appears to make a documentation image stale but no refresh was
requested, ask whether the user wants it updated. When the request is explicit,
proceed without asking again. Confirm the intended screen or state if ambiguous.

Preserve the existing asset path where practical. Match the documentation's
theme, framing, and readable scale; do not use this as a reason to redesign
unrelated screenshots.

## Prepare a disposable real UI

Use a supported HA environment established by the `ha-verification` skill.
Confirm the interpreter and HA versions; provision component requirements from
the target HA manifests only when needed. Use the installed package feed as-is.

The [fixture helper](scripts/prepare_screenshot.py) copies the current integration
to a **new directory outside the repository**. It keeps its options flow and
translations intact but replaces startup in the copy with a model-list stub.
It seeds one registered bedroom satellite and a demonstration config entry.
No GitHub token, real HA config, or live device is needed.

Example from the repository root, with `VENV` set to the chosen environment:

```sh
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ha-docs.XXXXXX")"
"$VENV/bin/python" .github/skills/ha-docs-screenshots/scripts/prepare_screenshot.py \
  --output "$WORK/config" --port 18123
"$VENV/bin/python" -m homeassistant --config "$WORK/config" --skip-pip
```

Run the last command as an attached, task-owned background process using the
available process tool. Record its ID and verify an HTTP response at
`http://127.0.0.1:18123/` before opening the browser. Choose a free port; never
stop an unrelated service to take its port. The helper binds HTTP to loopback.

Complete local onboarding with disposable sample details. Open
**Settings > Devices & services > GitHub Copilot > Configure** (or the matching
options action in the installed HA version).

The fixture demonstrates selected-satellite access with allow-all off, device
control and memory on, and admin tools on only when that HA version supports
them. These are **example selections, not product defaults**. Adjust the
disposable entry through the real UI for the requested screenshot. Model names
are illustrative, not evidence of availability for a real account.

## Capture the intended state

Load the available browser automation skill, such as `playwright-cli`. Use a
uniquely named browser session owned by this task, and direct generated browser
snapshots/logs to the disposable workspace rather than the repository.

Use browser snapshots to navigate. Wait for fields, translations, and selections
to finish rendering. In particular:

- Keep selected endpoints and global allow-all consistent with the example
  being explained. Use registered entities, not fabricated UI chips.
- Close dropdowns, clear text selection, and remove input focus by clicking a
  neutral heading or blurring the actual focused input.
- Inspect internal scrolling. A full-page screenshot does not reveal controls
  hidden inside a scrolling dialog. Increase viewport height, scroll as needed,
  or use multiple clearly described images rather than silently clipping fields.
- Capture the dialog element when the background adds no useful context.
  Use the full page only when the surrounding navigation matters.

For this options form, a roughly 580-pixel-wide viewport is a useful starting
point, not a fixed contract. Choose height based on the actual current content.
Use a fresh dialog locator, not a reference copied from an earlier browser run.

With `playwright-cli`, `eval` expects an expression. A simple example is:

```js
(() => {
  window.getSelection()?.removeAllRanges();
  return true;
})()
```

HA uses shadow DOM: `document.activeElement` can be a host rather than the
focused input. Prefer a neutral UI click or blur the nested input if needed.
Verify visually that focus styling is gone.

## Inspect and publish the image

Capture first to a temporary PNG, then view the actual image. Check legibility,
complete labels/help text, the intended toggle states, footer controls, and
absence of personal data, tokens, private URLs, cursors, or selection artifacts.

Only replace the documentation asset after this inspection. Update its alt
text and nearby explanation to describe what is actually visible. Distinguish
example enabled settings from disabled/empty defaults and note relevant HA
version restrictions. Check that the Markdown image link still resolves.

A genuine HA frontend rendering synthetic backend data is acceptable; a
hand-drawn HTML imitation or edited label pasted onto an old screenshot is not
evidence of the current UI. This fixture verifies form presentation, not
Copilot inference, device control, or end-to-end authorization.

## Clean up and report

Close only the task-owned browser session and stop only the task-owned server.
Remove identified task-created browser output, config, and temporary captures
after preserving the final image. Do not remove shared environments or caches,
or use browser close-all/process-name termination commands.

Check `git diff --check` and the final change list for accidental artifacts.
Report the image path and accompanying documentation changes. Do not commit,
push, deploy, or modify live HA settings unless separately requested.
