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
   rather than at the frame rate. The number dropped is
   max(MCP_WARMUP_FRAMES, MCP_WARMUP_S * fps) and has to exceed the driver's
   queue depth. Note that depth is not fixed: korandiz/v4l asks for four
   buffers, but V4L2 permits the driver to grant more and the library maps
   whatever it gets, so four is the common case rather than a guarantee. The
   time window is what covers a deeper queue at a decent frame rate.
3. A maximum age on what we hand out. Note this is measured from when a frame
   *arrived*, because HTTP gives us no capture timestamp -- so it cannot detect
   a stale queued frame (see 2), but it does catch a pipeline that stalled or
   died, which would otherwise leave a frame from an earlier connection sitting
   in memory looking usable.

Camera controls
---------------
cam2ip owns the stream and exposes no way to change zoom, focus or exposure, so
this server drives them itself: it opens the same V4L2 node beside cam2ip and
writes controls directly, which is allowed because controls are not part of the
exclusive streaming interface. See v4l2_controls.py for why that is safe and
what it costs.

The cost worth restating here is that control state lives in the camera's
firmware, not in this process. Left alone it outlives the container and lands on
whoever opens the camera next. So anything this server changes is remembered and
put back once the camera has been idle for MCP_CONTROL_IDLE_S -- the same
"nobody is asking any more" signal that drops the MJPEG subscription, applied to
a second kind of state.

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
  CAMERA_DEVICE          V4L2 node for controls       (/dev/video0)
  CAMERA_CONTROLS        enable the control tools     (auto)
  MCP_CONTROL_IDLE_S     put changed controls back after this long idle (120.0;
                         0 disables, leaving changes until camera_reset)
  AUDIO_CAPTURE          offer record_audio: true | auto | false (false)
  AUDIO_DEVICE           ALSA device or card-name substring (first USB card)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

import httpx2
from mcp.server import MCPServer
from mcp.server.mcpserver import Audio, Image
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ContentBlock, TextContent

import audio_capture
import v4l2_controls
from v4l2_controls import CameraControlError, CameraControls

# Pillow backs the region crop and nothing else. Imported softly because it is
# the one dependency that is not needed to serve a whole frame: a missing Pillow
# should cost the crop argument, not the server.
try:
    from PIL import Image as PILImage
except ImportError:  # pragma: no cover - exercised by the packaging, not the tests
    PILImage = None

log = logging.getLogger("cam2mcp")

# cam2ip sets this literal boundary; used when a server omits it from the header.
FALLBACK_BOUNDARY = b"--boundary"

# Refuse to buffer more than this while looking for a part delimiter. A real
# MJPEG part is a single frame, so anything larger means we are not parsing
# multipart at all.
MAX_PART_BYTES = 32 * 1024 * 1024

RECONNECT_BACKOFF_S = (0.1, 0.25, 0.5, 1.0, 2.0)

# JPEG quality for a cropped region. Above cam2ip's own 75 because this is the
# second lossy pass over the same pixels, applied to exactly the part someone is
# trying to read detail out of; the region is a fraction of the frame, so the
# result is still smaller than the whole frame at 75.
CROP_QUALITY = 90


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
    # Defaulted, unlike the fields above, so that constructing a Config for the
    # frame path alone does not have to say anything about controls -- which is
    # also what keeps adding a control setting from touching every test that
    # builds one.
    camera_device: str = "/dev/video0"
    controls_enabled: str = "auto"
    control_idle_s: float = 120.0
    audio_enabled: str = "false"
    audio_device: str = ""

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
            camera_device=_env_str("CAMERA_DEVICE", "/dev/video0"),
            # "auto" rather than a boolean default: the control tools need a
            # device node passed into the container, which the frame path does
            # not (it reaches the camera through cam2ip over HTTP, and cam2ip may
            # not even be local). Registering tools that are guaranteed to fail
            # in that setup is worse than not offering them, so absence of the
            # node disables them quietly -- while an explicit "true" turns that
            # into a startup error, for someone who meant to have them.
            controls_enabled=_env_str("CAMERA_CONTROLS", "auto").strip().lower(),
            control_idle_s=_env_number("MCP_CONTROL_IDLE_S", 120.0, float, 0.0),
            # Off unless asked for, and deliberately not "auto" like the camera
            # controls. The difference is what the capability is: a webcam's
            # indicator light announces that it is being watched, and a caller
            # asking this server for a picture already knows a camera is
            # involved. A microphone advertises nothing, and "the sound card
            # happened to be visible in the container" is not consent to record
            # the room. An operator turns this on.
            audio_enabled=_env_str("AUDIO_CAPTURE", "false").strip().lower(),
            audio_device=_env_str("AUDIO_DEVICE", "").strip(),
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


def _describe_exception(exc: BaseException) -> str:
    """Render an exception for the status output and error messages.

    httpx2's timeouts stringify to the empty string, so the obvious
    f"{type(exc).__name__}: {exc}" yields "ReadTimeout: " -- a dangling colon
    that looks like a truncated message. Fall back to the URL, which is the part
    a reader actually wants, and to the bare class name if even that is missing.
    """
    name = type(exc).__name__
    detail = str(exc).strip()
    if detail:
        return f"{name}: {detail}"
    try:
        return f"{name} for {exc.request.url}"  # type: ignore[attr-defined]
    except (AttributeError, RuntimeError):
        # httpx raises RuntimeError from .request when no request is attached.
        return name


def _error_kind(exc: BaseException) -> str:
    """Classify a stream failure, for choosing what to tell the caller.

    "timeout" is the interesting one. cam2ip's MJPEG handler writes no response
    headers until it has a frame to put in the first part, so a cam2ip that
    cannot open the camera leaves us waiting for headers that never come -- we
    time out without ever seeing a status line. That is indistinguishable at the
    socket level from a camera merely being slow, but combined with having
    received no frames at all it is very strong evidence of a missing device,
    which is the most common way this is misconfigured.
    """
    if isinstance(exc, httpx2.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx2.HTTPStatusError):
        return "http_status"
    if isinstance(exc, httpx2.TransportError):
        return "transport"
    return "other"


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
        self._last_error_kind: str | None = None
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

    def _state(self, now: float) -> str:
        """One word for what is actually going on, for readers who stop at the top.

        The two connection booleans describe the HTTP link to cam2ip, which stays
        perfectly alive when the camera is unplugged -- cam2ip keeps the response
        open and simply stops writing parts. Read on their own they say "fine"
        during a total camera outage, so this leads instead.
        """
        if self._task is None:
            return "idle"
        if self._permanent_error is not None:
            return "failed"
        if not self._connected:
            return "reconnecting"
        # Connected but dry. A live stream delivers at the frame rate, so silence
        # for longer than the read timeout means frames are not coming -- the
        # camera is gone, or wedged, whatever cam2ip's own log says.
        if self._frame is None or now - self._frame_at > self._config.http_timeout_s:
            return "connected_but_no_frames"
        return "streaming"

    def last_frame_data(self) -> bytes | None:
        """The most recent frame, without waiting for or provoking a new one.

        For describing the stream rather than using it -- reading the capture
        size out of a frame that is already in hand. Deliberately not a grab: a
        status call must not wake the camera up, or asking how things are going
        would itself turn the indicator light on.
        """
        return self._frame

    def status(self) -> dict[str, object]:
        now = time.monotonic()
        return {
            "state": self._state(now),
            "cam2ip_url": self._config.base_url,
            # Both of these are about the HTTP conversation with cam2ip, not
            # about the camera; see _state.
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
                    await self._set_error(
                        _describe_exception(exc),
                        permanent=_is_permanent(exc),
                        kind=_error_kind(exc),
                    )

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
            # Deliberately not clearing the last error here. Opening the socket
            # proves cam2ip is listening, not that the camera works -- during an
            # outage this loop reconnects happily and never sees a frame, and
            # clearing on connect made the one useful diagnostic flicker in and
            # out. _publish clears it, because a frame is the actual proof.

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
            # Frames are flowing, so whatever went wrong before is now resolved.
            self._last_error = None
            self._permanent_error = None
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

    async def _set_error(
        self, message: str | None, permanent: bool = False, kind: str | None = None
    ) -> None:
        if message:
            log.warning("stream error: %s", message)
        async with self._cond:
            self._last_error = message
            self._last_error_kind = kind
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
        state = self._state(time.monotonic())

        # Was sending, then stopped: an unplugged or wedged camera. This layer
        # cannot tell those apart -- cam2ip reports "no such device" to its own
        # log and shows us only a stalled stream -- so point at the log.
        if state == "connected_but_no_frames" and self._frames_received:
            return (
                f"{detail}; cam2ip is connected but has stopped sending frames -- "
                f"its log distinguishes a camera that is absent from one that is "
                f"merely slow"
            )

        # Never sent anything, and we timed out rather than being refused: cam2ip
        # is listening but produced no frame to open the response with, which is
        # what a missing or unopenable camera looks like from here. Worth naming
        # explicitly, since it is the most common way a first run is misconfigured
        # -- and a bare "(last error: ReadTimeout)" sends people to the network.
        if self._frames_received == 0 and self._last_error_kind == "timeout":
            return (
                f"{detail}; cam2ip is listening but has not produced a single frame "
                f"-- it usually means it cannot open the camera. Check the device "
                f"exists, and that it is passed in if cam2ip runs in a container "
                f"(--device=/dev/video0 on Linux). cam2ip's own log gives the reason"
            )

        # Anything else -- refused connection, 404, non-multipart response -- is
        # better described by the error itself than by a guess about the camera.
        if self._last_error:
            return f"{detail} (last error: {self._last_error})"

        # Connected is load-bearing here, not decoration. Without it this branch
        # also caught the case below, and told someone whose connection was
        # refused that cam2ip had accepted it.
        if self._frames_received == 0 and self._connected:
            return (
                f"{detail}; cam2ip accepted the connection but sent no frames -- "
                f"check that it can open the camera"
            )

        # No frames, no error, never connected: the deadline expired while the
        # connection attempt was still outstanding, so we genuinely do not know
        # why yet. Windows reaches this readily -- a refused loopback connect
        # takes about a second to surface there, against microseconds on Linux --
        # and guessing at the camera would be wrong twice over.
        if self._frames_received == 0:
            return (
                f"{detail}; the connection attempt had not finished, so there is "
                f"no error to report yet -- check CAM2IP_BASE_URL points somewhere "
                f"cam2ip is listening, and raise MCP_GRAB_TIMEOUT_S if it is simply "
                f"slower to answer than that"
            )
        return detail


# ---------------------------------------------------------------------------
# Cropping
# ---------------------------------------------------------------------------


def frame_size(data: bytes) -> tuple[int, int] | None:
    """Pixel dimensions of a JPEG, or None if they cannot be read.

    Pillow parses the header lazily -- opening does not decode the scan -- so
    this is cheap enough to run on every status call.
    """
    if PILImage is None:
        return None
    try:
        import io

        with PILImage.open(io.BytesIO(data)) as image:
            return image.size
    except Exception:  # noqa: BLE001 - a frame we cannot parse is not fatal here
        return None


def crop_jpeg(data: bytes, region: tuple[float, float, float, float], quality: int) -> bytes:
    """Return the given fraction of a JPEG, re-encoded.

    The region is fractions of the frame rather than pixels, and that is a
    deliberate interface choice rather than a convenience. A caller picking a
    region is a model that has just looked at the picture and wants "the top
    right quarter"; it knows where things are in the frame proportionally, but it
    does not know the frame is 1920 wide unless something tells it. Fractions
    also survive the capture resolution being changed underneath it, which pixel
    coordinates silently would not.
    """
    if PILImage is None:
        raise RuntimeError(
            "cropping needs Pillow, which is not installed in this image; "
            "call grab_frame without region to get the whole frame"
        )

    import io

    left, top, width, height = region
    for name, value in zip(("x", "y", "width", "height"), region):
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"region {name}={value} must be between 0 and 1")
    if width <= 0 or height <= 0:
        raise ValueError("region width and height must be greater than 0")
    if left + width > 1.0 or top + height > 1.0:
        raise ValueError(
            f"region [{left}, {top}, {width}, {height}] runs past the edge of the "
            f"frame: x+width and y+height must each be at most 1"
        )

    with PILImage.open(io.BytesIO(data)) as image:
        source_w, source_h = image.size
        box = (
            int(left * source_w),
            int(top * source_h),
            max(int((left + width) * source_w), int(left * source_w) + 1),
            max(int((top + height) * source_h), int(top * source_h) + 1),
        )
        cropped = image.crop(box)
        if cropped.mode not in ("RGB", "L"):
            cropped = cropped.convert("RGB")
        out = io.BytesIO()
        # Higher quality than cam2ip's own re-encode, because this is the second
        # lossy pass over the same pixels and it is applied to the region someone
        # is trying to read detail out of. Cheap: the region is a fraction of the
        # frame, so the file is smaller than the original even at 90.
        cropped.save(out, format="JPEG", quality=quality, optimize=True)
        return out.getvalue()


# ---------------------------------------------------------------------------
# Camera controls
# ---------------------------------------------------------------------------


class ControlSession:
    """Camera controls plus the promise to put them back.

    The restore is on a timer rather than tied to a request, because there is no
    request that means "done adjusting". An agent zooms in, grabs a frame, thinks,
    grabs another; any per-call restore would undo the zoom before the second
    grab. What genuinely marks the end is the same thing that ends the stream --
    nobody asking for anything for a while -- so that is what this waits for.

    Every camera interaction refreshes the timer, control writes and frame grabs
    alike, since a caller still taking pictures is still using the settings it
    chose even if it is not changing them.
    """

    def __init__(self, controls: CameraControls, idle_s: float) -> None:
        self._controls = controls
        self._idle_s = idle_s
        self._lock = asyncio.Lock()
        self._last_touch = time.monotonic()
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._last_restore: str | None = None

    @property
    def device(self) -> str:
        return self._controls.device

    def touch(self) -> None:
        """Note camera activity, so the restore timer starts over."""
        self._last_touch = time.monotonic()

    async def _run(self, fn, *args, **kwargs):
        """Run a blocking ioctl off the event loop.

        Control ioctls normally return in microseconds, so this looks like
        overkill -- but they go to a USB device that can be unplugged mid-call,
        and a control write that blocks on a wedged device would otherwise stall
        every frame the server is serving at the time.
        """
        async with self._lock:
            self.touch()
            result = await asyncio.to_thread(fn, *args, **kwargs)
            self._ensure_watchdog()
            return result

    def _ensure_watchdog(self) -> None:
        if (
            self._idle_s > 0
            and not self._closed
            and self._controls.dirty
            and (self._task is None or self._task.done())
        ):
            self._task = asyncio.create_task(self._watch(), name="camera-control-restore")

    async def _watch(self) -> None:
        """Wait out the idle period, then put the controls back.

        Re-checks rather than sleeping once for idle_s: every touch pushes the
        deadline out, so the wait has to be recomputed against the latest one.
        """
        try:
            while not self._closed and self._controls.dirty:
                remaining = self._idle_s - (time.monotonic() - self._last_touch)
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    continue
                async with self._lock:
                    # Checked again under the lock: a control write could have
                    # landed between the deadline passing and the lock being
                    # taken, and restoring out from under it would undo a change
                    # the caller has not seen the result of yet.
                    if time.monotonic() - self._last_touch < self._idle_s:
                        continue
                    await self._restore_locked("idle")
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced through status
            log.warning("automatic control restore failed: %s", exc)

    async def _restore_locked(self, why: str) -> dict[str, int]:
        try:
            restored = await asyncio.to_thread(self._controls.restore)
        except CameraControlError as exc:
            self._last_restore = f"{why}: {exc}"
            log.warning("control restore (%s) incomplete: %s", why, exc)
            raise
        if restored:
            log.info("restored camera controls (%s): %s", why, ", ".join(restored))
            self._last_restore = f"{why}: " + ", ".join(f"{k}={v}" for k, v in restored.items())
        return restored

    # -- operations --------------------------------------------------------

    async def list_all(self) -> list[dict[str, object]]:
        return await self._run(self._controls.get_all)

    async def get(self, name: str) -> int:
        return await self._run(self._controls.get, name)

    async def set_many(self, settings: dict[str, int]) -> dict[str, int]:
        """Apply several controls in one call, reporting what the driver stored.

        Applied in the order given so a caller can express a dependency between
        two of them, and not rolled back on a later failure: the ones that landed
        are real changes to the camera and pretending otherwise would be a lie
        about the state the caller is now in. The error names which succeeded.
        """
        applied: dict[str, int] = {}
        async with self._lock:
            self.touch()
            try:
                for name, value in settings.items():
                    applied[name] = await asyncio.to_thread(self._controls.set, name, value)
            except CameraControlError as exc:
                if applied:
                    raise CameraControlError(
                        f"{exc}. Already applied: "
                        + ", ".join(f"{k}={v}" for k, v in applied.items())
                    ) from None
                raise
            finally:
                self._ensure_watchdog()
        return applied

    async def restore(self) -> dict[str, int]:
        async with self._lock:
            self.touch()
            return await self._restore_locked("requested")

    async def reset_to_defaults(self) -> dict[str, int]:
        async with self._lock:
            self.touch()
            result = await asyncio.to_thread(self._controls.reset_to_defaults)
            self._last_restore = "reset to driver defaults"
            return result

    def status(self) -> dict[str, object]:
        changed = self._controls.changed()
        return {
            "device": self._controls.device,
            "changed_by_this_server": changed or None,
            "restore_after_idle_s": self._idle_s or None,
            "seconds_idle": round(time.monotonic() - self._last_touch, 1),
            "last_restore": self._last_restore,
        }

    async def aclose(self) -> None:
        """Put the controls back on the way out.

        Shutdown is the one moment where leaving the camera changed is certain to
        strand it: there will be no later idle tick to catch it, because there is
        no later anything. Best-effort -- a container being killed does not always
        leave time for a USB round trip -- which is why the idle restore exists
        rather than relying on this.
        """
        self._closed = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown
                pass
        if self._controls.dirty:
            try:
                await self._restore_locked("shutdown")
            except Exception as exc:  # noqa: BLE001 - shutdown
                log.warning("could not restore controls on shutdown: %s", exc)


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

CONFIG = Config.from_env()
_source: FrameSource | None = None
_controls: ControlSession | None = None


def source() -> FrameSource:
    global _source
    if _source is None:
        _source = FrameSource(CONFIG)
    return _source


def controls_available() -> bool:
    """Whether the control tools should exist at all in this deployment.

    Resolved once at import, because MCP advertises its tool list at startup and
    a tool that appears and disappears would be worse than one that is absent:
    a client caches the list it was given.
    """
    if CONFIG.controls_enabled in ("false", "0", "no", "off"):
        return False
    if CONFIG.controls_enabled in ("true", "1", "yes", "on"):
        return True
    return os.path.exists(CONFIG.camera_device)


def controls() -> ControlSession:
    global _controls
    if _controls is None:
        _controls = ControlSession(
            CameraControls(CONFIG.camera_device), CONFIG.control_idle_s
        )
    return _controls


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
    try:
        yield
    finally:
        # Controls first: this is the state that outlives the process, so it is
        # the one worth spending the shutdown window on.
        if _controls is not None:
            await _controls.aclose()
        if _source is not None:
            await _source.aclose()


mcp = MCPServer(
    name="cam2mcp",
    instructions=(
        "Provides live still images from a webcam attached to the host running "
        "this server. Call grab_frame to see what the camera currently sees."
    ),
    lifespan=lifespan,
)


@mcp.tool()
async def grab_frame(
    max_age_s: float | None = None,
    region: list[float] | None = None,
) -> Image:
    """Capture a frame from the attached webcam and return it as a JPEG image.

    The image shows what the camera sees now, not a leftover from an earlier
    call. Waking a cold camera takes a moment, so the first call after a quiet
    spell is slower than the ones after it.

    To see something in more detail there are two options, and region is usually
    the better one. It crops the frame that was already captured, so it costs
    nothing but a re-encode and changes no camera state -- meaning it cannot
    disturb anything else using the camera, and there is nothing to undo. Use
    camera_zoom instead when the detail is not resolved in the full frame at all,
    since that crops inside the camera before the sensor image is scaled down.

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
        region: Crop to [x, y, width, height], as fractions of the frame between
            0 and 1, with x=0, y=0 at the top left. The whole frame is
            [0, 0, 1, 1]; the top right quarter is [0.5, 0, 0.5, 0.5]. Fractions
            rather than pixels, so a region can be chosen from having looked at
            the picture, without knowing what resolution it was captured at.
    """
    if max_age_s is not None and max_age_s < 0:
        raise ValueError("max_age_s must be >= 0")
    if region is not None and len(region) != 4:
        raise ValueError(
            f"region takes exactly 4 numbers -- [x, y, width, height] as "
            f"fractions of the frame -- got {len(region)}"
        )

    frame = await source().grab(max_age_s)
    # A frame grab is camera activity, so it defers the automatic restore of any
    # controls the caller set: still taking pictures means still using them.
    if _controls is not None:
        _controls.touch()

    data = frame.data
    subtype = frame.content_type.partition("/")[2] or "jpeg"
    if region is not None:
        # Off the event loop: decoding and re-encoding a 4K frame is tens of
        # milliseconds of pure CPU, which is long enough to stutter the MJPEG
        # pump keeping frames fresh for everyone else.
        data = await asyncio.to_thread(
            crop_jpeg, data, (region[0], region[1], region[2], region[3]), CROP_QUALITY
        )
        subtype = "jpeg"
    return Image(data=data, format=subtype)


@mcp.tool()
async def camera_status() -> dict[str, object]:
    """Report how the camera stream is doing: connection state, frame counts and errors.

    Useful for diagnosing a camera that returns errors or frames that look
    older than expected. Also reports the captured frame size, and which camera
    controls this server has changed and not yet put back.
    """
    status = source().status()

    frame = source().last_frame_data()
    size = frame_size(frame) if frame is not None else None
    status["frame_size"] = f"{size[0]}x{size[1]}" if size else None

    if controls_available():
        status["controls"] = controls().status()
    return status


# ---------------------------------------------------------------------------
# Camera control tools
# ---------------------------------------------------------------------------
#
# Registered only where there is a device node to drive (see controls_available),
# because a tool whose every call fails is worse than one that was never offered.
# Everything these do is also reachable through camera_set; they exist as
# separate tools because naming the common adjustments makes them findable
# without first enumerating the control list.

if controls_available():

    @mcp.tool()
    async def camera_controls() -> dict[str, object]:
        """List every camera setting, with its range, current value and default.

        Start here when an image needs fixing rather than reframing -- too dark,
        out of focus, wrong colour -- since it shows what this particular camera
        supports rather than what cameras generally do. Settings marked inactive
        are being overridden by an automatic mode; setting one directly turns
        that mode off for you.

        Anything listed can be written with camera_set, including the settings
        that have no dedicated tool: brightness, contrast, saturation, gamma,
        sharpness, backlight_compensation and power_line_frequency.
        """
        session = controls()
        return {
            "device": session.device,
            "controls": await session.list_all(),
            "changed_by_this_server": session.status()["changed_by_this_server"],
            "note": (
                "Changes persist in the camera itself, not in this server, so "
                "they outlive this conversation and affect anything else using "
                "the camera. They are put back automatically once the camera has "
                "been idle, or immediately by camera_reset."
            ),
        }

    @mcp.tool()
    async def camera_zoom(level: int) -> dict[str, object]:
        """Set the camera's digital zoom.

        This crops inside the camera, ahead of the scaling that produces the
        streamed frame, so it recovers real detail that the full frame does not
        resolve. That is its advantage over grab_frame's region argument, and the
        reason to prefer region anyway when the detail *is* already in the frame:
        zoom narrows the field of view for everything else using the camera and
        has to be undone, while a region crop does neither.

        There is no pan or tilt on this class of camera, so a zoomed view is
        centred and cannot be aimed. To look at something off-centre, stay zoomed
        out and crop with grab_frame's region instead.

        Args:
            level: 0 for the full field of view, up to the maximum reported by
                camera_controls as zoom_absolute (100 on the EMEET S600).
        """
        applied = await controls().set_many({"zoom_absolute": level})
        return {"zoom_absolute": applied["zoom_absolute"]}

    @mcp.tool()
    async def camera_focus(
        position: int | None = None, auto: bool | None = None
    ) -> dict[str, object]:
        """Focus the camera, automatically or at a fixed distance.

        Worth reaching for when continuous autofocus will not settle: it hunts on
        close subjects, on low-contrast surfaces, and on anything held up to the
        lens, which is most of the cases where a close look is wanted in the
        first place. Fixing the focus manually also stops it drifting between
        frames while something is being examined.

        The position is a raw driver value, not a distance, and the mapping is
        not linear or documented; find the right one by trying a value, grabbing
        a frame and adjusting. Higher values are nearer on this camera.

        Args:
            position: Focus point, 0 to the maximum reported as focus_absolute
                (1023 on the EMEET S600). Setting it turns autofocus off.
            auto: True to hand focus back to continuous autofocus, False to hold
                the current point. Ignored if position is given.
        """
        settings: dict[str, int] = {}
        if position is not None:
            settings["focus_absolute"] = position
        elif auto is not None:
            settings["focus_automatic_continuous"] = 1 if auto else 0
        else:
            raise ValueError("camera_focus needs either position or auto")

        applied = await controls().set_many(settings)
        return {
            **applied,
            "focus_automatic_continuous": await controls().get(
                "focus_automatic_continuous"
            ),
        }

    @mcp.tool()
    async def camera_exposure(
        time_absolute: int | None = None,
        gain: int | None = None,
        auto: bool | None = None,
    ) -> dict[str, object]:
        """Set exposure time and gain, or hand them back to automatic.

        Three situations need this. A screen or anything else lit by a flickering
        source bands unless the exposure time is matched to the mains frequency
        (see also power_line_frequency via camera_set). A moving subject blurs
        unless the time is shortened. And a dark scene stays dark if the
        automatic mode is metering for a bright window instead -- though
        backlight_compensation via camera_set is often the better fix there.

        Raising gain brightens without lengthening the exposure, at the cost of
        noise. Prefer a longer exposure when the subject is still.

        Args:
            time_absolute: Exposure time in units of 100us, so 100 is 10ms. The
                range this camera accepts is reported as exposure_time_absolute
                (1 to 5000). Setting it switches exposure to manual.
            gain: Sensor gain, 0 to the reported maximum (100 here).
            auto: True to return to automatic exposure, False to hold the current
                settings. Applied after the other two if combined with them.
        """
        settings: dict[str, int] = {}
        if time_absolute is not None:
            settings["exposure_time_absolute"] = time_absolute
        if gain is not None:
            settings["gain"] = gain
        # Last, so that combining auto=True with an explicit time is not
        # self-defeating: setting the time switches to manual, and re-enabling
        # automatic afterwards is the order that matches what was asked for.
        if auto is not None:
            settings["auto_exposure"] = (
                v4l2_controls.EXPOSURE_AUTO if auto else v4l2_controls.EXPOSURE_MANUAL
            )
        if not settings:
            raise ValueError("camera_exposure needs time_absolute, gain or auto")

        return await controls().set_many(settings)

    @mcp.tool()
    async def camera_white_balance(
        temperature: int | None = None, auto: bool | None = None
    ) -> dict[str, object]:
        """Fix the white balance at a colour temperature, or return it to automatic.

        Automatic white balance is a guess about what in the scene is neutral,
        and it shifts as the scene changes. Pin it when colour is the thing being
        judged -- wire colours, resistor bands, indicator LEDs, anything where
        the answer changes if the camera decides the lighting is warmer than it
        thought a frame ago.

        Rough guide: 2700-3000K incandescent, 4000K fluorescent, 5000-5500K
        daylight, 6500K overcast. Lower values make the image warmer.

        Args:
            temperature: Colour temperature in kelvin, within the range reported
                as white_balance_temperature (2300-6500 here). Setting it turns
                automatic white balance off.
            auto: True to return to automatic, False to hold the current value.
                Ignored if temperature is given.
        """
        settings: dict[str, int] = {}
        if temperature is not None:
            settings["white_balance_temperature"] = temperature
        elif auto is not None:
            settings["white_balance_automatic"] = 1 if auto else 0
        else:
            raise ValueError("camera_white_balance needs either temperature or auto")

        return await controls().set_many(settings)

    @mcp.tool()
    async def camera_set(settings: dict[str, int]) -> dict[str, object]:
        """Set any camera controls by name, for the ones without a dedicated tool.

        Names and valid values come from camera_controls; brightness, contrast,
        saturation, hue, gamma, sharpness, backlight_compensation and
        power_line_frequency all live here. Values are applied in the order given
        and each is read back, so the reply is what the camera actually stored
        rather than what was asked for -- a driver may clamp or round silently.

        Args:
            settings: Control name to value, for example
                {"sharpness": 96, "backlight_compensation": 1}.
        """
        if not settings:
            raise ValueError("camera_set needs at least one setting")
        return await controls().set_many(settings)

    @mcp.tool()
    async def camera_reset(to_defaults: bool = False) -> dict[str, object]:
        """Undo camera setting changes.

        Worth calling explicitly when finished, even though an idle camera is
        restored automatically: the settings live in the camera's firmware, so
        until then they apply to every other thing that opens it.

        Args:
            to_defaults: False (the default) puts back only what this server
                changed, leaving anything set by another application alone. True
                sets every control to the driver's default instead, which is the
                way to clear state this server did not create -- an earlier
                session that exited without restoring, or another application's
                leftovers.
        """
        session = controls()
        if to_defaults:
            return {"reset": "driver defaults", "controls": await session.reset_to_defaults()}
        restored = await session.restore()
        return {
            "reset": "values this server changed",
            "controls": restored or "nothing had been changed",
        }


# ---------------------------------------------------------------------------
# Microphone
# ---------------------------------------------------------------------------


def audio_available() -> bool:
    """Whether to offer recording. Requires being switched on, not just possible."""
    if CONFIG.audio_enabled not in ("true", "1", "yes", "on", "auto"):
        return False
    if CONFIG.audio_enabled == "auto":
        return audio_capture.available(CONFIG.audio_device or None)
    return True


if audio_available():

    @mcp.tool()
    async def record_audio(seconds: float = 3.0) -> list[ContentBlock]:
        """Record a short clip from the webcam's microphone and report its level.

        The microphone is a separate device from the camera sharing the same
        cable, so recording neither needs nor disturbs the video stream, and
        nothing lights up while it happens.

        The reply is the audio itself plus a measured level, because most
        questions here are answered by the level alone: whether a machine is
        still running, whether a room is occupied, whether the microphone is
        connected at all. A dead channel and a quiet room are indistinguishable
        by ear on a short clip and obvious in the numbers.

        Args:
            seconds: How long to record, up to 60. Longer clips are rarely more
                informative for a level reading; 2-5s is enough to tell a running
                machine from a stopped one.
        """
        device = audio_capture.find_device(CONFIG.audio_device or None)
        wav = await audio_capture.record(device, seconds)
        # Off the event loop: the sum-of-squares over a long clip is real CPU,
        # and the MJPEG pump is running underneath.
        levels = await asyncio.to_thread(audio_capture.analyse, wav)
        # Built as content blocks rather than returned as a (str, Audio) pair:
        # the tool-result serializer handles a single Image or Audio helper, and
        # a list of ContentBlocks, but not a list with a helper inside it -- that
        # fails at call time with "unable to serialize unknown type".
        return [
            TextContent(type="text", text=json.dumps({"device": device, **levels}, indent=2)),
            Audio(data=wav, format="wav").to_audio_content(),
        ]


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
