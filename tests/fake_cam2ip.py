"""A fake cam2ip that reproduces the V4L2 buffer queue behaviour.

This is the part of the real stack that made frames stale, modelled closely
enough to regress against:

* The driver captures into a fixed pool of buffers (4, like korandiz/v4l's
  TurnOn) and then *stalls* -- filled buffers sit in the done-queue until a
  reader dequeues one. It does not overwrite them.
* A read dequeues exactly one buffer, oldest first.

So a pipeline that idles hands back whatever was in front of the lens when it
last drained, and the next few reads walk forward through the rest of the stale
queue. ``GET /jpeg`` does one read per request; ``GET /mjpeg`` reads in a loop
for as long as the client stays subscribed.
"""

from __future__ import annotations

import asyncio
import json
import struct
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

BOUNDARY = "--boundary"  # the literal boundary cam2ip uses

JPEG_SOI = b"\xff\xd8"
JPEG_COM = b"\xff\xfe"
JPEG_EOI = b"\xff\xd9"


def make_frame(seq: int, captured_at: float) -> bytes:
    """Build a JPEG-shaped blob carrying its own identity in a COM segment.

    Structurally a valid JPEG stream (SOI, comment, EOI) with no image data,
    which is all this needs to be: nothing in the path under test decodes it.
    """
    payload = json.dumps({"seq": seq, "captured_at": captured_at}).encode()
    return JPEG_SOI + JPEG_COM + struct.pack(">H", len(payload) + 2) + payload + JPEG_EOI


def read_frame_meta(data: bytes) -> dict:
    """Recover the {seq, captured_at} a frame was built with."""
    if not data.startswith(JPEG_SOI + JPEG_COM):
        raise ValueError(f"not a fake frame: {data[:16]!r}")
    (length,) = struct.unpack(">H", data[4:6])
    return json.loads(data[6 : 4 + length])


@dataclass
class CaptureQueue:
    """The driver's buffer queue: bounded, FIFO, and it stalls when full."""

    depth: int = 4
    fps: float = 30.0
    frames: deque[bytes] = field(default_factory=deque)
    captured: int = 0
    dequeued: int = 0
    _new: asyncio.Event = field(default_factory=asyncio.Event)

    async def run_driver(self) -> None:
        """Fill free buffers at `fps`, then stall until a reader frees one."""
        period = 1.0 / self.fps
        while True:
            await asyncio.sleep(period)
            if len(self.frames) < self.depth:
                self.captured += 1
                self.frames.append(make_frame(self.captured, time.time()))
                self._new.set()

    async def capture(self) -> bytes:
        """Dequeue the oldest filled buffer, waiting if none is ready."""
        while not self.frames:
            self._new.clear()
            await self._new.wait()
        self.dequeued += 1
        return self.frames.popleft()

    async def wait_until_stalled(self, timeout: float = 5.0) -> int:
        """Block until every buffer is filled, then return the capture count.

        Once the queue is full the driver has nowhere to put new frames, so the
        count stops moving. That makes it a watermark: any later frame with a
        higher sequence number must have been captured after this returned,
        which is what lets the freshness tests avoid timing assumptions.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while len(self.frames) < self.depth:
            if loop.time() > deadline:
                raise AssertionError(
                    f"queue never filled ({len(self.frames)}/{self.depth}); "
                    f"is something still draining it?"
                )
            await asyncio.sleep(0.01)
        return self.captured


class FakeCam2ip:
    def __init__(self, *, depth: int = 4, fps: float = 30.0, delay_s: float = 0.0,
                 mjpeg_status: int = 200, stall_headers: bool = False) -> None:
        self.queue = CaptureQueue(depth=depth, fps=fps)
        self.delay_s = delay_s
        self.mjpeg_status = mjpeg_status
        self.mjpeg_connections = 0
        self.jpeg_requests = 0
        self.silent = False
        self.stall_headers = stall_headers
        self.base_url = ""
        self.app = Starlette(
            routes=[
                Route("/jpeg", self._jpeg),
                Route("/mjpeg", self._mjpeg),
            ]
        )

    async def _jpeg(self, request: Request) -> Response:
        """One read per request -- the shape that returned stale frames."""
        self.jpeg_requests += 1
        frame = await self.queue.capture()
        return Response(
            frame,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store, no-cache", "Connection": "close"},
        )

    def go_silent(self) -> None:
        """Hold the connection open but stop sending frames.

        What an unplugged USB camera looks like from this side: cam2ip keeps the
        HTTP response open and its capture loop just fails, so the link stays up
        while frames stop. Nothing at the HTTP layer signals the difference
        between this and a very slow camera.
        """
        self.silent = True

    async def _mjpeg(self, request: Request) -> Response:
        if self.mjpeg_status != 200:
            return Response("nope", status_code=self.mjpeg_status)

        if self.stall_headers:
            # What a cam2ip that cannot open the camera does: its MJPEG handler
            # writes no response headers until it has a frame for the first part,
            # so the client waits for a status line that never arrives and times
            # out having never seen the response begin. Distinct from go_silent(),
            # where headers were sent and frames stopped afterwards.
            await asyncio.sleep(3600)

        self.mjpeg_connections += 1

        async def parts() -> AsyncIterator[bytes]:
            # Byte layout matches Go's mime/multipart writer, which is what
            # cam2ip streams: no leading CRLF on the first part, one before
            # every delimiter after it.
            first = True
            while True:
                while self.silent:
                    await asyncio.sleep(0.05)
                frame = await self.queue.capture()
                prefix = b"" if first else b"\r\n"
                first = False
                yield (
                    prefix
                    + f"--{BOUNDARY}\r\n".encode()
                    + b"Content-Type: image/jpeg\r\n\r\n"
                    + frame
                )
                if self.delay_s:
                    await asyncio.sleep(self.delay_s)

        return StreamingResponse(
            parts(),
            media_type=f"multipart/x-mixed-replace;boundary={BOUNDARY}",
            headers={"Cache-Control": "no-store, no-cache"},
        )


@asynccontextmanager
async def running_fake_cam2ip(**kwargs) -> AsyncIterator[FakeCam2ip]:
    """Serve a FakeCam2ip on an ephemeral port for the duration of the block."""
    fake = FakeCam2ip(**kwargs)
    config = uvicorn.Config(
        fake.app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(config)

    serve_task = asyncio.create_task(server.serve())
    driver_task = asyncio.create_task(fake.queue.run_driver())
    try:
        while not server.started:
            if serve_task.done():
                serve_task.result()  # re-raise the startup failure
            await asyncio.sleep(0.01)

        port = server.servers[0].sockets[0].getsockname()[1]
        fake.base_url = f"http://127.0.0.1:{port}"
        yield fake
    finally:
        driver_task.cancel()
        server.should_exit = True
        server.force_exit = True
        for task in (driver_task, serve_task):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - teardown
                pass


def _exit_with_parent() -> None:
    """Ask the kernel to signal us when whatever launched us dies.

    entrypoint.sh execs the MCP server over itself and leaves cam2ip running as
    a background child, so a test that terminates the server orphans this
    process. A container tears it down with the PID namespace; a test run would
    leave it holding a port until someone noticed. Linux-only, and a no-op
    elsewhere -- the tests that need it are Linux-only too.
    """
    if sys.platform != "linux":
        return

    import ctypes
    import signal

    PR_SET_PDEATHSIG = 1
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    except OSError:  # pragma: no cover - no libc to talk to
        pass


def main() -> None:
    """Stand in for the cam2ip binary, reading CAM2IP_* config like the real one.

    Lets the entrypoint tests exercise container startup without a webcam.
    """
    import os

    _exit_with_parent()

    host, _, port = os.environ.get("CAM2IP_BIND_ADDR", "0.0.0.0:56000").rpartition(":")

    async def serve() -> None:
        fake = FakeCam2ip(depth=4, fps=30.0)
        config = uvicorn.Config(
            fake.app, host=host or "0.0.0.0", port=int(port),
            log_level="warning", lifespan="off",
        )
        driver = asyncio.create_task(fake.queue.run_driver())
        try:
            await uvicorn.Server(config).serve()
        finally:
            driver.cancel()

    print(f"fake cam2ip listening on {host}:{port}", file=sys.stderr)
    asyncio.run(serve())


if __name__ == "__main__":
    main()
