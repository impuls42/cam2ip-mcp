"""End-to-end tests over the real MCP transports.

These launch the server as a subprocess exactly the way a client would, so they
cover the wiring the unit tests cannot: env-var configuration, the tool schemas,
base64 image content, and -- for stdio -- that nothing pollutes stdout, which is
the JSON-RPC channel.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import sys
from contextlib import asynccontextmanager, closing
from pathlib import Path

import httpx2
import pytest
from mcp import Client, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client

from fake_cam2ip import read_frame_meta, running_fake_cam2ip

SERVER = str(Path(__file__).resolve().parent.parent / "cam2mcp_server.py")


def free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def server_env(base_url: str, **overrides) -> dict[str, str]:
    env = {
        **os.environ,
        "CAM2IP_BASE_URL": base_url,
        "MCP_GRAB_TIMEOUT_S": "10",
        "MCP_LOG_LEVEL": "WARNING",
    }
    env.update({key: str(value) for key, value in overrides.items()})
    return env


@asynccontextmanager
async def stdio_session(base_url: str, **overrides):
    params = StdioServerParameters(
        command=sys.executable,
        args=[SERVER],
        env=server_env(base_url, MCP_MODE="stdio", **overrides),
    )
    async with Client(stdio_client(params)) as client:
        yield client


@asynccontextmanager
async def http_session(base_url: str, *, mode: str = "streamable-http", **overrides):
    port = free_port()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        SERVER,
        env=server_env(
            base_url, MCP_MODE=mode, MCP_HTTP_HOST="127.0.0.1", MCP_HTTP_PORT=port, **overrides
        ),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await _wait_for_port(port, process)
        if mode == "sse":
            async with Client(sse_client(f"http://127.0.0.1:{port}/sse")) as client:
                yield client
        else:
            async with Client(f"http://127.0.0.1:{port}/mcp") as client:
                yield client
    finally:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except asyncio.TimeoutError:  # pragma: no cover
            process.kill()
            await process.wait()


async def _wait_for_port(port: int, process, timeout: float = 20.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if process.returncode is not None:
            raise AssertionError(f"server exited early with code {process.returncode}")
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.05)
            continue
        writer.close()
        await writer.wait_closed()
        return
    raise AssertionError(f"server did not listen on port {port} within {timeout}s")


def image_bytes(result) -> bytes:
    """Pull the single image out of a tool result."""
    images = [block for block in result.content if block.type == "image"]
    assert len(images) == 1, f"expected one image, got {result.content}"
    assert images[0].mime_type == "image/jpeg"
    return base64.b64decode(images[0].data)


class TestStdio:
    """The default transport, and the one where stdout must stay clean."""

    async def test_lists_both_tools(self):
        async with running_fake_cam2ip() as fake:
            async with stdio_session(fake.base_url) as client:
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}

        assert set(tools) == {"grab_frame", "camera_status"}
        assert "max_age_s" in tools["grab_frame"].input_schema["properties"]
        assert tools["grab_frame"].input_schema.get("required", []) == []

    async def test_grab_frame_returns_a_fresh_image(self):
        async with running_fake_cam2ip() as fake:
            watermark = await fake.queue.wait_until_stalled()
            await asyncio.sleep(0.4)

            async with stdio_session(fake.base_url) as client:
                result = await client.call_tool("grab_frame", {})

        assert not result.is_error, result.content
        meta = read_frame_meta(image_bytes(result))
        assert meta["seq"] > watermark

    async def test_grab_frame_accepts_max_age(self):
        async with running_fake_cam2ip() as fake:
            async with stdio_session(fake.base_url) as client:
                result = await client.call_tool("grab_frame", {"max_age_s": 2.0})

        assert not result.is_error, result.content
        assert image_bytes(result).startswith(b"\xff\xd8")

    async def test_camera_status_reports_the_stream(self):
        async with running_fake_cam2ip() as fake:
            async with stdio_session(fake.base_url) as client:
                await client.call_tool("grab_frame", {})
                result = await client.call_tool("camera_status", {})

        assert not result.is_error, result.content
        status = json.loads(result.content[0].text)
        assert status["stream_connected"] is True
        assert status["frames_published"] >= 1
        assert status["cam2ip_url"] == fake.base_url

    async def test_reports_an_error_when_the_camera_is_unreachable(self):
        async with stdio_session("http://127.0.0.1:1", MCP_GRAB_TIMEOUT_S="1") as client:
            result = await client.call_tool("grab_frame", {})

        assert result.is_error
        assert "no frame newer than" in result.content[0].text


class TestStreamableHttp:
    async def test_grab_frame_returns_a_fresh_image(self):
        async with running_fake_cam2ip() as fake:
            watermark = await fake.queue.wait_until_stalled()
            await asyncio.sleep(0.4)

            async with http_session(fake.base_url) as client:
                result = await client.call_tool("grab_frame", {})

        assert not result.is_error, result.content
        meta = read_frame_meta(image_bytes(result))
        assert meta["seq"] > watermark

    async def test_repeated_calls_on_one_session_stay_fresh(self):
        async with running_fake_cam2ip() as fake:
            async with http_session(fake.base_url, MCP_FRAME_MAX_AGE_S="0.2") as client:
                seqs = []
                for _ in range(3):
                    result = await client.call_tool("grab_frame", {})
                    assert not result.is_error, result.content
                    seqs.append(read_frame_meta(image_bytes(result))["seq"])
                    await asyncio.sleep(0.3)

        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), seqs


class TestSse:
    """The legacy transport. Advertised in the README, so it has to work.

    Its wiring differs from Streamable HTTP -- a separate endpoint pair, and its
    own transport_security path -- so it can regress independently.
    """

    async def test_grab_frame_returns_a_fresh_image(self):
        async with running_fake_cam2ip() as fake:
            watermark = await fake.queue.wait_until_stalled()
            await asyncio.sleep(0.4)

            async with http_session(fake.base_url, mode="sse") as client:
                tools = {tool.name for tool in (await client.list_tools()).tools}
                result = await client.call_tool("grab_frame", {})

        assert tools == {"grab_frame", "camera_status"}
        assert not result.is_error, result.content
        meta = read_frame_meta(image_bytes(result))
        assert meta["seq"] > watermark

    async def test_honours_a_host_allowlist(self):
        """MCP_ALLOWED_HOSTS reaches the SSE transport, not just streamable-http.

        Rejection lands on the transport, before a session exists: the server
        answers the connect with 421 rather than letting a call through to fail.
        """
        async with running_fake_cam2ip() as fake:
            with pytest.raises(httpx2.HTTPStatusError) as excinfo:
                async with http_session(
                    fake.base_url, mode="sse", MCP_ALLOWED_HOSTS="example.invalid"
                ) as client:
                    await client.call_tool("grab_frame", {})

        # The client connects as 127.0.0.1, which the allowlist excludes.
        assert excinfo.value.response.status_code == 421


class TestAllowlists:
    """An allowlist that locks out the operator is worse than none at all.

    mcp replaces its own defaults as soon as any settings are passed, and then
    checks Host and Origin together, with no allowlist entry meaning "any host"
    ("*" matches only a literal "*" Host header). So a half-specified allowlist
    is not a loose one, it is a closed door.
    """

    async def test_a_matching_host_is_admitted(self):
        """The permitting side, so the tests are not all about rejection."""
        async with running_fake_cam2ip() as fake:
            async with http_session(
                fake.base_url, MCP_ALLOWED_HOSTS="127.0.0.1:*,localhost:*"
            ) as client:
                result = await client.call_tool("grab_frame", {})

        assert not result.is_error, result.content
        assert image_bytes(result).startswith(b"\xff\xd8")

    @pytest.mark.parametrize("mode", ["streamable-http", "sse"])
    async def test_a_non_matching_host_is_refused(self, mode):
        """No session can be established at all.

        The exception type is deliberately not pinned: SSE surfaces the 421 as an
        httpx HTTPStatusError, while streamable-http's probe turns it into an
        MCPError. What makes this specific to the allowlist rather than to any
        old connection failure is the contrast with the matching-host test above,
        which is identical apart from the allowlist value.
        """
        async with running_fake_cam2ip() as fake:
            with pytest.raises(Exception):  # noqa: B017 - see docstring
                async with http_session(
                    fake.base_url, mode=mode, MCP_ALLOWED_HOSTS="example.invalid"
                ) as client:
                    await client.call_tool("grab_frame", {})

    async def test_origins_without_hosts_is_refused_at_startup(self):
        """Setting only origins used to start cleanly and 421 every request.

        The host allowlist ends up empty, which rejects everything -- and it also
        overrides the loopback allowlist mcp would otherwise have applied, so the
        setting that was meant to loosen things silently sealed the server shut.
        Now it fails at startup, naming the variable that is missing.
        """
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            SERVER,
            env=server_env(
                "http://127.0.0.1:1",
                MCP_MODE="streamable-http",
                MCP_HTTP_HOST="127.0.0.1",
                MCP_HTTP_PORT=free_port(),
                MCP_ALLOWED_ORIGINS="https://app.example.com",
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        message = stderr.decode()

        assert process.returncode != 0
        assert "MCP_ALLOWED_HOSTS" in message
        assert "Traceback" not in message

    async def test_both_together_are_accepted(self):
        async with running_fake_cam2ip() as fake:
            async with http_session(
                fake.base_url,
                MCP_ALLOWED_HOSTS="127.0.0.1:*",
                MCP_ALLOWED_ORIGINS="https://app.example.com",
            ) as client:
                result = await client.call_tool("grab_frame", {})

        # No Origin header from this client, which mcp permits.
        assert not result.is_error, result.content

    async def test_stdio_ignores_the_allowlist(self):
        """The variables are HTTP-only; a stdio server must not trip over them."""
        async with running_fake_cam2ip() as fake:
            async with stdio_session(
                fake.base_url, MCP_ALLOWED_ORIGINS="https://app.example.com"
            ) as client:
                result = await client.call_tool("grab_frame", {})

        assert not result.is_error, result.content


class TestBadConfiguration:
    @pytest.mark.parametrize(
        "env,expected",
        [
            ({"MCP_MODE": "carrier-pigeon"}, "MCP_MODE"),
            ({"MCP_FRAME_MAX_AGE_S": "soon"}, "MCP_FRAME_MAX_AGE_S"),
            ({"MCP_WARMUP_FRAMES": "-3"}, "MCP_WARMUP_FRAMES"),
        ],
    )
    async def test_exits_with_a_clear_message(self, env, expected):
        """A typo in configuration should say which variable, not raise a traceback."""
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            SERVER,
            env=server_env("http://127.0.0.1:1", **env),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=30)

        assert process.returncode != 0
        assert expected in stderr.decode()
        assert "Traceback" not in stderr.decode()
