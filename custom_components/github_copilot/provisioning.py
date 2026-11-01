"""Provision the SDK-pinned runtime without changing HA's process environment."""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

# The pinned SDK only exposes its cache override through environment variables.
# Run its own checksum-verified installer in a child, never mutate HA's os.environ.
_INSTALL_SCRIPT = """
import sys
from copilot._cli_version import get_runtime_platform
from copilot._cli_download import ensure_runtime_wrapper
try:
    get_runtime_platform()
except RuntimeError:
    sys.exit(2)
try:
    print(ensure_runtime_wrapper(), flush=True)
except OSError:
    sys.exit(3)
except RuntimeError as error:
    sys.exit(4 if "checksum" in str(error).lower() else 5)
"""
_ERRORS = {
    2: "The Copilot SDK does not support this OS or CPU architecture.",
    3: "Cannot write or execute the Copilot runtime cache. Check storage permissions.",
    4: "Copilot runtime checksum verification failed. The download was rejected.",
    5: "Could not download the pinned Copilot runtime. Check connectivity to GitHub.",
}


async def async_runtime_path(hass: HomeAssistant) -> str:
    """Return a verified runtime in HA's persistent, version-specific cache."""
    from copilot._cli_version import CLI_VERSION

    cache = Path(hass.config.path(".storage", DOMAIN, "runtime", CLI_VERSION))
    lock = hass.data.setdefault(f"{DOMAIN}_provision_lock", asyncio.Lock())
    async with lock:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _INSTALL_SCRIPT,
            env={**os.environ, "COPILOT_CLI_EXTRACT_DIR": str(cache)},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=300)
        except (TimeoutError, asyncio.CancelledError):
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise RuntimeError(
                _ERRORS.get(
                    process.returncode,
                    "The Copilot runtime installer failed. Check the SDK installation.",
                )
            )
        runtime_path = Path(output.decode().strip())
        if not runtime_path.is_file() or not runtime_path.resolve().is_relative_to(
            cache.resolve()
        ):
            raise RuntimeError("The SDK returned an invalid Copilot runtime path.")
        return str(runtime_path)
