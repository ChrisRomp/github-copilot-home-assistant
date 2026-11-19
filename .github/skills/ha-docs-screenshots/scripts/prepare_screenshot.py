"""Prepare a loopback-only Home Assistant fixture for documentation screenshots."""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import sys
from pathlib import Path
from textwrap import dedent

RUNTIME = """\
from types import SimpleNamespace

from .const import DOMAIN

MODELS = [
    SimpleNamespace(id="gpt-6-luna", name="GPT-6 Luna"),
    SimpleNamespace(id="claude-sonnet-5.5", name="Claude Sonnet 5.5"),
]


class ScreenshotRuntime:
    is_started = True

    async def async_list_models(self, *, force_refresh=False):
        return MODELS


async def async_setup_entry(hass, entry):
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = ScreenshotRuntime()
    return True


async def async_unload_entry(hass, entry):
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return True
"""

SEED = """\
from datetime import UTC, datetime
from types import MappingProxyType

from homeassistant.config_entries import SOURCE_USER, ConfigEntry
from homeassistant.const import CONF_LLM_HASS_API, CONF_PROMPT
from homeassistant.helpers import entity_registry as er

from custom_components.github_copilot.config_flow import GitHubCopilotConfigFlow
from custom_components.github_copilot.const import (
    CONF_AI_TASK_MODEL,
    CONF_AI_TASK_TIMEOUT,
    CONF_ALLOW_ALL_SATELLITES,
    CONF_ALLOWED_SATELLITES,
    CONF_ENABLE_DEVICE_CONTROL,
    CONF_ENABLE_HOME_ASSISTANT_API,
    CONF_ENABLE_SHARED_MEMORY,
    CONF_MODEL,
    DOMAIN,
)
from custom_components.github_copilot.llm_api import home_assistant_api_supported


async def async_setup(hass, config):
    if hass.config_entries.async_entries(DOMAIN):
        return True

    registry = er.async_get(hass)
    satellite = registry.async_get_or_create(
        "assist_satellite",
        "screenshot_seed",
        "bedroom",
        suggested_object_id="bedroom",
    )
    admin_supported = home_assistant_api_supported()
    now = datetime.now(UTC)
    entry = ConfigEntry(
        created_at=now,
        data={},
        discovery_keys=MappingProxyType({}),
        domain=DOMAIN,
        minor_version=GitHubCopilotConfigFlow.MINOR_VERSION,
        options={
            CONF_MODEL: "gpt-6-luna",
            CONF_AI_TASK_MODEL: "claude-sonnet-5.5",
            CONF_AI_TASK_TIMEOUT: 180,
            CONF_ENABLE_DEVICE_CONTROL: True,
            CONF_ENABLE_HOME_ASSISTANT_API: admin_supported,
            CONF_ALLOW_ALL_SATELLITES: False,
            CONF_ALLOWED_SATELLITES: [satellite.id],
            CONF_ENABLE_SHARED_MEMORY: True,
            CONF_LLM_HASS_API: (
                ["assist", "homeassistant"] if admin_supported else ["assist"]
            ),
            CONF_PROMPT: "",
        },
        source=SOURCE_USER,
        subentries_data=(),
        title="GitHub Copilot",
        unique_id=None,
        version=GitHubCopilotConfigFlow.VERSION,
    )
    await hass.config_entries.async_add(entry)
    return True
"""


def main() -> None:
    """Create a fresh fixture without modifying the source integration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New disposable HA config directory outside the repository",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[4],
        help="Repository root (default: inferred from this script's location)",
    )
    parser.add_argument(
        "--port", type=int, default=18123, help="Loopback HTTP port (default: 18123)"
    )
    args = parser.parse_args()
    root = args.repo_root.expanduser().resolve()
    requested_output = args.output.expanduser()
    if requested_output.exists() or requested_output.is_symlink():
        parser.error(f"Refusing to overwrite existing output: {requested_output}")
    output = requested_output.resolve()
    source = root / "custom_components" / "github_copilot"

    if not (source / "config_flow.py").is_file():
        parser.error("Repository root must contain the GitHub Copilot integration")
    if output == root or root in output.parents:
        parser.error("Output must be outside the repository")
    if not 1024 <= args.port <= 65535:
        parser.error("Port must be between 1024 and 65535")

    try:
        output.mkdir(parents=True, exist_ok=False)
        components = output / "custom_components"
        shutil.copytree(
            source,
            components / "github_copilot",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        (components / "__init__.py").write_text("", encoding="utf-8")
        (components / "github_copilot" / "__init__.py").write_text(
            RUNTIME, encoding="utf-8"
        )
        (output / "configuration.yaml").write_text(
            dedent(f"""\
            homeassistant:
              name: Copilot Screenshot
              unit_system: metric
              time_zone: UTC
              currency: USD
            http:
              server_host: 127.0.0.1
              server_port: {args.port}
            frontend:
            config:
            screenshot_seed:
            """),
            encoding="utf-8",
        )
        seed = components / "screenshot_seed"
        seed.mkdir()
        (seed / "manifest.json").write_text(
            json.dumps(
                {
                    "domain": "screenshot_seed",
                    "name": "Screenshot seed",
                    "version": "1.0.0",
                    "codeowners": [],
                    "documentation": "https://example.invalid",
                    "iot_class": "local_push",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (seed / "__init__.py").write_text(SEED, encoding="utf-8")
    except OSError as err:
        parser.exit(1, f"Could not prepare screenshot fixture: {err}\n")

    print(f"Prepared disposable config: {output}")
    print(f"Local UI: http://127.0.0.1:{args.port}/")
    print("Start with:")
    print(
        shlex.join(
            [
                sys.executable,
                "-m",
                "homeassistant",
                "--config",
                str(output),
                "--skip-pip",
            ]
        )
    )


if __name__ == "__main__":
    main()
