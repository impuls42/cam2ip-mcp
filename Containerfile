# Stage 1: Build cam2ip binary
FROM golang:1.23-alpine AS cam2ip-builder

# Install build dependencies
RUN apk add --no-cache \
    git \
    make \
    build-base \
    pkgconfig \
    v4l-utils-dev \
    libjpeg-turbo-dev \
    linux-headers

WORKDIR /build

# Copy go.mod and go.sum first for better layer caching
COPY cam2ip/go.mod cam2ip/go.sum ./
RUN go mod download

# Copy the rest of the source code
COPY cam2ip/ ./

# Build cam2ip with turbo JPEG support
RUN CGO_ENABLED=1 go build \
    -tags turbo \
    -o cam2ip \
    -trimpath \
    -ldflags "-s -w" \
    github.com/gen2brain/cam2ip/cmd/cam2ip


# Stage 2: Final runtime image
FROM python:3.12-alpine

# Install runtime dependencies
RUN apk add --no-cache \
    v4l-utils \
    libjpeg-turbo \
    ca-certificates

# Copy cam2ip binary from builder
COPY --from=cam2ip-builder /build/cam2ip /usr/local/bin/cam2ip

# Set up Python environment
WORKDIR /app

# Copy and install Python dependencies (separate layer for better caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY cam2ip_mcp_server.py .
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Expose cam2ip HTTP server + MCP HTTP server ports
EXPOSE 56000 3000

# Environment variables with defaults
ENV CAM2IP_ENABLED=true \
    CAM2IP_BASE_URL=http://127.0.0.1:56000 \
    CAM2IP_HTTP_TIMEOUT_S=5.0 \
    CAM2IP_BIND_ADDR=0.0.0.0:56000 \
    CAM2IP_INDEX=0 \
    MCP_MODE=stdio \
    MCP_HTTP_HOST=0.0.0.0 \
    MCP_HTTP_PORT=3000

ENTRYPOINT ["/entrypoint.sh"]
