"""Tests for cam2ip_probe.py, the reachability probe behind startup and HEALTHCHECK.

The address logic is what needs covering here. Probing the wrong address is
indistinguishable from cam2ip being down, and both of its consumers turn that
into a hard failure -- the container refuses to start, or reports unhealthy
forever -- while cam2ip is perfectly fine.
"""

from __future__ import annotations

import asyncio
import socket
import sys
from contextlib import closing

import pytest

from cam2ip_probe import main, target_from_host_port, target_from_url


class TestTargetFromUrl:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("http://127.0.0.1:56000", ("127.0.0.1", 56000)),
            ("http://192.168.1.5:56000", ("192.168.1.5", 56000)),
            ("http://camera.local:8080/", ("camera.local", 8080)),
            ("http://camera.local", ("camera.local", 80)),
            ("https://camera.local", ("camera.local", 443)),
            ("https://camera.local:8443/path", ("camera.local", 8443)),
            # A bare host:port is a plausible thing to find in a URL variable.
            ("127.0.0.1:56000", ("127.0.0.1", 56000)),
        ],
    )
    def test_resolves(self, url, expected):
        assert target_from_url(url) == expected

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("http://0.0.0.0:56000", ("127.0.0.1", 56000)),
            ("http://[::]:56000", ("::1", 56000)),
        ],
    )
    def test_maps_wildcard_to_loopback(self, url, expected):
        """0.0.0.0 means "every interface" to bind, but is not connectable."""
        assert target_from_url(url) == expected

    @pytest.mark.parametrize("url", ["", "http://", "not a url", "ftp://host"])
    def test_rejects_unusable(self, url):
        with pytest.raises(ValueError):
            target_from_url(url)


class TestTargetFromHostPort:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("127.0.0.1:3000", ("127.0.0.1", 3000)),
            ("0.0.0.0:3000", ("127.0.0.1", 3000)),
            (":::3000", ("::1", 3000)),
            ("[::]:3000", ("::1", 3000)),
            ("[2001:db8::1]:3000", ("2001:db8::1", 3000)),
        ],
    )
    def test_resolves(self, value, expected):
        assert target_from_host_port(value) == expected

    @pytest.mark.parametrize("value", ["3000", "host:notaport", ""])
    def test_rejects_unusable(self, value):
        with pytest.raises(ValueError):
            target_from_host_port(value)


@pytest.fixture
def listener():
    """A listening socket, and the address it is actually bound to."""
    sockets = []

    def bind(host: str = "127.0.0.1"):
        sock = socket.socket()
        sock.bind((host, 0))
        sock.listen(8)
        sockets.append(sock)
        return host, sock.getsockname()[1]

    yield bind
    for sock in sockets:
        sock.close()


class TestProbing:
    def test_succeeds_against_a_listening_port(self, listener):
        host, port = listener()
        assert main([f"--url=http://{host}:{port}"]) == 0

    def test_fails_when_nothing_listens(self):
        with closing(socket.socket()) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        assert main([f"--url=http://127.0.0.1:{port}"]) == 1

    def test_no_targets_is_healthy(self):
        """Nothing to probe means nothing to contradict PID 1 being alive."""
        assert main([]) == 0

    def test_requires_every_target(self, listener):
        host, port = listener()
        with closing(socket.socket()) as sock:
            sock.bind(("127.0.0.1", 0))
            dead = sock.getsockname()[1]

        assert main([f"--url=http://{host}:{port}"]) == 0
        assert main([f"--url=http://{host}:{port}", f"--host-port=127.0.0.1:{dead}"]) == 1

    def test_rejects_a_malformed_target(self, capsys):
        """Exit 2, distinct from 1, so misconfigured reads differently from down."""
        assert main(["--url=nonsense"]) == 2
        assert "nonsense" in capsys.readouterr().err

    def test_gives_up_when_the_watched_process_dies(self):
        """Startup must fail fast on a dead cam2ip, not sit out the deadline."""
        process = __import__("subprocess").Popen([sys.executable, "-c", "pass"])
        process.wait()

        with closing(socket.socket()) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]

        assert main([f"--url=http://127.0.0.1:{port}", "--timeout=30",
                     f"--pid={process.pid}"]) == 1


class TestPinnedInterfaceRegression:
    """The bug: both probes hardcoded 127.0.0.1 and only took the port from
    CAM2IP_BIND_ADDR.

    cam2ip listens solely on the addresses it bound to, so pinning a single
    interface -- a reasonable "do not expose this on everything" setting -- made
    the loopback probe fail. Startup burned its whole 30s deadline and exited 1,
    and HEALTHCHECK reported unhealthy forever, both while cam2ip was fine and
    reachable at the address the MCP server was configured to use.

    127.0.0.2 stands in for a pinned interface: it is loopback, so it is always
    available, but connecting to 127.0.0.1 on the same port is refused.
    """

    pytestmark = pytest.mark.skipif(
        sys.platform != "linux", reason="needs a bindable 127.0.0.0/8 alias"
    )

    def test_probes_the_pinned_interface_not_loopback(self, listener):
        host, port = listener("127.0.0.2")

        # What the old code did, and why it broke.
        assert main([f"--url=http://127.0.0.1:{port}"]) == 1
        # What it does now: the address CAM2IP_BASE_URL actually names.
        assert main([f"--url=http://{host}:{port}"]) == 0

    def test_failure_message_names_both_settings(self, monkeypatch, capsys):
        """A base URL / bind address mismatch is the likely cause, so say so."""
        monkeypatch.setenv("CAM2IP_BASE_URL", "http://127.0.0.1:56000")
        monkeypatch.setenv("CAM2IP_BIND_ADDR", "192.168.1.5:56000")

        with closing(socket.socket()) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]

        assert main([f"--url=http://127.0.0.1:{port}"]) == 1
        stderr = capsys.readouterr().err
        assert "CAM2IP_BASE_URL" in stderr
        assert "CAM2IP_BIND_ADDR" in stderr
        assert "192.168.1.5:56000" in stderr

    def test_an_unreachable_mcp_port_does_not_blame_cam2ip(self, monkeypatch, capsys):
        """Only cam2ip's address is split across two settings, so only it can
        be unreachable because they disagree."""
        monkeypatch.setenv("CAM2IP_BASE_URL", "http://127.0.0.1:56000")
        monkeypatch.setenv("CAM2IP_BIND_ADDR", "0.0.0.0:56000")

        with closing(socket.socket()) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]

        assert main([f"--host-port=127.0.0.1:{port}"]) == 1
        stderr = capsys.readouterr().err
        assert "the MCP server is not accepting connections" in stderr
        assert "CAM2IP_BIND_ADDR" not in stderr


class TestEntrypointUsesTheProbe:
    """End to end: the entrypoint must start against a pinned interface."""

    pytestmark = pytest.mark.skipif(
        sys.platform != "linux", reason="needs a bindable 127.0.0.0/8 alias"
    )

    async def test_startup_succeeds_with_a_pinned_bind_address(self, tmp_path):
        import os
        import stat

        from mcp import Client, StdioServerParameters
        from mcp.client.stdio import stdio_client

        repo_root = __import__("pathlib").Path(__file__).resolve().parent.parent
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()

        for name, body in (
            ("python", f'exec {sys.executable} "$@"'),
            ("cam2ip", f'exec {sys.executable} {repo_root / "tests" / "fake_cam2ip.py"} "$@"'),
        ):
            path = bin_dir / name
            path.write_text(f"#!/bin/sh\n{body}\n")
            path.chmod(path.stat().st_mode | stat.S_IEXEC)

        with closing(socket.socket()) as sock:
            sock.bind(("127.0.0.2", 0))
            port = sock.getsockname()[1]

        params = StdioServerParameters(
            command="/bin/sh",
            args=[str(repo_root / "entrypoint.sh")],
            env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "MCP_SERVER_PATH": str(repo_root / "cam2ip_mcp_server.py"),
                "MCP_PROBE_PATH": str(repo_root / "cam2ip_probe.py"),
                # Pinned to one interface, with a base URL that agrees.
                "CAM2IP_BIND_ADDR": f"127.0.0.2:{port}",
                "CAM2IP_BASE_URL": f"http://127.0.0.2:{port}",
                "MCP_MODE": "stdio",
                "MCP_GRAB_TIMEOUT_S": "15",
                "MCP_LOG_LEVEL": "WARNING",
            },
        )

        async with Client(stdio_client(params)) as client:
            result = await asyncio.wait_for(client.call_tool("grab_frame", {}), timeout=40)

        assert not result.is_error, result.content
