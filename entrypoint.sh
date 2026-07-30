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
CAM2IP_BASE_URL=${CAM2IP_BASE_URL:-http://127.0.0.1:56000}
MCP_MODE=${MCP_MODE:-stdio}
MCP_HTTP_HOST=${MCP_HTTP_HOST:-0.0.0.0}
MCP_HTTP_PORT=${MCP_HTTP_PORT:-3000}
MCP_SERVER_PATH=${MCP_SERVER_PATH:-/app/cam2mcp_server.py}
MCP_PROBE_PATH=${MCP_PROBE_PATH:-/app/cam2ip_probe.py}

export CAM2IP_BASE_URL CAM2IP_BIND_ADDR

cam2ip_enabled() {
    case "$CAM2IP_ENABLED" in
        true | 1 | yes | on) return 0 ;;
        *) return 1 ;;
    esac
}

mcp_serves_http() {
    case "$MCP_MODE" in
        sse | streamable-http) return 0 ;;
        *) return 1 ;;
    esac
}

# `healthcheck`: is everything this container depends on still answering?
#
# Probes CAM2IP_BASE_URL rather than loopback, because cam2ip only listens on
# what CAM2IP_BIND_ADDR bound it to -- see cam2ip_probe.py. In an HTTP mode the
# MCP port is checked too, so the check still means something when cam2ip is
# external. With neither (stdio against an external cam2ip) there is nothing to
# probe: the MCP server is PID 1, so the container being up is the whole signal.
if [ "${1:-}" = "healthcheck" ]; then
    set --
    if cam2ip_enabled; then
        set -- "$@" --url "$CAM2IP_BASE_URL"
    fi
    if mcp_serves_http; then
        set -- "$@" --host-port "${MCP_HTTP_HOST}:${MCP_HTTP_PORT}"
    fi
    exec python "$MCP_PROBE_PATH" --timeout 2 "$@"
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

    # Wait on the address the MCP server will use, not on loopback: an operator
    # who pinned CAM2IP_BIND_ADDR to one interface must not be told cam2ip
    # failed to start, and a CAM2IP_BASE_URL that points somewhere cam2ip is not
    # should be caught here rather than at the first frame request.
    if ! python "$MCP_PROBE_PATH" --url "$CAM2IP_BASE_URL" --timeout 30 --pid "$CAM2IP_PID"; then
        kill "$CAM2IP_PID" 2>/dev/null || true
        exit 1
    fi

    log "cam2ip is up (pid ${CAM2IP_PID})"
else
    log "cam2ip disabled (CAM2IP_ENABLED=${CAM2IP_ENABLED}), expecting one at ${CAM2IP_BASE_URL}"
fi

log "starting MCP server (mode ${MCP_MODE:-stdio})"

# exec so the MCP server becomes PID 1: it inherits the container's stdin and
# stdout, and receives SIGTERM directly on `docker stop`. cam2ip is left as a
# child and goes away with the container.
exec python "$MCP_SERVER_PATH"
