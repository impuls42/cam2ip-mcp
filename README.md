# cam2ip MCP Server Container

A containerized solution that runs both [cam2ip](https://github.com/gen2brain/cam2ip) and an MCP (Model Context Protocol) server for accessing camera frames.

## Features

- **cam2ip**: Streams camera feed via HTTP/MJPEG
- **MCP Server**: Provides programmatic access to camera frames via the Model Context Protocol
- **Three MCP transports**: stdio, SSE, and Streamable HTTP
- **Multi-arch support**: Built for both `linux/amd64` and `linux/arm64`
- **Automated builds**: GitHub Actions workflow for building and pushing to GitHub Container Registry

## Quick Start

### Using Docker Compose (Recommended)

The easiest way to run the container:

```bash
# Clone the repository
git clone --recursive https://github.com/impuls42/cam2ip-mcp.git
cd cam2ip-mcp

# (Optional) Copy and customize environment variables
cp .env.example .env
# Edit .env with your preferred settings

# Start the service
docker-compose up -d

# View logs
docker-compose logs -f

# Stop the service
docker-compose down
```

### Using Pre-built Image

Pull the latest image from GitHub Container Registry:

```bash
docker pull ghcr.io/impuls42/cam2ip-mcp:latest
```

Run the container with access to your webcam:

```bash
docker run --rm -it \
  --device=/dev/video0:/dev/video0 \
  -p 56000:56000 \
  ghcr.io/impuls42/cam2ip-mcp:latest
```

### Building Locally

```bash
# Clone with submodules
git clone --recursive https://github.com/impuls42/cam2ip-mcp.git
cd cam2ip-mcp

# Build the container
docker build -f Containerfile -t cam2ip-mcp .

# Or use BuildKit for better caching and parallel builds
DOCKER_BUILDKIT=1 docker build -f Containerfile -t cam2ip-mcp .

# Run the container
docker run --rm -it \
  --device=/dev/video0:/dev/video0 \
  -p 56000:56000 \
  cam2ip-mcp
```

### Build Optimizations

The Containerfile is optimized for:
- **Layer caching**: Dependencies are copied before source code
- **Multi-stage builds**: Only the final binary is copied to the runtime image
- **Minimal runtime dependencies**: Only essential libraries are included
- **TurboJPEG support**: Built with `-tags turbo` for hardware-accelerated JPEG encoding

For faster rebuilds during development, the build cache is preserved between builds.

## Configuration

### Environment Variables

- `CAM2IP_ENABLED`: Start the built-in cam2ip server — set to `false` when using an external instance (default: `true`)
- `CAM2IP_BASE_URL`: Base URL for cam2ip server (default: `http://127.0.0.1:56000`)
- `CAM2IP_HTTP_TIMEOUT_S`: HTTP timeout in seconds (default: `5.0`)
- `CAM2IP_BIND_ADDR`: Bind address for cam2ip (default: `0.0.0.0:56000`)
- `CAM2IP_INDEX`: Camera index to use - 0 for /dev/video0, 1 for /dev/video1, etc. (default: `0`)
- `CAM2IP_WIDTH`: Frame width in pixels (default: `640`)
- `CAM2IP_HEIGHT`: Frame height in pixels (default: `480`)
- `CAM2IP_QUALITY`: JPEG quality 1-100 (default: `75`)
- `CAM2IP_DELAY`: Delay between frames in milliseconds (default: `10`)
- `CAM2IP_ROTATE`: Rotate image - valid values: 90, 180, 270 (default: `0`)
- `CAM2IP_TIMESTAMP`: Draw timestamp on image (default: `false`)

See [.env.example](.env.example) for a complete list of configuration options.

### Using Docker Compose

1. Copy the example environment file:
   ```bash
   cp .env.example .env
   ```

2. Edit `.env` with your preferred settings

3. (Optional) Create `docker-compose.override.yml` for local customizations:
   ```bash
   cp docker-compose.override.yml.example docker-compose.override.yml
   ```

4. Start the service:
   ```bash
   docker-compose up -d
   ```

### Example with Custom Configuration (Docker CLI)

```bash
docker run --rm -it \
  --device=/dev/video0:/dev/video0 \
  -p 8080:8080 \
  -e CAM2IP_BIND_ADDR=0.0.0.0:8080 \
  -e CAM2IP_BASE_URL=http://127.0.0.1:8080 \
  -e CAM2IP_INDEX=0 \
  ghcr.io/impuls42/cam2ip-mcp:latest
```

## Accessing the Services

### cam2ip HTTP Interface

Once running, you can access:
- JPEG snapshot: `http://localhost:56000/jpeg`
- MJPEG stream: `http://localhost:56000/`

### MCP Transport Modes

The MCP server supports three transports via the `MCP_MODE` environment variable:

| Mode | Value | Endpoint | Use case |
|------|-------|----------|----------|
| stdio | `stdio` | stdin/stdout | Claude Desktop, Cline, local MCP clients |
| SSE | `sse` | `http://host:3000/sse` | Legacy HTTP-based MCP clients |
| Streamable HTTP | `streamable-http` | `http://host:3000/mcp` | Current MCP spec HTTP clients |

#### 1. stdio (Default)

For MCP clients that launch the server as a subprocess (Claude Desktop, Cline, etc.):

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

#### 2. SSE (Server-Sent Events)

Legacy HTTP transport. Useful for clients that don't yet support Streamable HTTP:

```bash
docker run --rm -it \
  --device=/dev/video0:/dev/video0 \
  -p 56000:56000 -p 3000:3000 \
  -e MCP_MODE=sse \
  ghcr.io/impuls42/cam2ip-mcp:latest
```

MCP clients connect to: `http://localhost:3000/sse`

#### 3. Streamable HTTP (Current Spec)

The recommended HTTP transport per the current MCP specification:

```bash
docker run --rm -it \
  --device=/dev/video0:/dev/video0 \
  -p 56000:56000 -p 3000:3000 \
  -e MCP_MODE=streamable-http \
  ghcr.io/impuls42/cam2ip-mcp:latest
```

MCP clients connect to: `http://localhost:3000/mcp`

Or with Docker Compose, set in your `.env`:
```bash
MCP_MODE=streamable-http
```

All three modes expose the same `grab_frame` tool which fetches a JPEG frame from the camera and returns it as a base64-encoded image.

## Building from Source

### Prerequisites

- Docker or Podman
- Git (with submodule support)

### Build Steps

```bash
# Clone with submodules
git clone --recursive https://github.com/impuls42/cam2ip-mcp.git
cd cam2ip-mcp

# Build
docker build -f Containerfile -t cam2ip-mcp .
```

## Development

### Running Tests

```bash
# Test cam2ip endpoint
curl http://localhost:56000/jpeg -o test.jpg

# Verify the image
file test.jpg
```

### Logs

View container logs:

```bash
docker logs <container_id>
```

## Architecture

The container uses an optimized multi-stage build:

### Build Process

1. **Stage 1 - cam2ip builder**: 
   - Uses `golang:1.23-alpine` base image
   - Installs build dependencies (v4l-utils-dev, libjpeg-turbo-dev)
   - Leverages Go module caching by copying `go.mod`/`go.sum` first
   - Builds cam2ip with `CGO_ENABLED=1` and `-tags turbo` for TurboJPEG support
   - Creates optimized binary with stripped symbols

2. **Stage 2 - Final runtime**:
   - Uses `python:3.12-alpine` base image
   - Copies only the cam2ip binary (no build dependencies)
   - Installs Python dependencies in a separate layer for better caching
   - Total image size is significantly reduced

### Runtime Components

1. **cam2ip** (Go binary): Captures frames from the video device and serves them over HTTP
2. **MCP Server** (Python): Provides MCP protocol access to the camera frames

Both services are managed by the `entrypoint.sh` script which:
- Starts cam2ip in the background
- Starts the MCP server in the foreground
- Handles graceful shutdown of both services

## License

This project combines:
- [cam2ip](https://github.com/gen2brain/cam2ip) - See cam2ip/COPYING
- MCP Server implementation - See LICENSE

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## Troubleshooting

### Camera not accessible

Ensure the container has access to the video device:
```bash
# Check device permissions
ls -l /dev/video0

# Add user to video group if needed
sudo usermod -a -G video $USER
```

### Port already in use

Change the port mapping:
```bash
docker run --rm -it \
  --device=/dev/video0:/dev/video0 \
  -p 8080:56000 \
  ghcr.io/impuls42/cam2ip-mcp:latest
```
