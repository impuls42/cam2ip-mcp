#!/bin/sh
set -e

# Default camera index if not specified (0 = /dev/video0, 1 = /dev/video1, etc.)
CAM2IP_INDEX=${CAM2IP_INDEX:-0}

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
echo "Starting MCP server..."

# Start MCP server in foreground
# The MCP server uses stdio, so it runs in the foreground
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
