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
except (TimeoutError, ConnectionError):
    sys.exit(5)
except OSError:
    sys.exit(3)
except RuntimeError as error:
    message = str(error).lower()
    if "checksum mismatch" in message:
        sys.exit(4)
    sys.exit(5 if message.startswith("failed to download") else 1)
"""
_INSTALL_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONUTF8",
        "PYTHONIOENCODING",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "SYSTEMROOT",
        "WINDIR",
        "TMPDIR",
        "TMP",
        "TEMP",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "COPILOT_CLI_DOWNLOAD_BASE_URL",
        "COPILOT_SKIP_CLI_DOWNLOAD",
    }
)
_ERRORS = {
    2: "The Copilot SDK does not support this OS or CPU architecture.",
    3: "Cannot write or execute the Copilot runtime cache. Check storage permissions.",
    4: "Copilot runtime checksum verification failed. The download was rejected.",
    5: "Could not download the pinned Copilot runtime. Check connectivity to GitHub.",
}


class RuntimeDownloadError(RuntimeError):
    """A transient failure downloading the pinned runtime."""


async def async_runtime_path(hass: HomeAssistant) -> str:
    """Return a verified runtime in HA's persistent, version-specific cache."""
    from copilot._cli_version import CLI_VERSION

    cache = Path(hass.config.path(".storage", DOMAIN, "runtime", CLI_VERSION))
    lock = hass.data.setdefault(f"{DOMAIN}_provision_lock", asyncio.Lock())
    async with lock:
        # Preserve operator transport settings, including lowercase proxy variables.
        safe_env = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in _INSTALL_ENV_ALLOWLIST
        }
        safe_env["COPILOT_CLI_EXTRACT_DIR"] = str(cache)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _INSTALL_SCRIPT,
            env=safe_env,
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
            error_type = (
                RuntimeDownloadError if process.returncode == 5 else RuntimeError
            )
            raise error_type(
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
