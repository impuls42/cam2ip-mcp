"""Tests for entrypoint.sh, the container startup path.

Run against a stand-in `cam2ip` on PATH rather than a real webcam. The point is
the shell plumbing: whether the MCP server ends up with a usable stdin, whether
anything leaks onto stdout, and whether a cam2ip that fails to start is
reported instead of hung on.
"""

from __future__ import annotations

import asyncio
import base64
import os
import signal
import socket
import stat
import sys
from contextlib import closing
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client

from fake_cam2ip import read_frame_meta

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = REPO_ROOT / "entrypoint.sh"
FAKE_CAM2IP = Path(__file__).resolve().parent / "fake_cam2ip.py"

pytestmark = pytest.mark.skipif(
    not os.path.exists("/bin/sh"), reason="needs a POSIX shell"
)


def free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def write_shim(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    """A PATH entry providing the `python` that has this run's dependencies.

    In the image `python` is the interpreter with requirements.txt installed;
    here it has to be pointed at the test environment's interpreter instead.
    """
    path = tmp_path / "bin"
    path.mkdir()
    write_shim(path / "python", f'exec {sys.executable} "$@"')
    return path


@pytest.fixture
def shim_bin(bin_dir: Path) -> Path:
    """Adds a `cam2ip` that serves the fake camera."""
    write_shim(bin_dir / "cam2ip", f'exec {sys.executable} {FAKE_CAM2IP} "$@"')
    return bin_dir


@pytest.fixture
def failing_shim_bin(bin_dir: Path) -> Path:
    """Adds a `cam2ip` that dies at startup, as it does with no camera attached."""
    write_shim(bin_dir / "cam2ip", "echo 'camera: no camera at index 0' >&2\nexit 1")
    return bin_dir


def entrypoint_env(bin_dir: Path, port: int, **overrides) -> dict[str, str]:
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "MCP_SERVER_PATH": str(REPO_ROOT / "cam2ip_mcp_server.py"),
        "CAM2IP_BIND_ADDR": f"127.0.0.1:{port}",
        "CAM2IP_BASE_URL": f"http://127.0.0.1:{port}",
        "MCP_MODE": "stdio",
        "MCP_GRAB_TIMEOUT_S": "10",
        "MCP_LOG_LEVEL": "WARNING",
    }
    env.update({key: str(value) for key, value in overrides.items()})
    return env


class TestStdioTransportThroughEntrypoint:
    """The bug: `python server.py &` in POSIX sh gets stdin=/dev/null.

    A background MCP server sees EOF immediately and exits, so stdio mode was
    dead on arrival whenever the entrypoint also started cam2ip -- the default.
    If these pass, the server is being exec'd in the foreground with the
    container's real stdin.
    """

    async def test_grab_frame_works_end_to_end(self, shim_bin):
        port = free_port()
        params = StdioServerParameters(
            command="/bin/sh",
            args=[str(ENTRYPOINT)],
            env=entrypoint_env(shim_bin, port),
        )

        async with Client(stdio_client(params)) as client:
            tools = {tool.name for tool in (await client.list_tools()).tools}
            result = await client.call_tool("grab_frame", {})

        assert tools == {"grab_frame", "camera_status"}
        assert not result.is_error, result.content
        images = [block for block in result.content if block.type == "image"]
        frame = base64.b64decode(images[0].data)
        assert read_frame_meta(frame)["seq"] > 0

    async def test_startup_chatter_stays_off_stdout(self, shim_bin):
        """Any log line on stdout would corrupt the JSON-RPC stream."""
        port = free_port()
        process = await asyncio.create_subprocess_exec(
            "/bin/sh",
            str(ENTRYPOINT),
            env=entrypoint_env(shim_bin, port),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Own process group, so teardown can take the backgrounded cam2ip
            # with it -- otherwise it holds the stderr pipe open and wait() hangs.
            start_new_session=True,
        )
        try:
            # Wait for startup to say everything it is going to say.
            stderr = await read_until(process.stderr, b"starting MCP server", timeout=20)
            assert b"starting cam2ip" in stderr
            assert process.returncode is None, "server should still be running"

            # No request has been sent, so a clean stdout is still empty.
            stdout = await read_until(process.stdout, b"\n", timeout=1.0)
            assert stdout == b"", f"entrypoint wrote {stdout!r} to stdout"
        finally:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            await asyncio.wait_for(process.wait(), timeout=10)


class TestCam2ipStartupFailure:
    async def test_fails_fast_with_a_clear_message(self, failing_shim_bin):
        """A camera-less cam2ip must be reported, not waited on for 30s."""
        port = free_port()
        process = await asyncio.create_subprocess_exec(
            "/bin/sh",
            str(ENTRYPOINT),
            env=entrypoint_env(failing_shim_bin, port),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)

        assert process.returncode == 1
        assert b"cam2ip exited during startup" in stderr
        assert b"no camera at index 0" in stderr  # cam2ip's own reason survives
        assert stdout == b""


class TestExternalCam2ip:
    async def test_skips_startup_when_disabled(self, bin_dir):
        """CAM2IP_ENABLED=false must not need a cam2ip binary on PATH at all."""
        port = free_port()
        params = StdioServerParameters(
            command="/bin/sh",
            args=[str(ENTRYPOINT)],
            env=entrypoint_env(
                bin_dir, port, CAM2IP_ENABLED="false", MCP_GRAB_TIMEOUT_S="1"
            ),
        )

        async with Client(stdio_client(params)) as client:
            # The server is up and answering even with no camera behind it.
            result = await client.call_tool("camera_status", {})

        assert not result.is_error, result.content


class TestDiagnosticPassthrough:
    async def test_flags_are_forwarded_to_cam2ip(self, bin_dir):
        """`docker run <image> --list-devices` should reach cam2ip, not the server."""
        write_shim(bin_dir / "cam2ip", 'echo "cam2ip got: $*"')

        process = await asyncio.create_subprocess_exec(
            "/bin/sh",
            str(ENTRYPOINT),
            "--list-devices",
            env=entrypoint_env(bin_dir, free_port()),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)

        assert process.returncode == 0
        assert stdout.strip() == b"cam2ip got: --list-devices"

    async def test_healthcheck_probes_cam2ip(self, shim_bin):
        port = free_port()
        env = entrypoint_env(shim_bin, port)

        # Nothing listening yet, so the probe must fail.
        failing = await asyncio.create_subprocess_exec(
            "/bin/sh", str(ENTRYPOINT), "healthcheck", env=env,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        assert await asyncio.wait_for(failing.wait(), timeout=30) != 0

        # With cam2ip up it must succeed -- without consuming a frame.
        camera = await asyncio.create_subprocess_exec(
            sys.executable, str(FAKE_CAM2IP), env=env,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await _wait_for_port(port)
            passing = await asyncio.create_subprocess_exec(
                "/bin/sh", str(ENTRYPOINT), "healthcheck", env=env,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            assert await asyncio.wait_for(passing.wait(), timeout=30) == 0
        finally:
            camera.terminate()
            await asyncio.wait_for(camera.wait(), timeout=10)


async def read_until(stream, needle: bytes, timeout: float) -> bytes:
    """Collect from stream until needle shows up, EOF, or the timeout expires."""
    collected = bytearray()

    async def pump() -> None:
        while needle not in collected:
            chunk = await stream.read(256)
            if not chunk:
                return
            collected.extend(chunk)

    try:
        await asyncio.wait_for(pump(), timeout)
    except asyncio.TimeoutError:
        pass
    return bytes(collected)


async def _wait_for_port(port: int, timeout: float = 20.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.05)
            continue
        writer.close()
        await writer.wait_closed()
        return
    raise AssertionError(f"nothing listening on {port} within {timeout}s")
