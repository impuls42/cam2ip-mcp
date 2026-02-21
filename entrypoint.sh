#!/bin/sh
set -e

# Whether to start the built-in cam2ip server (set to "false" to use an external one)
CAM2IP_ENABLED=${CAM2IP_ENABLED:-true}

# Default camera index if not specified (0 = /dev/video0, 1 = /dev/video1, etc.)
CAM2IP_INDEX=${CAM2IP_INDEX:-0}

CAM2IP_PID=""

if [ "$CAM2IP_ENABLED" = "true" ] || [ "$CAM2IP_ENABLED" = "1" ]; then
    # Start cam2ip in the background
    echo "Starting cam2ip on ${CAM2IP_BIND_ADDR} with camera index ${CAM2IP_INDEX}..."
    cam2ip --bind-addr "${CAM2IP_BIND_ADDR}" --index "${CAM2IP_INDEX}" &
    CAM2IP_PID=$!

    # Wait a moment for cam2ip to start
    sleep 2

    # Check if cam2ip is running
    if ! kill -0 $CAM2IP_PID 2>/dev/null; then
        echo "ERROR: cam2ip failed to start"
        exit 1
    fi

    echo "cam2ip started successfully (PID: $CAM2IP_PID)"
else
    echo "cam2ip disabled (CAM2IP_ENABLED=${CAM2IP_ENABLED}), skipping..."
fi

echo "Starting MCP server (mode: ${MCP_MODE:-stdio})..."

if [ -n "$CAM2IP_PID" ]; then
    # cam2ip is running – launch MCP in foreground, manage both
    python /app/cam2ip_mcp_server.py &
    MCP_PID=$!

    # Function to handle shutdown
    shutdown() {
        echo "Shutting down..."
        kill $CAM2IP_PID 2>/dev/null || true
        kill $MCP_PID 2>/dev/null || true
        wait $CAM2IP_PID 2>/dev/null || true
        wait $MCP_PID 2>/dev/null || true
        exit 0
    }

    # Trap signals
    trap shutdown SIGTERM SIGINT

    # Wait for either process to exit
    wait -n

    # If we get here, one process exited, so shut down gracefully
    shutdown
else
    # No cam2ip – run MCP server directly in the foreground
    exec python /app/cam2ip_mcp_server.py
fi
