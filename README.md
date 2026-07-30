# cam2mcp

An MCP server that hands a model live still images from a webcam, packaged with
[cam2ip](https://github.com/gen2brain/cam2ip) in a single container.

- **`grab_frame`** — returns a JPEG of what the camera sees right now
- **`camera_status`** — connection state, frame counts and last error, for when
  something looks wrong
- **Three transports**: stdio, SSE, and Streamable HTTP
- **Multi-arch**: `linux/amd64` and `linux/arm64`

> **This project was renamed from `cam2ip-mcp` to `cam2mcp`.** GitHub redirects
> the old repository URL, but the container registry does not, so the old
> `ghcr.io/impuls42/cam2ip-mcp` package has been deleted rather than left
> serving a build that would never be updated again. Repoint anything still
> pulling that path at `ghcr.io/impuls42/cam2mcp`; it now fails outright
> instead of quietly handing back a stale image.

## Quick Start

### As a stdio server (Claude Desktop, Cline, ...)

The MCP client launches the container and talks to it over stdin/stdout:

```json
{
  "mcpServers": {
    "cam2mcp": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "--device=/dev/video0:/dev/video0",
        "ghcr.io/impuls42/cam2mcp:latest"
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
  ghcr.io/impuls42/cam2mcp:latest
```

Clients connect to `http://localhost:3000/mcp`.

### With Docker Compose

```bash
git clone --recursive https://github.com/impuls42/cam2mcp.git
cd cam2mcp

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
   on. They arrive in an instant burst rather than at the frame rate, so a count
   alone would not flush a queue deeper than it assumes. This also covers a
   camera's auto-exposure ramp.

   The number actually dropped is `max(MCP_WARMUP_FRAMES, MCP_WARMUP_S × fps)`,
   and it has to exceed the driver's queue depth. Which term binds is not the one
   you might expect: at 30fps the 0.25s window is ~7.5 frames, so the *window*
   decides and the 5-frame count never does; below about 20fps the count takes
   over. Measured drops were 7–8 on both Linux/V4L2 and macOS/AVFoundation at
   30fps — the window doing the work in both cases.

   On the depth it has to beat: `korandiz/v4l` *requests* four buffers and maps
   however many the driver grants, checking only that the count is non-zero. V4L2
   permits a driver to grant more than asked, so in principle the depth is not
   fixed — but `uvcvideo`, which is what a USB webcam uses, was measured granting
   exactly the requested count at every value from 1 to 32, with no upward bump.
   Its depth is therefore pinned at the four requested, independently of frame
   rate, and the 5-frame floor covers it on its own. Measured directly from
   `v4l2_buffer.timestamp`: after any stall from 0.5s to 30s, exactly four frames
   come back stale and the fifth is current, with only their *age* growing.

   If you do meet a driver that inflates the request beyond what the formula
   covers at your frame rate, raise `MCP_WARMUP_FRAMES` to match.
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
coincide. This was measured: a snapshot taken through the old path came back
stamped `13:27:29` while showing a scene clock reading `13:26:06`.

One limitation of the overlay: cam2ip draws it as light glyphs with no contrast
plate, so it washes out against a bright scene — which is precisely when someone
is likely to be squinting at an overexposed stream trying to work out what it is
showing. Turn it off with `CAM2IP_TIMESTAMP=false` if it is not earning its keep.

### This is a V4L2 problem specifically

The buffer queue described above is Linux's. cam2ip's macOS backend
(AVFoundation) keeps a single always-overwritten slot and blocks until the next
frame arrives, so it cannot go stale this way, and testing confirmed neither path
returns an old frame there — including the one-shot snapshot that fails on Linux.
Windows behaves like macOS in this respect.

The machinery here is harmless on those platforms rather than useless: the
max-age bound still catches a stalled pipeline, and it was observed refusing 15s-
and 71s-old cached frames on macOS. But if you are running on a backend with no
stale-queue problem and want the cold-start latency back, `MCP_WARMUP_S=0` costs
you nothing there.

### Reproducing the stale queue, if you want to see it

The queue only holds stale frames while something keeps the device open *and*
stops draining it. `CAM2IP_LAZY` decides the first half, and the default makes
this harder to observe, not easier:

- **`CAM2IP_LAZY=false`** holds the device open whether or not anything is
  subscribed, so the queue keeps four frames from whenever it last drained,
  however long ago. This is the setting under which a snapshot was measured
  returning a scene 68 seconds old.
- **`CAM2IP_LAZY=true`** (the default) releases the device once nothing is
  subscribed, which frees the buffers, so the next request reopens and captures
  fresh ones. Measured at 10s, 20s and 60s+ after a stream dropped, `/jpeg`
  returned the current scene every time — on the default settings a snapshot
  after an idle could not be made stale by any idle length tried.

So reproducing it through cam2ip needs `CAM2IP_LAZY=false`. Idling against the
defaults measures the reopen path instead.

Below cam2ip the behaviour is unconditional, and that is what the fix is sized
against. Reading the driver directly — `VIDIOC_REQBUFS(4)`, stream on, stall,
then drain — returns exactly four stale frames and then a current one, for every
stall from 0.5s to 30s. Only their age grows; the depth never does.

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

> **On Windows, these variables do nothing if the binary is named `cam2ip.exe`.**
> cam2ip derives the prefix from its own filename, and the mapping uppercases and
> replaces hyphens but not dots — so `cam2ip.exe` looks for `CAM2IP.EXE_WIDTH`
> rather than `CAM2IP_WIDTH`. Nothing warns you; the flags simply keep their
> defaults. Either build the binary with no extension (Windows runs an
> extensionless PE file fine) or pass command-line flags instead. This does not
> affect the container, where the binary is `cam2ip`.

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

`camera_status` takes none, and is where to look first if `grab_frame` is
failing. It leads with `state`, which is the field to read:

| `state` | Meaning |
|---|---|
| `idle` | No subscription; the camera is released. Normal between requests. |
| `streaming` | Frames arriving at the frame rate. |
| `reconnecting` | The stream dropped and is being re-established. |
| `connected_but_no_frames` | cam2ip is reachable but has stopped sending — an unplugged or wedged camera. |
| `failed` | An error a retry cannot fix, such as a 404 or a non-MJPEG response. |

`stream_connected` and `stream_running` describe the HTTP conversation with
cam2ip, **not** the camera: both stay `true` when the camera is unplugged,
because cam2ip holds the response open and simply stops writing to it. Read
`state` and the frame counters instead. Also reported: the age of the frame in
memory, the last error, and whether that error is one a retry could fix.

`frames_received` and `frames_published` are cumulative for the life of the
server process, not per-connection. Their difference is every warm-up discard
since startup added together, so to see one cycle's discard take the delta across
that cycle rather than reading the totals.
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
docker build -f Containerfile -t cam2mcp:dev .
CAM2MCP_IMAGE=cam2mcp:dev python -m pytest tests/test_image.py -v
```

These are skipped unless `CAM2MCP_IMAGE` is set, and need Linux, since they
use `--network host` to let the container reach the fake camera. CI runs them
before anything is published, so a green build alone cannot ship an image that
fails to start.

### On macOS

The container cannot see your camera. Docker Desktop runs containers in a Linux
VM with no passthrough for the host's AVFoundation devices, so there is no
`/dev/video0` inside it — `--list-devices` returns nothing and `docker compose
up` fails outright with `error gathering device information while adding custom
device "/dev/video0"`. This is a Docker Desktop limitation, not a problem with
the image, which builds and runs fine otherwise.

Run the two processes natively instead. cam2ip builds without CGO on darwin too
(it reaches AVFoundation through `purego`, not cgo):

```bash
mkdir -p bin
(cd cam2ip && GOTOOLCHAIN=auto CGO_ENABLED=0 go build \
  -ldflags "-X main.version=$(git rev-parse --short HEAD)" -o ../bin/cam2ip ./cmd/cam2ip)
./bin/cam2ip --bind-addr 127.0.0.1:56000 &

python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
CAM2IP_BASE_URL=http://127.0.0.1:56000 ./.venv/bin/python cam2mcp_server.py
```

The binary must be named exactly `cam2ip` — it derives its `CAM2IP_*` variable
prefix from its own filename, so any other name silently ignores that
configuration. The `-ldflags` are what make `--version` report cam2ip's revision
rather than this repo's; see the note in the Containerfile.

### Finding your camera

```bash
docker run --rm --device=/dev/video0:/dev/video0 \
  ghcr.io/impuls42/cam2mcp:latest --list-devices
```

Any flag-shaped argument is passed straight to cam2ip, so `--help` and
`--version` work the same way.

### Building

```bash
git clone --recursive https://github.com/impuls42/cam2mcp.git
cd cam2mcp
docker build -f Containerfile -t cam2mcp \
  --build-arg CAM2IP_VERSION=$(git -C cam2ip rev-parse --short HEAD) .
```

**BuildKit is required.** The builder stage uses `--platform=$BUILDPLATFORM` so
it compiles natively and cross-compiles for the target, which the legacy builder
cannot parse — it fails with `"" is an invalid OS component`. Any current Docker
uses BuildKit by default; if yours does not, set `DOCKER_BUILDKIT=1`.

The `--build-arg` is what makes the startup banner report cam2ip's own revision.
Without it cam2ip falls back to Go's build info, which stamps whichever git tree
the build ran in — so it would report *this* repo's commit as the cam2ip version.

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

**The camera was unplugged and replugged.** Getting back from this needs three
things, and the shipped compose file only gives you one of them:

1. `CAM2IP_LAZY=true` (the default) is the only setting that *can* self-heal.
   cam2ip closes the device while nothing is subscribed, so the next request
   reopens it. With `CAM2IP_LAZY=false` it holds the original handle and retries
   reads against a device that is gone — measured at every grab failing until
   the container is restarted, with no recovery on its own.
2. The container has to be able to *see* the replugged device. A compose
   `devices:` mapping cannot: it is resolved once at container-create, so the
   node inside the container keeps pointing at a minor number the kernel has
   moved on from. Under a plain mapping neither `CAM2IP_LAZY` setting survives a
   replug, and the failure is not cam2ip's.
3. The kernel may not return the camera on the same minor, especially if it was
   held open when it disappeared — it can come back as `video1`. Mapping a
   `/dev/v4l/by-id/...` path instead of `/dev/video0` makes a restart enough,
   since the symlink follows the device. See the notes in `docker-compose.yml`
   for that and for the `/dev` bind-mount that avoids the restart entirely.

`camera_status` reports `state: connected_but_no_frames` throughout, which is the
signature to look for. Note that `stream_connected` and `stream_running` both
stay `true`: they describe the HTTP conversation with cam2ip, which is untouched
by the camera going away. The container `HEALTHCHECK` stays green too, for the
same reason — it probes the two HTTP ports, neither of which depends on a camera.
It is a check that the processes are alive, not that the camera is.

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
  ghcr.io/impuls42/cam2mcp:latest
```

## License

The MCP server, entrypoint, tests and packaging in this repository are MIT
licensed; see [LICENSE](LICENSE).

The bundled [cam2ip](https://github.com/gen2brain/cam2ip) is **GPL-3.0** and
stays that way — its terms are in `cam2ip/COPYING`, and they govern the cam2ip
binary inside the published image regardless of the license on this repository.
The two are aggregated rather than combined: cam2ip is built from an unmodified
upstream submodule into its own binary, and the MCP server talks to it over
HTTP as a separate process, so no GPL work is linked into or derived from the
MIT-licensed code here.

If you redistribute the image, you are redistributing a GPL-3.0 binary and owe
its recipients the corresponding source. The submodule pin is what discharges
that: `.gitmodules` names the upstream repository and the checked-out commit
identifies the exact revision built.
