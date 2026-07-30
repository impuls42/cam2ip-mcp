"""Unit tests for the multipart/x-mixed-replace parser."""

from __future__ import annotations

from typing import AsyncIterator

import pytest

from cam2mcp_server import FALLBACK_BOUNDARY, iter_mjpeg_parts, parse_boundary

BOUNDARY = b"--boundary"


async def chunks_of(data: bytes, size: int) -> AsyncIterator[bytes]:
    for start in range(0, len(data), size):
        yield data[start : start + size]


def mjpeg_stream(*bodies: bytes, content_type: bytes = b"image/jpeg") -> bytes:
    """Serialise parts the way Go's mime/multipart writer does."""
    out = b""
    for index, body in enumerate(bodies):
        if index:
            out += b"\r\n"
        out += b"--" + BOUNDARY + b"\r\nContent-Type: " + content_type + b"\r\n\r\n" + body
    return out + b"\r\n--" + BOUNDARY + b"--\r\n"


async def collect(data: bytes, chunk_size: int = 4096, boundary: bytes = BOUNDARY):
    return [part async for part in iter_mjpeg_parts(chunks_of(data, chunk_size), boundary)]


class TestParseBoundary:
    def test_reads_boundary_param(self):
        assert parse_boundary("multipart/x-mixed-replace;boundary=--boundary") == b"--boundary"

    def test_tolerates_spaces_and_quotes(self):
        assert parse_boundary('multipart/x-mixed-replace; boundary="xyz"') == b"xyz"

    def test_falls_back_when_absent(self):
        assert parse_boundary("multipart/x-mixed-replace") == FALLBACK_BOUNDARY


class TestIterParts:
    async def test_yields_each_body(self):
        parts = await collect(mjpeg_stream(b"one", b"two", b"three"))
        assert [body for _, body in parts] == [b"one", b"two", b"three"]

    async def test_reports_part_content_type(self):
        parts = await collect(mjpeg_stream(b"x", content_type=b"image/png"))
        assert parts[0][0] == "image/png"

    async def test_defaults_content_type_when_part_has_no_header(self):
        stream = b"--" + BOUNDARY + b"\r\n\r\nbody\r\n--" + BOUNDARY + b"--\r\n"
        parts = await collect(stream)
        assert parts == [("image/jpeg", b"body")]

    @pytest.mark.parametrize("chunk_size", [1, 2, 3, 7, 64, 4096])
    async def test_survives_arbitrary_chunk_boundaries(self, chunk_size):
        """A frame split across TCP reads must still come back whole."""
        parts = await collect(mjpeg_stream(b"alpha", b"beta"), chunk_size=chunk_size)
        assert [body for _, body in parts] == [b"alpha", b"beta"]

    async def test_body_containing_the_boundary_bytes_is_not_split(self):
        """Requiring the leading CRLF is what protects binary payloads."""
        body = b"\xff\xd8--" + BOUNDARY + b"trailing\xff\xd9"
        parts = await collect(mjpeg_stream(body, b"next"))
        assert [b for _, b in parts] == [body, b"next"]

    async def test_ignores_preamble_before_first_delimiter(self):
        parts = await collect(b"junk preamble\r\n" + mjpeg_stream(b"body"))
        assert [b for _, b in parts] == [b"body"]

    async def test_incomplete_trailing_part_is_not_yielded(self):
        """A half-received frame must be withheld, not handed over truncated."""
        stream = mjpeg_stream(b"complete") + b"--" + BOUNDARY + b"\r\n\r\nhalf"
        parts = await collect(stream)
        assert [b for _, b in parts] == [b"complete"]

    async def test_rejects_a_response_that_is_not_multipart(self):
        async def flood() -> AsyncIterator[bytes]:
            for _ in range(40):
                yield b"x" * (1024 * 1024)

        with pytest.raises(ValueError, match="no multipart delimiter"):
            async for _ in iter_mjpeg_parts(flood(), BOUNDARY):
                pass
