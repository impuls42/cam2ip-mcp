"""Smoke tests against a built container image.

Skipped unless CAM2IP_MCP_IMAGE names an image that is already built and loaded
into the local Docker daemon:

    docker build -f Containerfile -t cam2ip-mcp:dev .
    CAM2IP_MCP_IMAGE=cam2ip-mcp:dev python -m pytest tests/test_image.py

These cover what the other tests structurally cannot: that the *image* works.
A Containerfile that builds is not the same as an image that runs -- the pinned
dependencies have to resolve inside python:3.12-alpine, the cam2ip binary has to
execute in a runtime stage with no build tools, and the entrypoint has to hand a
working stdin to the MCP server. `docker run -i` is how MCP clients launch this
container, and it is exactly the path that was broken.

Uses --network host so the container can reach the fake cam2ip running in this
process, which means these are Linux-only (Docker Desktop does not share the
host network namespace the same way).
"""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import subprocess
import sys

import pytest
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client

from fake_cam2ip import read_frame_meta, running_fake_cam2ip

IMAGE = os.environ.get("CAM2IP_MCP_IMAGE")

pytestmark = [
    pytest.mark.skipif(not IMAGE, reason="set CAM2IP_MCP_IMAGE to a built image"),
    pytest.mark.skipif(shutil.which("docker") is None, reason="needs the docker CLI"),
    pytest.mark.skipif(
        sys.platform != "linux", reason="needs --network host to reach the fake camera"
    ),
]


def docker_run_args(base_url: str, **env) -> list[str]:
    settings = {
        "CAM2IP_ENABLED": "false",  # no webcam on a CI runner
        "CAM2IP_BASE_URL": base_url,
        "MCP_MODE": "stdio",
        "MCP_GRAB_TIMEOUT_S": "20",
    }
    settings.update({key: str(value) for key, value in env.items()})

    args = ["run", "--rm", "-i", "--network", "host"]
    for key, value in settings.items():
        args += ["-e", f"{key}={value}"]
    return args + [IMAGE]


class TestDiagnostics:
    """The cam2ip binary has to actually execute in the runtime stage.

    Built with CGO_ENABLED=0 so it should be static, but a broken build shows up
    here as a loader error rather than as a mystery at first frame.
    """

    def test_cam2ip_reports_its_version(self):
        result = subprocess.run(
            ["docker", "run", "--rm", IMAGE, "--version"],
            capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, result.stderr
        assert "cam2ip" in result.stdout

    def test_list_devices_runs(self):
        """Finds no cameras on a runner, but must exit cleanly rather than crash."""
        result = subprocess.run(
            ["docker", "run", "--rm", IMAGE, "--list-devices"],
            capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, result.stderr


class TestStdioInContainer:
    """`docker run -i` is how MCP clients launch this, and it was broken."""

    async def test_grab_frame_returns_a_fresh_image(self):
        async with running_fake_cam2ip() as fake:
            watermark = await fake.queue.wait_until_stalled()
            await asyncio.sleep(0.4)

            params = StdioServerParameters(
                command="docker",
                args=docker_run_args(fake.base_url),
                env=dict(os.environ),
            )
            async with Client(stdio_client(params)) as client:
                tools = {tool.name for tool in (await client.list_tools()).tools}
                result = await client.call_tool("grab_frame", {})

        assert tools == {"grab_frame", "camera_status"}
        assert not result.is_error, result.content
        images = [block for block in result.content if block.type == "image"]
        frame = base64.b64decode(images[0].data)
        assert read_frame_meta(frame)["seq"] > watermark

    async def test_camera_status_round_trips(self):
        async with running_fake_cam2ip() as fake:
            params = StdioServerParameters(
                command="docker",
                args=docker_run_args(fake.base_url),
                env=dict(os.environ),
            )
            async with Client(stdio_client(params)) as client:
                await client.call_tool("grab_frame", {})
                result = await client.call_tool("camera_status", {})

        assert not result.is_error, result.content
