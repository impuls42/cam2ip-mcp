#!/usr/bin/env python3
"""MCP server that returns live JPEG frames from a webcam, backed by cam2ip.

Why this does not just GET /jpeg
--------------------------------
V4L2 capture uses a queue of mmap'd buffers (korandiz/v4l, which cam2ip uses,
allocates 4 of them in TurnOn). Once the camera is streaming, the driver fills
those buffers and then *waits*: filled buffers stay in the done-queue until
somebody dequeues them. Each frame read dequeues exactly one buffer, oldest
first.

So if the capture pipeline sits idle between requests, the queue keeps whatever
was in front of the lens when it last drained. A one-shot GET /jpeg after an
idle period dequeues that -- a picture from minutes or hours ago -- and the
next few requests walk forward through the rest of the stale queue. No amount
of HTTP cache-busting helps, because nothing was ever cached: the staleness is
in the kernel's buffer queue.

The fix is to never let the pipeline idle while we care about frames. Three
things do that, and each covers a case the others cannot:

1. A single MJPEG subscription, held open. cam2ip serves every client from one
   capture loop, so one live subscriber keeps that loop dequeuing at the
   camera's frame rate. While it is up, the queue is always nearly empty and an
   arriving frame really is a picture of now (lag: queue depth / fps).
2. Dropping the first frames after each (re)connect. Those are precisely the
   pre-idle ones the queue was sitting on, and they arrive in an instant burst
   rather than at the frame rate -- so MCP_WARMUP_FRAMES must exceed the
   driver's buffer count (4 for V4L2 via korandiz/v4l), and MCP_WARMUP_S
   discards the burst on cameras whose queue is deeper than that.
3. A maximum age on what we hand out. Note this is measured from when a frame
   *arrived*, because HTTP gives us no capture timestamp -- so it cannot detect
   a stale queued frame (see 2), but it does catch a pipeline that stalled or
   died, which would otherwise leave a frame from an earlier connection sitting
   in memory looking usable.

Configuration (all optional):

  CAM2IP_BASE_URL        cam2ip base URL              (http://127.0.0.1:56000)
  CAM2IP_HTTP_TIMEOUT_S  HTTP connect/read timeout    (5.0)
  MCP_MODE               stdio | sse | streamable-http (stdio)
  MCP_HTTP_HOST          bind host for HTTP modes     (0.0.0.0)
  MCP_HTTP_PORT          bind port for HTTP modes     (3000)
  MCP_FRAME_MAX_AGE_S    reject frames that arrived longer ago than (1.0)
  MCP_GRAB_TIMEOUT_S     give up waiting for a frame  (15.0)
  MCP_WARMUP_FRAMES      frames to drop after connect (5)
  MCP_WARMUP_S           also drop frames for this long after connect (0.25)
  MCP_STREAM_IDLE_S      keep stream warm this long   (30.0)
  MCP_ALLOWED_HOSTS      comma-separated Host allowlist (unset = allow any)
  MCP_ALLOWED_ORIGINS    comma-separated Origin allowlist (unset = allow any)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

import httpx2
from mcp.server import MCPServer
from mcp.server.mcpserver import Image
from mcp.server.transport_security import TransportSecuritySettings

log = logging.getLogger("cam2ip-mcp")

# cam2ip sets this literal boundary; used when a server omits it from the header.
FALLBACK_BOUNDARY = b"--boundary"

# Refuse to buffer more than this while looking for a part delimiter. A real
# MJPEG part is a single frame, so anything larger means we are not parsing
# multipart at all.
MAX_PART_BYTES = 32 * 1024 * 1024

RECONNECT_BACKOFF_S = (0.1, 0.25, 0.5, 1.0, 2.0)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _env_number(name: str, default: float, cast=float, minimum=None, why: str = ""):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = cast(raw)
    except ValueError:
        raise SystemExit(f"{name}: expected {cast.__name__}, got {raw!r}") from None
    if minimum is not None and value < minimum:
        message = f"{name}: must be >= {minimum}, got {value}"
        raise SystemExit(f"{message} ({why})" if why else message)
    return value


def _env_list(name: str) -> list[str] | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Config:
    base_url: str
    http_timeout_s: float
    mode: str
    http_host: str
    http_port: int
    frame_max_age_s: float
    grab_timeout_s: float
    warmup_frames: int
    warmup_s: float
    stream_idle_s: float
    allowed_hosts: list[str] | None
    allowed_origins: list[str] | None

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            base_url=_env_str("CAM2IP_BASE_URL", "http://127.0.0.1:56000").rstrip("/"),
            http_timeout_s=_env_number("CAM2IP_HTTP_TIMEOUT_S", 5.0, float, 0.1),
            mode=_env_str("MCP_MODE", "stdio").strip().lower(),
            http_host=_env_str("MCP_HTTP_HOST", "0.0.0.0"),
            # Port 0 would bind an ephemeral port, but mcp does not report back
            # which one it got, so nothing could tell a client where to connect.
            http_port=_env_number(
                "MCP_HTTP_PORT", 3000, int, 1,
                why="port 0 would pick an ephemeral port that is never reported, "
                    "leaving no way to find the server",
            ),
            frame_max_age_s=_env_number("MCP_FRAME_MAX_AGE_S", 1.0, float, 0.0),
            grab_timeout_s=_env_number("MCP_GRAB_TIMEOUT_S", 15.0, float, 0.1),
            warmup_frames=_env_number("MCP_WARMUP_FRAMES", 5, int, 0),
            warmup_s=_env_number("MCP_WARMUP_S", 0.25, float, 0.0),
            stream_idle_s=_env_number("MCP_STREAM_IDLE_S", 30.0, float, 0.0),
            allowed_hosts=_env_list("MCP_ALLOWED_HOSTS"),
            allowed_origins=_env_list("MCP_ALLOWED_ORIGINS"),
        )


# ---------------------------------------------------------------------------
# MJPEG parsing
# ---------------------------------------------------------------------------


def parse_boundary(content_type: str) -> bytes:
    """Extract the multipart boundary from a Content-Type header value."""
    for param in content_type.split(";")[1:]:
        key, _, value = param.partition("=")
        if key.strip().lower() == "boundary":
            value = value.strip().strip('"')
            if value:
                return value.encode("latin-1")
    return FALLBACK_BOUNDARY


async def iter_mjpeg_parts(
    chunks: AsyncIterator[bytes], boundary: bytes
) -> AsyncIterator[tuple[str, bytes]]:
    """Yield (content_type, body) for each part of a multipart/x-mixed-replace stream.

    Parts carry no Content-Length -- cam2ip does not send one -- so each body
    runs until the next delimiter. Per RFC 2046 that delimiter is CRLF + "--" +
    boundary, and requiring the leading CRLF is what keeps a boundary-shaped
    byte sequence inside JPEG data from splitting a frame.
    """
    delimiter = b"--" + boundary
    body_end = b"\r\n" + delimiter
    buffer = bytearray()
    in_part = False
    content_type = "image/jpeg"

    async for chunk in chunks:
        buffer += chunk

        while True:
            if not in_part:
                start = buffer.find(delimiter)
                if start < 0:
                    break
                header_end = buffer.find(b"\r\n\r\n", start + len(delimiter))
                if header_end < 0:
                    break
                headers = bytes(buffer[start + len(delimiter) : header_end])
                content_type = _part_content_type(headers)
                del buffer[: header_end + 4]
                in_part = True

            end = buffer.find(body_end)
            if end < 0:
                break
            body = bytes(buffer[:end])
            # Leave the delimiter in place; it starts the next part.
            del buffer[: end + 2]
            in_part = False
            if body:
                yield content_type, body

        if len(buffer) > MAX_PART_BYTES:
            raise ValueError(
                f"no multipart delimiter in {len(buffer)} bytes; "
                f"is {boundary!r} the right boundary?"
            )


def _part_content_type(headers: bytes) -> str:
    for line in headers.split(b"\r\n"):
        key, sep, value = line.partition(b":")
        if sep and key.strip().lower() == b"content-type":
            return value.decode("latin-1").split(";")[0].strip() or "image/jpeg"
    return "image/jpeg"


# ---------------------------------------------------------------------------
# Frame source
# ---------------------------------------------------------------------------


class FrameUnavailable(RuntimeError):
    """No sufficiently fresh frame could be obtained."""


def _is_permanent(exc: BaseException) -> bool:
    """Whether an error will still be an error after a retry.

    A refused connection or a timeout is worth retrying -- cam2ip may still be
    starting, or the camera may be waking up. A 4xx or a response that is not
    multipart at all means we are asking the wrong thing of the wrong endpoint,
    and no amount of reconnecting changes that.
    """
    if isinstance(exc, httpx2.HTTPStatusError):
        return 400 <= exc.response.status_code < 500
    # Raised by this module for a non-multipart response or an unparseable stream.
    return isinstance(exc, ValueError)


@dataclass(frozen=True)
class Frame:
    data: bytes
    content_type: str
    age_s: float


class FrameSource:
    """Keeps the newest camera frame in memory via a persistent MJPEG subscription.

    The subscription starts on the first grab and is dropped once nothing has
    asked for a frame in stream_idle_s seconds, so the camera is not held open
    (and its indicator light not left on) while the server is idle.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._mjpeg_url = f"{config.base_url}/mjpeg"

        self._client: httpx2.AsyncClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._cond = asyncio.Condition()
        self._closed = False

        self._frame: bytes | None = None
        self._content_type = "image/jpeg"
        self._frame_at = 0.0
        self._last_grab_at = 0.0
        self._waiters = 0

        self._connected = False
        self._frames_received = 0
        self._frames_published = 0
        self._last_error: str | None = None
        self._permanent_error: str | None = None

    # -- public API --------------------------------------------------------

    async def grab(self, max_age_s: float | None = None) -> Frame:
        """Return a frame no older than max_age_s, waiting for a new one if needed."""
        if self._closed:
            raise FrameUnavailable("frame source is closed")

        max_age = self._config.frame_max_age_s if max_age_s is None else max_age_s
        started = time.monotonic()
        deadline = started + self._config.grab_timeout_s

        async with self._cond:
            self._last_grab_at = started

            while True:
                # A frame that arrived within max_age of this call starting is
                # good enough, so back-to-back calls are cheap. Checked before
                # touching the stream: if memory can answer, there is no reason
                # to wake the camera up.
                if self._frame is not None and self._frame_at >= started - max_age:
                    return Frame(
                        data=self._frame,
                        content_type=self._content_type,
                        age_s=max(0.0, time.monotonic() - self._frame_at),
                    )

                # A running pump that has hit something a retry cannot fix -- a
                # 404, an endpoint that is not MJPEG -- will still be failing
                # when the deadline expires, so say so now rather than making
                # the caller wait out grab_timeout_s for the same answer. Only
                # while that pump is alive: once it exits, the next grab starts
                # a fresh one and is entitled to a fresh verdict.
                if self._task is not None and self._permanent_error is not None:
                    raise FrameUnavailable(
                        f"cannot get frames from {self._mjpeg_url}: {self._permanent_error}"
                    )

                self._start_pump_locked()

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FrameUnavailable(self._timeout_message(max_age))

                self._waiters += 1
                try:
                    await asyncio.wait_for(self._cond.wait(), remaining)
                except (asyncio.TimeoutError, TimeoutError):
                    pass
                finally:
                    self._waiters -= 1
                    self._last_grab_at = max(self._last_grab_at, time.monotonic())

    def status(self) -> dict[str, object]:
        now = time.monotonic()
        return {
            "cam2ip_url": self._config.base_url,
            "stream_connected": self._connected,
            "stream_running": self._task is not None,
            "frames_received": self._frames_received,
            "frames_published": self._frames_published,
            "last_frame_age_s": (
                round(now - self._frame_at, 3) if self._frame is not None else None
            ),
            "frame_max_age_s": self._config.frame_max_age_s,
            "warmup_frames": self._config.warmup_frames,
            "warmup_s": self._config.warmup_s,
            "stream_idle_s": self._config.stream_idle_s,
            "last_error": self._last_error,
            "last_error_is_permanent": self._permanent_error is not None,
        }

    async def aclose(self) -> None:
        self._closed = True
        async with self._cond:
            task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown
                pass
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- pump --------------------------------------------------------------

    def _start_pump_locked(self) -> None:
        """Start the MJPEG pump if it is not running. Caller holds self._cond."""
        if self._task is None and not self._closed:
            # A new attempt earns a new verdict: whatever was unfixable last
            # time may have been fixed since.
            self._permanent_error = None
            self._task = asyncio.create_task(self._pump(), name="cam2ip-frame-pump")

    async def _pump(self) -> None:
        attempt = 0
        try:
            while not self._closed:
                try:
                    idle = await self._stream_once()
                    if idle:
                        return
                    attempt = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - reported via status
                    await self._set_error(f"{type(exc).__name__}: {exc}", permanent=_is_permanent(exc))

                    # Reconnecting has to honour the idle timeout too. Without
                    # this the retry loop is the one path that never checks, so
                    # a stream that cannot be established (wrong base URL, say)
                    # would retry every 2s for the life of the process, long
                    # after the request that started it gave up.
                    if await self._should_go_idle():
                        log.info("no frame requested in %.1fs, giving up on reconnecting",
                                 self._config.stream_idle_s)
                        return

                    backoff = RECONNECT_BACKOFF_S[min(attempt, len(RECONNECT_BACKOFF_S) - 1)]
                    attempt += 1
                    await asyncio.sleep(backoff)
        finally:
            self._connected = False
            # Clear this before taking the lock. Acquiring it can raise if we
            # are being cancelled and a grabber holds it, and a self._task left
            # pointing at a dead pump would stop it ever being restarted.
            if self._task is asyncio.current_task():
                self._task = None
            # Then wake grabbers so they re-check and restart promptly. Only an
            # optimisation: their wait is bounded by grab_timeout_s regardless.
            # Not guarded: a CancelledError here belongs to a task that is ending
            # anyway and is caught by aclose(), while anything else -- a misused
            # Condition, say -- is a bug worth seeing rather than swallowing.
            async with self._cond:
                self._cond.notify_all()

    async def _stream_once(self) -> bool:
        """Consume one MJPEG connection. Returns True if it ended because we went idle."""
        client = self._ensure_client()
        log.info("subscribing to %s", self._mjpeg_url)

        async with client.stream("GET", self._mjpeg_url) as response:
            response.raise_for_status()

            content_type = response.headers.get("content-type", "")
            if "multipart/" not in content_type.lower():
                raise ValueError(
                    f"{self._mjpeg_url} returned {content_type!r}, expected "
                    f"multipart/x-mixed-replace"
                )

            self._connected = True
            await self._set_error(None)

            # Frames already sitting in the driver's queue when we subscribed
            # are exactly the stale ones, and they arrive in an instant burst
            # rather than at the frame rate. Drop by count *and* by elapsed
            # time so a queue deeper than we assumed is still flushed -- and so
            # the first frame we publish is past any auto-exposure ramp.
            dropped = 0
            connected_at = time.monotonic()

            parts = iter_mjpeg_parts(response.aiter_bytes(), parse_boundary(content_type))
            async with aclosing(parts):
                async for part_type, body in parts:
                    self._frames_received += 1

                    if (dropped < self._config.warmup_frames
                            or time.monotonic() - connected_at < self._config.warmup_s):
                        dropped += 1
                        continue

                    await self._publish(body, part_type)

                    if await self._should_go_idle():
                        log.info("no frame requested in %.1fs, dropping subscription",
                                 self._config.stream_idle_s)
                        return True

        raise ConnectionError(f"{self._mjpeg_url} closed the stream")

    def _ensure_client(self) -> httpx2.AsyncClient:
        if self._client is None:
            timeout = self._config.http_timeout_s
            self._client = httpx2.AsyncClient(
                timeout=httpx2.Timeout(timeout),
                headers={"Accept": "multipart/x-mixed-replace, image/jpeg"},
            )
        return self._client

    async def _publish(self, body: bytes, content_type: str) -> None:
        async with self._cond:
            self._frame = body
            self._content_type = content_type
            self._frame_at = time.monotonic()
            self._frames_published += 1
            self._cond.notify_all()

    async def _should_go_idle(self) -> bool:
        """Whether to drop the subscription: nobody waiting, nobody asking lately.

        A waiting grabber can never be starved by this, and not because
        grab_timeout_s happens to be shorter than stream_idle_s -- that ordering
        is not load-bearing and callers may invert it. Two things hold instead:
        _waiters is non-zero for as long as anyone is blocked, and the check runs
        under the same condition the grabber holds, so it cannot slip into the
        moment between a wakeup and the next wait. A grabber also refreshes
        _last_grab_at every time it wakes, so even a zero idle timeout keeps the
        pump alive underneath it. Covered by the inverted-ordering test in
        tests/test_freshness.py.
        """
        async with self._cond:
            if self._waiters > 0:
                return False
            return time.monotonic() - self._last_grab_at > self._config.stream_idle_s

    async def _set_error(self, message: str | None, permanent: bool = False) -> None:
        if message:
            log.warning("stream error: %s", message)
        async with self._cond:
            self._last_error = message
            if message is None:
                self._permanent_error = None
            elif permanent:
                self._permanent_error = message
            # Waiting grabbers are blocked on this condition, so wake them when
            # what they are waiting on changes. A permanent failure means they
            # should stop waiting at once; a transient one costs them a re-check.
            self._cond.notify_all()

    def _timeout_message(self, max_age: float) -> str:
        detail = (
            f"no frame newer than {max_age:g}s from {self._mjpeg_url} "
            f"within {self._config.grab_timeout_s:g}s"
        )
        if self._last_error:
            return f"{detail} (last error: {self._last_error})"
        if self._frames_received == 0:
            return (
                f"{detail}; cam2ip accepted the connection but sent no frames -- "
                f"check that it can open the camera (container needs --device=/dev/video0)"
            )
        return detail


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

CONFIG = Config.from_env()
_source: FrameSource | None = None


def source() -> FrameSource:
    global _source
    if _source is None:
        _source = FrameSource(CONFIG)
    return _source


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
    try:
        yield
    finally:
        if _source is not None:
            await _source.aclose()


mcp = MCPServer(
    name="cam2ip-mcp",
    instructions=(
        "Provides live still images from a webcam attached to the host running "
        "this server. Call grab_frame to see what the camera currently sees."
    ),
    lifespan=lifespan,
)


@mcp.tool()
async def grab_frame(max_age_s: float | None = None) -> Image:
    """Capture a frame from the attached webcam and return it as a JPEG image.

    The image shows what the camera sees now, not a leftover from an earlier
    call. Waking a cold camera takes a moment, so the first call after a quiet
    spell is slower than the ones after it.

    On what max_age_s does and does not promise: it bounds how long ago a frame
    *arrived here*, not when the sensor captured it, because nothing in the
    stream carries a capture time. It is therefore not a staleness detector -- a
    frame that sat in the camera's buffer queue for a minute and reached us a
    moment ago is young by this measure. What excludes those is the server
    dropping the first frames after it reconnects, and keeping the pipeline
    draining so they stop accumulating. This bound catches a different failure:
    a stalled or dead pipeline leaving an old frame in memory.

    Args:
        max_age_s: How old a frame may be, in seconds, before it is refused.
            Defaults to the server's MCP_FRAME_MAX_AGE_S setting (1.0s). Raise
            it to accept a slightly older frame in exchange for a faster reply.
            Zero is not "as fresh as possible" but something stricter: it demands
            a frame that arrives *after* this call begins, so one captured
            microseconds earlier is refused and the call waits for the next.
    """
    if max_age_s is not None and max_age_s < 0:
        raise ValueError("max_age_s must be >= 0")

    frame = await source().grab(max_age_s)
    subtype = frame.content_type.partition("/")[2] or "jpeg"
    return Image(data=frame.data, format=subtype)


@mcp.tool()
async def camera_status() -> dict[str, object]:
    """Report how the camera stream is doing: connection state, frame counts and errors.

    Useful for diagnosing a camera that returns errors or frames that look
    older than expected.
    """
    return source().status()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

VALID_MODES = ("stdio", "sse", "streamable-http")


def transport_security() -> TransportSecuritySettings | None:
    """Host/Origin allowlist for the HTTP transports, if the operator set one.

    Returning None leaves mcp's own defaults in place: an allowlist is applied
    automatically when bound to loopback, and omitted when bound to a public
    interface (where the Host header is not knowable up front).

    Passing settings at all replaces those defaults, and mcp's middleware then
    validates Host and Origin together or not at all -- there is no per-check
    switch, and no allowlist entry that means "any host" ("*" matches only a
    literal "*" Host header). So an empty host list is not "unrestricted", it is
    "reject everything with 421", which is why only setting origins cannot be
    honoured and is refused below instead.
    """
    if CONFIG.allowed_hosts is None and CONFIG.allowed_origins is None:
        return None

    if CONFIG.allowed_hosts is None:
        raise SystemExit(
            "MCP_ALLOWED_ORIGINS needs MCP_ALLOWED_HOSTS set as well.\n"
            "  mcp checks Host and Origin together, and its host allowlist cannot\n"
            "  express \"any host\" -- so restricting origins alone would leave an\n"
            "  empty host allowlist, and every request would be refused with 421.\n"
            "  List the names clients reach this server by, e.g.\n"
            "  MCP_ALLOWED_HOSTS=camera.example.com:*,127.0.0.1:*"
        )

    # Hosts without origins is honoured as-is: a request carrying no Origin
    # header passes (which is every non-browser MCP client), and one carrying any
    # Origin is refused. That is the stricter reading of "lock this down", and
    # MCP_ALLOWED_ORIGINS is how to let specific browsers back in.
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=CONFIG.allowed_hosts,
        allowed_origins=CONFIG.allowed_origins or [],
    )


def main() -> None:
    # stdout is the JSON-RPC channel in stdio mode, so all logging goes to stderr.
    logging.basicConfig(
        level=os.environ.get("MCP_LOG_LEVEL", "INFO").upper(),
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if CONFIG.mode not in VALID_MODES:
        raise SystemExit(
            f"MCP_MODE={CONFIG.mode!r} is not one of: {', '.join(VALID_MODES)}"
        )

    if CONFIG.mode == "stdio":
        log.info("serving MCP over stdio, camera via %s", CONFIG.base_url)
        mcp.run(transport="stdio")
        return

    # Resolved before announcing anything, so a rejected allowlist does not print
    # "serving ..." on its way out.
    security = transport_security()

    path = "/mcp" if CONFIG.mode == "streamable-http" else "/sse"
    log.info(
        "serving MCP over %s at http://%s:%d%s, camera via %s",
        CONFIG.mode, CONFIG.http_host, CONFIG.http_port, path, CONFIG.base_url,
    )
    mcp.run(
        transport=CONFIG.mode,
        host=CONFIG.http_host,
        port=CONFIG.http_port,
        transport_security=security,
    )


if __name__ == "__main__":
    main()
