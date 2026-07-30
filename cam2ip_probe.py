#!/usr/bin/env python3
"""TCP reachability probe for the entrypoint's startup wait and HEALTHCHECK.

Separate from entrypoint.sh so the address logic can be tested, which is where
the interesting mistakes live. Two of them are worth spelling out:

* **Probe what the MCP server dereferences, not what cam2ip bound to.** cam2ip
  only listens on the addresses in CAM2IP_BIND_ADDR, so probing loopback
  unconditionally fails whenever an operator pins a single interface -- the
  container would never finish starting, and HEALTHCHECK would report unhealthy
  forever, with cam2ip perfectly fine. Deriving the target from CAM2IP_BASE_URL
  instead also means a BASE_URL that disagrees with BIND_ADDR is caught here,
  with a message naming both, rather than at the first frame request.
* **A wildcard bind is not a connectable address.** 0.0.0.0 and :: mean "every
  interface" to bind(2) but are not useful to connect(2), so they map to the
  corresponding loopback address.

Deliberately a bare TCP connect. Requesting a frame would dequeue one from the
capture queue, and on a HEALTHCHECK interval would wake the camera every time,
defeating CAM2IP_LAZY.

Usage:
    cam2ip_probe.py --url URL [--host-port HOST:PORT] [--timeout S] [--pid PID]

Every target given must be reachable. --timeout retries until the deadline
(default: a single attempt). --pid fails fast if that process dies meanwhile.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from urllib.parse import urlsplit

# Wildcard binds, and the loopback address to reach each one on.
WILDCARD_HOSTS = {"0.0.0.0": "127.0.0.1", "::": "::1", "[::]": "::1", "*": "127.0.0.1"}

DEFAULT_PORTS = {"http": 80, "https": 443}

# What a target is, for failure messages: --url probes cam2ip (a base URL is how
# its address is expressed), --host-port probes the MCP server's own listener.
CAM2IP = "cam2ip"
MCP_SERVER = "the MCP server"

# host, port, and which of the two the target is.
Target = tuple[str, int, str]


def connectable(host: str, port: int) -> tuple[str, int]:
    """Map a bind address to one that can be connected to."""
    return WILDCARD_HOSTS.get(host, host), port


def target_from_url(url: str) -> tuple[str, int]:
    """Resolve host and port from a base URL, applying the scheme's default port."""
    parts = urlsplit(url)
    if not parts.scheme:
        # Bare "host:port" is a plausible thing to end up in a URL variable.
        parts = urlsplit(f"//{url}")

    host = parts.hostname
    if not host:
        raise ValueError(f"no host in URL: {url!r}")

    port = parts.port or DEFAULT_PORTS.get(parts.scheme.lower())
    if port is None:
        raise ValueError(f"no port in URL and no default for scheme: {url!r}")

    return connectable(host, port)


def target_from_host_port(value: str) -> tuple[str, int]:
    """Resolve a "host:port" pair, tolerating a bracketed IPv6 host."""
    host, separator, port = value.rpartition(":")
    if not separator:
        raise ValueError(f"expected HOST:PORT, got {value!r}")

    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]

    try:
        return connectable(host or "127.0.0.1", int(port))
    except ValueError:
        raise ValueError(f"not a port number in {value!r}: {port!r}") from None


def reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        socket.create_connection((host, port), timeout).close()
        return True
    except OSError:
        return False


def describe_failure(host: str, port: int, what: str) -> str:
    """Explain an unreachable target, naming the settings that decide it."""
    detail = f"{what} is not accepting connections at {host}:{port}"

    # Only cam2ip's address is split across two settings, so only it can be
    # unreachable because they disagree. Blaming them for an unreachable MCP
    # port would send the reader somewhere irrelevant.
    base_url = os.environ.get("CAM2IP_BASE_URL")
    bind_addr = os.environ.get("CAM2IP_BIND_ADDR")
    if what == CAM2IP and base_url and bind_addr:
        detail += (
            f"\n  CAM2IP_BASE_URL={base_url} (where the MCP server looks)"
            f"\n  CAM2IP_BIND_ADDR={bind_addr} (where cam2ip listens)"
            f"\n  These have to agree: cam2ip only listens on what it bound to."
        )
    return detail


def wait_for(targets: list[Target], timeout: float, pid: int | None) -> str | None:
    """Wait for every target. Returns None on success, or a message explaining why not."""
    deadline = time.monotonic() + timeout
    pending = list(targets)

    while True:
        if pid is not None:
            try:
                os.kill(pid, 0)
            except OSError:
                return "cam2ip exited during startup (see its output above)"

        pending = [target for target in pending if not reachable(target[0], target[1])]
        if not pending:
            return None

        if time.monotonic() >= deadline:
            return describe_failure(*pending[0])

        time.sleep(0.2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", action="append", default=[],
                        help="cam2ip base URL to probe; repeatable")
    parser.add_argument("--host-port", action="append", default=[],
                        help="MCP server HOST:PORT to probe; repeatable")
    parser.add_argument("--timeout", type=float, default=0.0,
                        help="keep retrying for this long (default: one attempt)")
    parser.add_argument("--pid", type=int, default=None,
                        help="give up early if this process exits")
    args = parser.parse_args(argv)

    try:
        targets: list[Target] = [(*target_from_url(url), CAM2IP) for url in args.url]
        targets += [(*target_from_host_port(v), MCP_SERVER) for v in args.host_port]
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    # Nothing to probe means nothing to contradict: the MCP server runs as PID 1,
    # so the container being up is the only liveness signal there is.
    if not targets:
        return 0

    failure = wait_for(targets, args.timeout, args.pid)
    if failure:
        print(failure, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
