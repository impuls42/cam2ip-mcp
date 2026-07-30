#!/bin/sh
#
# Container entrypoint: bring up cam2ip, then hand the process over to the MCP
# server.
#
# Two rules shape this script, both because of stdio transport:
#
#   * Nothing may write to stdout. In MCP_MODE=stdio, stdout is the JSON-RPC
#     channel -- a stray "Starting..." line corrupts the protocol. All logging
#     goes to stderr, and cam2ip's stdout is redirected there too.
#   * The MCP server must run in the *foreground*, via exec. POSIX sh assigns
#     /dev/null to the stdin of a background command when job control is off, so
#     `python server.py &` hands the server an immediately-closed stdin and the
#     stdio transport dies on the spot.
#
set -eu

log() { printf '%s\n' "$*" >&2; }

CAM2IP_ENABLED=${CAM2IP_ENABLED:-true}
CAM2IP_BIND_ADDR=${CAM2IP_BIND_ADDR:-0.0.0.0:56000}
CAM2IP_PORT=${CAM2IP_BIND_ADDR##*:}
MCP_SERVER_PATH=${MCP_SERVER_PATH:-/app/cam2ip_mcp_server.py}

cam2ip_enabled() {
    case "$CAM2IP_ENABLED" in
        true | 1 | yes | on) return 0 ;;
        *) return 1 ;;
    esac
}

# Wait for cam2ip to accept connections, failing fast if it exits first.
wait_for_cam2ip() {
    python - "$1" "$2" <<'PY'
import os
import socket
import sys
import time

port, pid = int(sys.argv[1]), int(sys.argv[2])
deadline = time.monotonic() + 30

while time.monotonic() < deadline:
    try:
        os.kill(pid, 0)
    except OSError:
        sys.exit("cam2ip exited during startup (see its output above)")
    try:
        socket.create_connection(("127.0.0.1", port), 1).close()
        sys.exit(0)
    except OSError:
        time.sleep(0.2)

sys.exit(f"cam2ip did not start listening on port {port} within 30s")
PY
}

# `healthcheck`: is cam2ip still accepting connections? Deliberately a bare TCP
# connect -- requesting a frame would dequeue one, and would wake the camera
# every interval, defeating CAM2IP_LAZY.
if [ "${1:-}" = "healthcheck" ]; then
    cam2ip_enabled || exit 0
    exec python -c "import socket,sys; socket.create_connection(('127.0.0.1', int(sys.argv[1])), 3).close()" "$CAM2IP_PORT"
fi

# Anything that looks like a flag is for cam2ip itself, so `docker run <image>
# --list-devices` works for finding out which camera index to use.
case "${1:-}" in
    -*) log "running cam2ip directly: $*"; exec cam2ip "$@" ;;
esac

if cam2ip_enabled; then
    # cam2ip reads its own CAM2IP_* environment variables (flagconf derives the
    # prefix from the binary name), so every flag it supports -- CAM2IP_WIDTH,
    # CAM2IP_QUALITY, CAM2IP_ROTATE, CAM2IP_DEVICE, CAM2IP_LAZY and the rest --
    # is configurable without this script knowing about any of them.
    log "starting cam2ip on ${CAM2IP_BIND_ADDR} (camera index ${CAM2IP_INDEX:-0})"
    cam2ip >&2 &
    CAM2IP_PID=$!

    if ! wait_for_cam2ip "$CAM2IP_PORT" "$CAM2IP_PID"; then
        kill "$CAM2IP_PID" 2>/dev/null || true
        exit 1
    fi

    log "cam2ip is up (pid ${CAM2IP_PID})"
else
    log "cam2ip disabled (CAM2IP_ENABLED=${CAM2IP_ENABLED}), expecting one at ${CAM2IP_BASE_URL:-http://127.0.0.1:56000}"
fi

log "starting MCP server (mode ${MCP_MODE:-stdio})"

# exec so the MCP server becomes PID 1: it inherits the container's stdin and
# stdout, and receives SIGTERM directly on `docker stop`. cam2ip is left as a
# child and goes away with the container.
exec python "$MCP_SERVER_PATH"
