import os
import sys
import base64
import asyncio
from contextlib import asynccontextmanager
from typing import Any

import httpx
import uvicorn
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp import types
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.responses import Response


class _AlreadySentResponse:
    """No-op ASGI response for endpoints that already called send()."""

    async def __call__(self, scope, receive, send):
        pass


CAM2IP_BASE_URL = os.environ.get("CAM2IP_BASE_URL", "http://127.0.0.1:56000").rstrip("/")
SNAPSHOT_URL = f"{CAM2IP_BASE_URL}/jpeg"

HTTP_TIMEOUT_S = float(os.environ.get("CAM2IP_HTTP_TIMEOUT_S", "5.0"))
MCP_MODE = os.environ.get("MCP_MODE", "stdio")  # "stdio", "sse", or "streamable-http"
MCP_HTTP_HOST = os.environ.get("MCP_HTTP_HOST", "0.0.0.0")
MCP_HTTP_PORT = int(os.environ.get("MCP_HTTP_PORT", "3000"))

server = Server("cam2ip-mcp")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="grab_frame",
            description="Fetch a single JPEG frame from cam2ip.",
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: Any) -> list[types.TextContent | types.ImageContent]:
    if name != "grab_frame":
        raise ValueError(f"Unknown tool: {name}")

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        resp = await client.get(SNAPSHOT_URL)
        resp.raise_for_status()

        content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip().lower()
        data = resp.content

    b64 = base64.b64encode(data).decode("ascii")

    return [
        types.ImageContent(
            type="image",
            data=b64,
            mimeType=content_type,
        )
    ]


# ---------------------------------------------------------------------------
# Transport helpers
# ---------------------------------------------------------------------------


def _run_uvicorn(app: Starlette) -> None:
    """Create and run a uvicorn server for the given ASGI app."""
    config = uvicorn.Config(
        app,
        host=MCP_HTTP_HOST,
        port=MCP_HTTP_PORT,
        log_level="info",
    )
    uv_server = uvicorn.Server(config)
    asyncio.get_event_loop().run_until_complete(uv_server.serve())


async def _serve_stdio() -> None:
    """MCP over standard input / output (for Claude Desktop, Cline, etc.)."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


async def _serve_sse() -> None:
    """MCP over HTTP with Server-Sent Events (legacy HTTP transport)."""
    sse = SseServerTransport("/messages")

    async def handle_sse(request):
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await server.run(
                streams[0], streams[1], server.create_initialization_options()
            )
        return _AlreadySentResponse()

    async def handle_messages(request):
        await sse.handle_post_message(
            request.scope, request.receive, request._send
        )
        return _AlreadySentResponse()

    app = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Route("/messages", endpoint=handle_messages, methods=["POST"]),
        ],
    )

    print(
        f"MCP SSE server listening on http://{MCP_HTTP_HOST}:{MCP_HTTP_PORT}/sse",
        file=sys.stderr,
    )
    config = uvicorn.Config(
        app, host=MCP_HTTP_HOST, port=MCP_HTTP_PORT, log_level="info"
    )
    await uvicorn.Server(config).serve()


async def _serve_streamable_http() -> None:
    """MCP over Streamable HTTP (current spec transport)."""
    session_manager = StreamableHTTPSessionManager(
        app=server,
        json_response=False,
        stateless=False,
    )

    @asynccontextmanager
    async def lifespan(app):
        async with session_manager.run():
            yield

    async def handle_mcp(scope, receive, send):
        await session_manager.handle_request(scope, receive, send)

    app = Starlette(
        lifespan=lifespan,
        routes=[
            Mount("/mcp", app=handle_mcp),
        ],
    )

    print(
        f"MCP Streamable HTTP server listening on http://{MCP_HTTP_HOST}:{MCP_HTTP_PORT}/mcp",
        file=sys.stderr,
    )
    config = uvicorn.Config(
        app, host=MCP_HTTP_HOST, port=MCP_HTTP_PORT, log_level="info"
    )
    await uvicorn.Server(config).serve()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

_TRANSPORTS = {
    "stdio": _serve_stdio,
    "sse": _serve_sse,
    "streamable-http": _serve_streamable_http,
}


async def main() -> None:
    mode = MCP_MODE.lower()
    serve_fn = _TRANSPORTS.get(mode)
    if serve_fn is None:
        valid = ", ".join(sorted(_TRANSPORTS))
        raise ValueError(f"Invalid MCP_MODE={mode!r}. Choose from: {valid}")
    await serve_fn()


if __name__ == "__main__":
    asyncio.run(main())
