# cam2ip MCP Server Container

An MCP server that hands a model live still images from a webcam, packaged with
[cam2ip](https://github.com/gen2brain/cam2ip) in a single container.

- **`grab_frame`** — returns a JPEG of what the camera sees right now
- **`camera_status`** — connection state, frame counts and last error, for when
  something looks wrong
- **Three transports**: stdio, SSE, and Streamable HTTP
- **Multi-arch**: `linux/amd64` and `linux/arm64`

## Quick Start

### As a stdio server (Claude Desktop, Cline, ...)

The MCP client launches the container and talks to it over stdin/stdout:

```json
{
  "mcpServers": {
    "cam2ip": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "--device=/dev/video0:/dev/video0",
        "ghcr.io/impuls42/cam2ip-mcp:latest"
      ]
    }
  }
}
```

`-i` is required: without it the container has no stdin and the server exits
immediately.

### As an HTTP server

```bash
docker run --rm \
  --device=/dev/video0:/dev/video0 \
  -p 56000:56000 -p 3000:3000 \
  -e MCP_MODE=streamable-http \
  ghcr.io/impuls42/cam2ip-mcp:latest
```

Clients connect to `http://localhost:3000/mcp`.

### With Docker Compose

```bash
git clone --recursive https://github.com/impuls42/cam2ip-mcp.git
cd cam2ip-mcp

cp .env.example .env   # optional; edit to taste
docker compose up -d
docker compose logs -f
```

Compose defaults to `streamable-http`, since a background service has nothing
attached to its stdin.

## How frames stay fresh

This is worth understanding before changing any of the `MCP_*` frame settings,
because it is the bug this server exists to avoid.

V4L2 capture works through a queue of memory-mapped buffers — four of them, as
allocated by [`korandiz/v4l`](https://github.com/korandiz/v4l), which is what
cam2ip uses. Once the camera is streaming, the driver fills those buffers and
then stops: a filled buffer stays in the done-queue until somebody dequeues it,
and it is **not** overwritten in the meantime. Every frame read dequeues exactly
one buffer, oldest first.

The consequence is that a capture pipeline which sits idle between requests is
holding four pictures of whatever was in front of the lens the last time it
drained. A single `GET /jpeg` after an idle period returns one of those — a
picture from minutes or hours ago — and the next few requests walk forward
through the rest of the stale queue before catching up to the present. Nothing
is cached anywhere, so no amount of cache-busting on the HTTP request changes
it; the staleness is in the kernel's buffer queue.

So the server does not take one-shot snapshots. Instead:

1. **It holds one MJPEG subscription open.** cam2ip serves all its clients from
   a single capture loop, so one live subscriber keeps that loop dequeuing at
   the camera's frame rate. While the subscription is up the queue stays nearly
   empty, and an arriving frame really is a picture of now.
2. **It discards the first frames after each connect** (`MCP_WARMUP_FRAMES`,
   `MCP_WARMUP_S`). Those are exactly the pre-idle ones the queue was sitting
   on. They arrive in an instant burst rather than at the frame rate, which is
   why there is a time-based window as well as a count — it flushes queues
   deeper than four. This also covers a camera's auto-exposure ramp.
3. **It caps the age of what it serves** (`MCP_FRAME_MAX_AGE_S`), waiting for a
   newer frame instead of answering with an old one. This age is measured from
   when a frame *arrived*, because HTTP carries no capture timestamp — so it
   cannot by itself detect a stale queued frame (that is step 2's job), but it
   does catch a pipeline that stalled or died and left an old frame in memory.

The subscription is dropped once nothing has asked for a frame in
`MCP_STREAM_IDLE_S` seconds, so the camera is released — and its indicator light
goes out — while the server is idle. The next request re-establishes it, paying
the warm-up cost once. That timeout applies while reconnecting too, so a stream
that cannot be established does not retry forever after the request that wanted
it has given up.

`CAM2IP_TIMESTAMP` is on by default, so each frame carries its time in the top
left corner and freshness can be read straight off the picture. Note that
cam2ip stamps a frame when it *reads* it out of the buffer queue, not when the
sensor captured it — which is only the same thing while the pipeline is being
drained continuously. Under the old snapshot approach the stamp would have read
as current on an hours-old picture; with the subscription held open the two
coincide.

## Configuration

Everything is set through environment variables; see
[.env.example](.env.example) for the annotated list.

### cam2ip options

cam2ip reads its own `CAM2IP_*` variables, one per flag, so any option it
supports works without this project knowing about it:

| Variable | Default | Meaning |
|---|---|---|
| `CAM2IP_ENABLED` | `true` | Start the bundled cam2ip; `false` to use an external one |
| `CAM2IP_INDEX` | `0` | Camera index (`0` = `/dev/video0`) |
| `CAM2IP_DEVICE` | — | Select by name instead, substring match; overrides index |
| `CAM2IP_WIDTH` / `CAM2IP_HEIGHT` | `640` / `480` | Frame size |
| `CAM2IP_QUALITY` | `75` | JPEG quality, 1–100 |
| `CAM2IP_DELAY` | `10` | Milliseconds between captures |
| `CAM2IP_ROTATE` | `0` | `90`, `180` or `270` |
| `CAM2IP_FLIP` | — | `horizontal` or `vertical` |
| `CAM2IP_TIMESTAMP` | `true` | Draw the capture time onto the image |
| `CAM2IP_TIME_FORMAT` | `2006-01-02 15:04:05` | Stamp format, in Go's reference layout |
| `CAM2IP_LAZY` | `true` | Open the camera only while a client is subscribed |
| `CAM2IP_BIND_ADDR` | `0.0.0.0:56000` | cam2ip listen address; if narrowed to one interface, `CAM2IP_BASE_URL` must name it too |
| `CAM2IP_HTPASSWD_FILE` | — | Enable basic auth on cam2ip's endpoints |

Run `docker run --rm <image> --help` for the authoritative list — any flag shown
there has a matching variable.

### MCP server options

| Variable | Default | Meaning |
|---|---|---|
| `MCP_MODE` | `stdio` | `stdio`, `sse` or `streamable-http` |
| `MCP_HTTP_HOST` / `MCP_HTTP_PORT` | `0.0.0.0` / `3000` | Bind address for the HTTP transports |
| `CAM2IP_BASE_URL` | `http://127.0.0.1:56000` | Where to reach cam2ip |
| `CAM2IP_HTTP_TIMEOUT_S` | `5.0` | HTTP connect/read timeout toward cam2ip |
| `MCP_FRAME_MAX_AGE_S` | `1.0` | Never serve a frame that arrived longer ago than this |
| `MCP_GRAB_TIMEOUT_S` | `15.0` | Give up waiting for a fresh frame after this long |
| `MCP_WARMUP_FRAMES` | `5` | Frames to discard after connecting; must exceed the driver's buffer count |
| `MCP_WARMUP_S` | `0.25` | Also discard frames for this long after connecting |
| `MCP_STREAM_IDLE_S` | `30.0` | Hold the camera stream open this long after the last request |
| `MCP_LOG_LEVEL` | `INFO` | Log level; logs always go to stderr |
| `MCP_ALLOWED_HOSTS` | — | Comma-separated `Host` allowlist for the HTTP transports |
| `MCP_ALLOWED_ORIGINS` | — | Comma-separated `Origin` allowlist; requires `MCP_ALLOWED_HOSTS` |

Leaving both allowlists unset keeps mcp's defaults — a loopback allowlist when
bound to `127.0.0.1`, no restriction when bound to a public interface. Setting
either replaces those defaults, and mcp then validates `Host` and `Origin`
together with no way to express "any host", so `MCP_ALLOWED_HOSTS` is required as
soon as you restrict anything; setting only `MCP_ALLOWED_ORIGINS` is refused at
startup rather than rejecting every request with a `421`. Hosts alone is the
common case: clients sending no `Origin` header pass, and any request carrying
one is refused, so add `MCP_ALLOWED_ORIGINS` to permit specific browsers.

### Transports

| Mode | Endpoint | Use case |
|---|---|---|
| `stdio` | stdin/stdout | The MCP client launches the container (`docker run -i`) |
| `sse` | `http://host:3000/sse` | Clients without Streamable HTTP support |
| `streamable-http` | `http://host:3000/mcp` | Current MCP spec |

In `stdio` mode, stdout is the JSON-RPC channel. Nothing else may write to it,
which is why every log line from both the entrypoint and cam2ip goes to stderr.

## Using the tools

`grab_frame` takes one optional argument:

- `max_age_s` — maximum acceptable frame age in seconds. Defaults to
  `MCP_FRAME_MAX_AGE_S`. Raise it to trade freshness for a faster reply.

`camera_status` takes none, and reports whether the stream is connected, how
many frames have been received and published, the age of the frame in memory,
and the last error — which is where to look first if `grab_frame` is failing.
`last_error_is_permanent` distinguishes the two kinds: a refused connection or a
timeout is worth retrying and gets ridden out until `MCP_GRAB_TIMEOUT_S`, while a
4xx or a response that is not MJPEG at all cannot be fixed by reconnecting, so
`grab_frame` reports it immediately instead of making the caller wait.

## cam2ip's own interface

While the container runs, cam2ip is reachable directly:

- `http://localhost:56000/` — links to the pages below
- `http://localhost:56000/jpeg` — single snapshot (subject to the stale-queue
  behaviour described above; the MCP tool is the reliable path)
- `http://localhost:56000/mjpeg` — live MJPEG stream
- `http://localhost:56000/html` — viewer page

## Development

### Running the tests

The suite needs no webcam: it runs a fake cam2ip that reproduces the V4L2
buffer-queue behaviour, and drives the server over its real transports.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest
```

Worth knowing about the coverage:

- `tests/test_freshness.py` asserts on the frame's own capture sequence number
  against a watermark taken while the fake camera's queue was stalled, so
  "returned a stale frame" fails independently of timing. It also tests the old
  one-shot `/jpeg` behaviour, to keep a record of what the bug looked like.
- `tests/test_entrypoint.py` runs `entrypoint.sh` with a stand-in cam2ip and
  checks the container startup path — including that stdout stays clean.
- `tests/test_transports.py` launches the server as a subprocess and speaks MCP
  to it over stdio and Streamable HTTP.

### Testing the image

The tests above run against the source tree. To check the built image instead —
that the pinned dependencies resolve inside `python:3.12-alpine`, that the
CGO-free cam2ip binary executes in a runtime stage with no build tools, and that
`docker run -i` gives the server a working stdin:

```bash
docker build -f Containerfile -t cam2ip-mcp:dev .
CAM2IP_MCP_IMAGE=cam2ip-mcp:dev python -m pytest tests/test_image.py -v
```

These are skipped unless `CAM2IP_MCP_IMAGE` is set, and need Linux, since they
use `--network host` to let the container reach the fake camera. CI runs them
before anything is published, so a green build alone cannot ship an image that
fails to start.

### Finding your camera

```bash
docker run --rm --device=/dev/video0:/dev/video0 \
  ghcr.io/impuls42/cam2ip-mcp:latest --list-devices
```

Any flag-shaped argument is passed straight to cam2ip, so `--help` and
`--version` work the same way.

### Building

```bash
git clone --recursive https://github.com/impuls42/cam2ip-mcp.git
cd cam2ip-mcp
docker build -f Containerfile -t cam2ip-mcp .
```

cam2ip builds with `CGO_ENABLED=0` — it is pure Go now, V4L2 access included —
so the builder stage runs natively on the build host and cross-compiles for
arm64 rather than going through emulation.

## Architecture

**Build** (two stages):

1. `golang:1.26-alpine` builds the cam2ip binary, statically and without CGO.
2. `python:3.12-alpine` installs the Python dependencies and receives just the
   binary.

**Runtime**: `entrypoint.sh` starts cam2ip in the background, waits for it to
accept connections, then `exec`s the MCP server so it becomes PID 1 — inheriting
the container's stdin and stdout, and receiving `SIGTERM` directly on
`docker stop`.

## Troubleshooting

**`grab_frame` reports no frames.** Call `camera_status`; `last_error` usually
says why. Then check the container logs for cam2ip's own complaints — a camera
it cannot open is the common case.

**Container exits at startup saying cam2ip is not accepting connections.** The
message prints both `CAM2IP_BASE_URL` and `CAM2IP_BIND_ADDR`, because a mismatch
between them is the usual cause: cam2ip listens only on the address it bound to,
so narrowing the bind to one interface without pointing the base URL at the same
place leaves the MCP server with nowhere to fetch from.

**Camera not accessible.** Confirm the device is passed in
(`--device=/dev/video0:/dev/video0`) and that it exists on the host:

```bash
ls -l /dev/video*
```

If the host requires it, add your user to the `video` group.

**Frames look older than they should.** Compare `last_frame_age_s` from
`camera_status` against `frame_max_age_s`. If warm-up has been lowered, restore
`MCP_WARMUP_FRAMES` to at least 5 — see "How frames stay fresh".

**Port already in use.** Remap it, and tell the MCP server where cam2ip went:

```bash
docker run --rm -i \
  --device=/dev/video0:/dev/video0 \
  -p 8080:8080 \
  -e CAM2IP_BIND_ADDR=0.0.0.0:8080 \
  -e CAM2IP_BASE_URL=http://127.0.0.1:8080 \
  ghcr.io/impuls42/cam2ip-mcp:latest
```

## License

The bundled [cam2ip](https://github.com/gen2brain/cam2ip) is covered by its own
terms, in `cam2ip/COPYING`. This repository does not currently carry a license
file of its own for the MCP server implementation.
