"""Opt-in Linux resource probe for image preparation plus the pinned SDK.

Run in a disposable, network-disabled ARM64 container with one CPU and 512 MiB
of memory. This is not a Home Assistant server or a real inference benchmark.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import platform
import resource
import struct
import sys
import time
import zlib
from pathlib import Path
from types import ModuleType, SimpleNamespace


def create_fixtures(directory: Path) -> None:
    from PIL import Image

    directory.mkdir(parents=True, exist_ok=True)
    for name, dimensions in (("small", (32, 32)), ("full_hd", (1920, 1080))):
        with Image.new("RGB", dimensions, (20, 40, 60)) as image:
            image.save(directory / f"{name}.png")
            if name == "small":
                image.save(directory / "small.gif")
                image.save(directory / "small.webp")
    for index in range(3):
        with Image.frombytes("RGB", (1280, 720), os.urandom(1280 * 720 * 3)) as image:
            image.save(directory / f"noise_{index}.png")
            image.save(directory / f"camera_{index}.jpg", quality=75)
    with Image.frombytes("RGB", (1254, 1254), os.urandom(1254 * 1254 * 3)) as image:
        image.save(directory / "output.png")
    with Image.frombytes("RGBA", (1254, 1254), os.urandom(1254 * 1254 * 4)) as image:
        image.save(directory / "oversized_output.png")
    header = bytearray((directory / "small.png").read_bytes())
    header[16:24] = struct.pack(">II", 4096, 2048)
    header[29:33] = struct.pack(">I", zlib.crc32(header[12:29]))
    (directory / "oversized_header.png").write_bytes(header)


def memory() -> dict[str, int]:
    root = Path("/sys/fs/cgroup")
    return {
        "cgroup_current_bytes": int((root / "memory.current").read_text()),
        "cgroup_peak_bytes": int((root / "memory.peak").read_text()),
        "python_peak_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        **{
            f"memory_events_{key}": int(value)
            for key, value in (
                line.split()
                for line in (root / "memory.events").read_text().splitlines()
            )
        },
    }


def cpu_seconds() -> float:
    values = dict(
        line.split()
        for line in Path("/sys/fs/cgroup/cpu.stat").read_text().splitlines()
    )
    return int(values["usage_usec"]) / 1_000_000


async def probe(fixtures: Path, runtime: Path) -> None:
    assert platform.system() == "Linux" and platform.machine() == "aarch64"
    memory_limit = int(Path("/sys/fs/cgroup/memory.max").read_text())
    quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
    assert memory_limit <= 512 * 1024 * 1024 and int(quota) <= int(period)

    # Measure the actual image helper without loading an unrelated HA server.
    ha = ModuleType("homeassistant")
    ha.__path__ = []
    errors = ModuleType("homeassistant.exceptions")
    errors.HomeAssistantError = type("HomeAssistantError", (Exception,), {})
    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.exceptions"] = errors

    from aiohttp import web
    from copilot import CopilotClient
    from copilot.client import RuntimeConnection
    from copilot.generated.rpc import PermissionDecisionReject, ToolsExecuteRequest
    from copilot.session import ModelCapabilitiesOverride, ModelSupportsOverride
    from copilot.tools import Tool, ToolBinaryResult, ToolResult

    from custom_components.github_copilot.images import (
        ImageResultCollector,
        prepare_images,
    )

    requests: list[int] = []

    async def complete(request):
        marker = b"data:image/"
        tail = b""
        count = 0
        # Do not retain another copy of the large request just for measurement.
        async for chunk in request.content.iter_chunked(65536):
            data = tail + chunk
            count += data.count(marker)
            tail = data[-(len(marker) - 1) :]
        requests.append(count)
        event = {
            "id": "fixture",
            "object": "chat.completion.chunk",
            "model": "gpt-4.1",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
        }
        return web.Response(
            text=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n",
            content_type="text/event-stream",
        )

    app = web.Application(client_max_size=32 * 1024 * 1024)
    app.router.add_post("/v1/chat/completions", complete)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    client = CopilotClient(
        connection=RuntimeConnection.for_stdio(path=str(runtime)),
        base_directory="/tmp/copilot-image-probe",
        working_directory="/tmp",
        use_logged_in_user=False,
        mode="empty",
        log_level="error",
    )
    model = SimpleNamespace(
        capabilities=SimpleNamespace(
            supports=SimpleNamespace(vision=True),
            limits=SimpleNamespace(
                vision=SimpleNamespace(
                    supported_media_types=[
                        "image/png",
                        "image/jpeg",
                        "image/gif",
                        "image/webp",
                    ],
                    max_prompt_images=8,
                    max_prompt_image_size=20 * 1024 * 1024,
                )
            ),
        )
    )
    cases = [
        ("small", ["small.png"], True),
        ("static_gif", ["small.gif"], True),
        ("static_webp", ["small.webp"], True),
        ("full_hd", ["full_hd.png"], True),
        ("dense_png", ["noise_0.png"], False),
        ("camera_one", ["camera_0.jpg"], True),
        ("camera_two", ["camera_0.jpg", "camera_1.jpg"], True),
        ("camera_three", ["camera_0.jpg", "camera_1.jpg", "camera_2.jpg"], False),
        ("oversized_header", ["oversized_header.png"], False),
    ]
    failure = None
    try:
        await client.start()
        print(
            json.dumps(
                {
                    "phase": "baseline",
                    "runtime": (await client.get_status()).version,
                    "platform": platform.platform(),
                    "memory_limit": memory_limit,
                    "cpu_quota": quota,
                    "cpu_period": period,
                    **memory(),
                }
            ),
            flush=True,
        )
        for name, filenames, expected in cases:
            start, cpu_start = time.monotonic(), cpu_seconds()
            before = memory()
            count_before = len(requests)
            try:
                attachments = await asyncio.to_thread(
                    prepare_images,
                    [
                        SimpleNamespace(
                            path=fixtures / filename,
                            mime_type={
                                ".jpg": "image/jpeg",
                                ".png": "image/png",
                                ".gif": "image/gif",
                                ".webp": "image/webp",
                            }[Path(filename).suffix],
                        )
                        for filename in filenames
                    ],
                    model,
                )
            except errors.HomeAssistantError as error:
                assert not expected, f"Unexpected rejection in {name}: {error}"
                print(
                    json.dumps(
                        {
                            "case": name,
                            "admitted": False,
                            "reason": str(error),
                            "seconds": time.monotonic() - start,
                            **memory(),
                        }
                    ),
                    flush=True,
                )
                assert len(requests) == count_before
                continue
            assert expected, f"Unsafe fixture was admitted: {name}"
            prepared = memory()
            session = await client.create_session(
                model="gpt-4.1",
                provider={
                    "type": "openai",
                    "wire_api": "completions",
                    "base_url": f"http://127.0.0.1:{port}/v1",
                    "api_key": "local-fixture",
                },
                model_capabilities=ModelCapabilitiesOverride(
                    supports=ModelSupportsOverride(vision=True)
                ),
                on_permission_request=lambda *_: PermissionDecisionReject(
                    feedback="Resource probe denies all permissions"
                ),
                available_tools=[],
                image_generation={"enabled": False, "save_to_disk": False},
                enable_config_discovery=False,
                enable_file_hooks=False,
                enable_host_git_operations=False,
                enable_session_store=False,
                enable_skills=False,
                enable_session_telemetry=False,
                skip_custom_instructions=True,
                streaming=True,
            )
            try:
                await session.send_and_wait(
                    "Inspect the supplied synthetic test images.",
                    attachments=attachments,
                    timeout=30,
                )
                assert len(requests) == count_before + 1
                assert requests[-1] == len(filenames)
            finally:
                try:
                    await session.abort()
                    await session.disconnect()
                    await client.delete_session(session.session_id)
                except Exception as error:
                    print(
                        json.dumps(
                            {
                                "phase": "session_cleanup_failed",
                                "case": name,
                                "error_type": type(error).__name__,
                                **memory(),
                            }
                        ),
                        flush=True,
                    )
                    raise
            del attachments
            print(
                json.dumps(
                    {
                        "case": name,
                        "admitted": True,
                        "images": len(filenames),
                        "seconds": time.monotonic() - start,
                        "cpu_seconds": cpu_seconds() - cpu_start,
                        "before": before,
                        "prepared": prepared,
                        "after": memory(),
                    }
                ),
                flush=True,
            )
        # A custom binary tool adds an extra SDK round trip compared with native
        # generation, providing a conservative output-transport fixture.
        for filename, expected in (
            ("output.png", True),
            ("oversized_output.png", False),
        ):
            raw_output = (fixtures / filename).read_bytes()
            encoded_output = base64.b64encode(raw_output).decode("ascii")
            before, start, cpu_start = memory(), time.monotonic(), cpu_seconds()
            session = await client.create_session(
                on_permission_request=lambda *_: PermissionDecisionReject(
                    feedback="No unrelated permissions in the output fixture"
                ),
                tools=[
                    Tool(
                        name="image_fixture",
                        description="Return a synthetic PNG fixture",
                        parameters={
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                        handler=lambda _, data=encoded_output: ToolResult(
                            text_result_for_llm="Image fixture",
                            binary_results_for_llm=[
                                ToolBinaryResult(
                                    data=data,
                                    mime_type="image/png",
                                )
                            ],
                        ),
                        skip_permission=True,
                    )
                ],
                available_tools=["custom:image_fixture"],
                enable_config_discovery=False,
                enable_file_hooks=False,
                enable_host_git_operations=False,
                enable_session_store=False,
                enable_skills=False,
                enable_session_telemetry=False,
                skip_custom_instructions=True,
            )
            try:
                result = await session.rpc.tools.execute(
                    ToolsExecuteRequest(name="image_fixture", arguments={}), timeout=30
                )
                try:
                    image = await asyncio.to_thread(
                        ImageResultCollector().decode, result
                    )
                except errors.HomeAssistantError:
                    assert not expected
                else:
                    assert expected and image.data == raw_output
                    del image
            finally:
                await session.disconnect()
                await client.delete_session(session.session_id)
            print(
                json.dumps(
                    {
                        "case": filename,
                        "bytes": len(raw_output),
                        "admitted": expected,
                        "seconds": time.monotonic() - start,
                        "cpu_seconds": cpu_seconds() - cpu_start,
                        "before": before,
                        "after": memory(),
                    }
                ),
                flush=True,
            )
            del result, raw_output, encoded_output, session
    except BaseException as error:
        failure = error
        print(
            json.dumps(
                {
                    "phase": "failed",
                    "error_type": type(error).__name__,
                    **memory(),
                }
            ),
            flush=True,
        )
        raise
    finally:
        try:
            async with asyncio.timeout(15):
                await client.stop()
        except Exception as error:
            print(
                json.dumps(
                    {
                        "phase": "client_cleanup_failed",
                        "error_type": type(error).__name__,
                    }
                ),
                flush=True,
            )
            await client.force_stop()
            if failure is None:
                raise
        await runner.cleanup()
    final = memory()
    assert final["memory_events_oom"] == final["memory_events_oom_kill"] == 0
    assert final["cgroup_peak_bytes"] <= memory_limit
    print(json.dumps({"phase": "complete", **final}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--make-fixtures", type=Path)
    parser.add_argument("--fixtures", type=Path)
    parser.add_argument("--runtime", type=Path)
    args = parser.parse_args()
    if args.make_fixtures is not None:
        create_fixtures(args.make_fixtures)
    else:
        assert args.fixtures is not None and args.runtime is not None
        asyncio.run(asyncio.wait_for(probe(args.fixtures, args.runtime), timeout=120))
