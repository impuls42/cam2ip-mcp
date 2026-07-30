# Stage 1: Build the cam2ip binary
#
# cam2ip is pure Go now (korandiz/v4l talks to V4L2 through syscalls, and JPEG
# coding goes through gen2brain/jpegn), so CGO is off: no libjpeg-turbo, no
# v4l-utils headers, and no emulated cross-compilation for arm64. The old
# `-tags turbo` build tag no longer exists upstream -- the libjpeg backend is
# `-tags libjpeg` and needs CGO, which is not worth reintroducing.
FROM --platform=$BUILDPLATFORM golang:1.26-alpine AS cam2ip-builder

ARG TARGETARCH

# cam2ip's own revision, for its startup banner. Without it the banner is
# misleading rather than merely absent: cam2ip falls back to Go's
# debug.ReadBuildInfo, which stamps whichever git tree the build ran in -- so
# building the submodule from this repo reports *this* repo's HEAD as the cam2ip
# version, and inside the image (where .dockerignore drops .git) it reports
# "(devel)". Either way someone debugging reads the wrong thing.
#   docker build --build-arg CAM2IP_VERSION=$(git -C cam2ip rev-parse --short HEAD) .
ARG CAM2IP_VERSION=""

WORKDIR /build

# Copy go.mod/go.sum first so dependency download caches independently of source.
COPY cam2ip/go.mod cam2ip/go.sum ./
RUN go mod download

COPY cam2ip/ ./

RUN CGO_ENABLED=0 GOOS=linux GOARCH=$TARGETARCH go build \
    -o cam2ip \
    -trimpath \
    -ldflags "-s -w ${CAM2IP_VERSION:+-X main.version=$CAM2IP_VERSION}" \
    github.com/gen2brain/cam2ip/cmd/cam2ip


# Stage 2: Final runtime image
FROM python:3.12-alpine

# v4l-utils is not needed to capture, but v4l2-ctl earns its keep when a camera
# will not open and someone has to find out what the device actually supports.
# alsa-utils provides arecord, which is how record_audio captures -- a binding
# would mean building a C extension for two architectures on musl, for one
# blocking read. Both are small; neither is loaded unless used.
RUN apk add --no-cache v4l-utils alsa-utils ca-certificates

COPY --from=cam2ip-builder /build/cam2ip /usr/local/bin/cam2ip

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY cam2mcp_server.py cam2ip_probe.py v4l2_controls.py audio_capture.py ./
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Both licenses ship with the image. The MIT one covers what is in /app; cam2ip's
# GPL-3.0 terms are not optional here, because conveying the binary means
# conveying its license with it. The submodule pin in .gitmodules is what points
# a recipient at the corresponding source.
COPY LICENSE /usr/share/licenses/cam2mcp/LICENSE
COPY cam2ip/COPYING /usr/share/licenses/cam2ip/COPYING

# cam2ip HTTP server + MCP HTTP server
EXPOSE 56000 3000

# cam2ip reads its own CAM2IP_* variables (the prefix comes from the binary
# name), so anything it accepts as a flag can be set here or at run time --
# CAM2IP_WIDTH, CAM2IP_QUALITY, CAM2IP_ROTATE and friends included.
#
# CAMERA_DEVICE is where the control tools look for the camera; CAMERA_CONTROLS
# is left at "auto" so they are offered when that node was passed in and quietly
# skipped when it was not -- the frame path reaches the camera through cam2ip
# over HTTP and needs no device of its own, so a container without one is a
# supported setup rather than a misconfiguration.
ENV CAM2IP_ENABLED=true \
    CAM2IP_BASE_URL=http://127.0.0.1:56000 \
    CAM2IP_HTTP_TIMEOUT_S=5.0 \
    CAM2IP_BIND_ADDR=0.0.0.0:56000 \
    CAM2IP_INDEX=0 \
    CAM2IP_LAZY=true \
    CAM2IP_TIMESTAMP=true \
    MCP_MODE=stdio \
    MCP_HTTP_HOST=0.0.0.0 \
    MCP_HTTP_PORT=3000 \
    CAMERA_DEVICE=/dev/video0 \
    CAMERA_CONTROLS=auto \
    MCP_CONTROL_IDLE_S=120 \
    AUDIO_CAPTURE=false

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["/entrypoint.sh", "healthcheck"]

ENTRYPOINT ["/entrypoint.sh"]
