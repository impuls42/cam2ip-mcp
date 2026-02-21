import os
import base64
import asyncio
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types


CAM2IP_BASE_URL = os.environ.get("CAM2IP_BASE_URL", "http://127.0.0.1:56000").rstrip("/")
SNAPSHOT_URL = f"{CAM2IP_BASE_URL}/jpeg"

# You can tune these if needed
HTTP_TIMEOUT_S = float(os.environ.get("CAM2IP_HTTP_TIMEOUT_S", "5.0"))

server = Server("cam2ip-mcp")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="grab_frame",
            description=f"Fetch a single JPEG frame.",
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

    # Return an "image" content item with base64-encoded data
    return [
        types.ImageContent(
            type="image",
            data=b64,
            mimeType=content_type,
        )
    ]


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
