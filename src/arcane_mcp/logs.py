"""Hand-written, bounded tail of Arcane container log downloads.

`/logs/download` stays in the OpenAPI hard-exclude list. Proxying that route
would pull a full attachment into the MCP client. This tool streams the same
download and returns only a capped tail.
"""

from __future__ import annotations

import codecs
import logging
from collections import deque
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

import httpx2
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

logger = logging.getLogger(__name__)

LOG_TOOL_NAME = "get_container_logs"
DEFAULT_TAIL = 200
MAX_TAIL = 2000
DEFAULT_MAX_BYTES = 131_072
HARD_MAX_BYTES = 524_288
LOG_TIMEOUT_SECONDS = 30.0
_CHUNK_SIZE = 8192

_DESCRIPTION = (
    "Return the tail of a container's Docker-retained logs. Read-only. "
    "Arcane's log download has no tail query, so this streams "
    "GET /environments/{id}/containers/{containerId}/logs/download and keeps only the most recent lines. "
    'id is the environment ("0" for local Docker, or an edge UUID). '
    "containerId is the container id or name. "
    f"tail defaults to {DEFAULT_TAIL} and is clamped to 1..{MAX_TAIL}. "
    f"max_bytes defaults to {DEFAULT_MAX_BYTES} and is hard-capped at {HARD_MAX_BYTES}. "
    "The result includes success, truncated, lines, bytes, and content, plus note when truncated. "
    "Requires Arcane permission containers:logs. Does not follow the log stream."
)

_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "success": {"type": "boolean"},
        "truncated": {"type": "boolean"},
        "lines": {"type": "integer"},
        "bytes": {"type": "integer"},
        "content": {"type": "string"},
        "note": {"type": "string"},
    },
    "required": ["success", "truncated", "lines", "bytes", "content"],
}


def _clamp_tail(tail: int) -> int:
    if tail < 1:
        return 1
    if tail > MAX_TAIL:
        return MAX_TAIL
    return tail


def _clamp_max_bytes(max_bytes: int) -> int:
    if max_bytes < 1:
        return 1
    if max_bytes > HARD_MAX_BYTES:
        return HARD_MAX_BYTES
    return max_bytes


def _utf8_suffix(text: str, max_bytes: int) -> str:
    """Return the trailing UTF-8 bytes of text, dropping a split code point."""
    if max_bytes < 1:
        return ""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    cut = raw[-max_bytes:]
    start = 0
    while start < len(cut) and 0x80 <= cut[start] <= 0xBF:
        start += 1
    return cut[start:].decode("utf-8")


class LogTailBuffer:
    """Ring of the most recent log lines, bounded by line count and UTF-8 bytes.

    Bytes past either limit are dropped as they arrive. The buffer never holds
    the whole download.
    """

    def __init__(self, *, tail: int, max_bytes: int) -> None:
        if tail < 1:
            raise ValueError("tail must be >= 1")
        if max_bytes < 1:
            raise ValueError("max_bytes must be >= 1")
        self._tail = tail
        self._max_bytes = max_bytes
        self._lines: deque[str] = deque()
        self._size = 0
        self._pending = ""
        self._truncated = False
        self._closed = False

    @property
    def byte_size(self) -> int:
        return self._size

    @property
    def line_count(self) -> int:
        return len(self._lines)

    def feed(self, text: str) -> None:
        if not text:
            return
        if self._pending:
            text = self._pending + text
            self._pending = ""
        start = 0
        length = len(text)
        while start < length:
            newline = text.find("\n", start)
            if newline < 0:
                break
            self._push(text[start : newline + 1])
            start = newline + 1
        if start >= length:
            return
        rest = text[start:]
        # An unfinished line still counts against the cap. Keep only its tail
        # so a multi-megabyte line without newlines cannot grow the buffer.
        if len(rest.encode("utf-8")) > self._max_bytes:
            rest = _utf8_suffix(rest, self._max_bytes)
            self._truncated = True
        self._pending = rest

    def finish(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._pending:
            pending = self._pending
            self._pending = ""
            self._push(pending)

    def as_dict(self) -> dict[str, Any]:
        self.finish()
        content = "".join(self._lines)
        size = len(content.encode("utf-8"))
        payload: dict[str, Any] = {
            "success": True,
            "truncated": self._truncated,
            "lines": len(self._lines),
            "bytes": size,
            "content": content,
        }
        if self._truncated:
            payload["note"] = (
                f"Truncated to the last {len(self._lines)} lines ({size} bytes). "
                "Older log lines were omitted. "
                f"tail is at most {MAX_TAIL}; max_bytes is at most {HARD_MAX_BYTES}."
            )
        return payload

    def _push(self, line: str) -> None:
        self._lines.append(line)
        self._size += len(line.encode("utf-8"))
        self._trim()

    def _trim(self) -> None:
        while self._lines and (len(self._lines) > self._tail or self._size > self._max_bytes):
            if len(self._lines) == 1 and self._size > self._max_bytes:
                trimmed = _utf8_suffix(self._lines[0], self._max_bytes)
                if trimmed != self._lines[0]:
                    self._truncated = True
                self._lines[0] = trimmed
                self._size = len(trimmed.encode("utf-8"))
                return
            dropped = self._lines.popleft()
            self._size -= len(dropped.encode("utf-8"))
            self._truncated = True


async def read_log_tail(chunks: AsyncIterator[bytes], *, tail: int, max_bytes: int) -> dict[str, Any]:
    """Decode a byte stream as UTF-8 (invalid bytes replaced) and keep the tail.

    The download is oldest-first and has no range/tail query, so the iterator
    is consumed through EOF. Each chunk is folded into the ring immediately;
    limits evict older lines instead of buffering the body for a final slice.
    """
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    buffer = LogTailBuffer(tail=tail, max_bytes=max_bytes)
    async for chunk in chunks:
        buffer.feed(decoder.decode(chunk))
    buffer.feed(decoder.decode(b"", final=True))
    return buffer.as_dict()


def _logs_path(environment_id: str, container_id: str) -> str:
    return (
        f"/environments/{quote(environment_id, safe='')}"
        f"/containers/{quote(container_id, safe='')}/logs/download"
    )


async def fetch_container_logs(
    client: httpx2.AsyncClient,
    *,
    environment_id: str,
    container_id: str,
    tail: int = DEFAULT_TAIL,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    environment_id = environment_id.strip()
    container_id = container_id.strip()
    if not environment_id or not container_id:
        raise ToolError("id and containerId are required")
    tail_n = _clamp_tail(tail)
    byte_cap = _clamp_max_bytes(max_bytes)
    try:
        async with client.stream(
            "GET",
            _logs_path(environment_id, container_id),
            headers={"Accept": "text/plain"},
            timeout=LOG_TIMEOUT_SECONDS,
        ) as response:
            if not response.is_success:
                detail = (await response.aread()).decode("utf-8", errors="replace")[:300]
                logger.warning(
                    "container log download failed env=%s container=%s status=%s",
                    environment_id,
                    container_id,
                    response.status_code,
                )
                raise ToolError(f"Arcane returned HTTP {response.status_code} for container logs: {detail}")
            # Chunk the transport reads so a single large socket buffer is folded
            # into the ring before the next piece is kept.
            result = await read_log_tail(
                response.aiter_bytes(chunk_size=_CHUNK_SIZE),
                tail=tail_n,
                max_bytes=byte_cap,
            )
    except ToolError:
        raise
    except httpx2.TimeoutException as exc:
        raise ToolError(f"Container log download timed out after {LOG_TIMEOUT_SECONDS:.0f}s") from exc
    except httpx2.HTTPError as exc:
        raise ToolError(f"Container log download failed: {exc}") from exc
    logger.info(
        "Fetched container logs env=%s container=%s lines=%d bytes=%d truncated=%s",
        environment_id,
        container_id,
        result["lines"],
        result["bytes"],
        result["truncated"],
    )
    return result


def register_logs_tool(mcp: FastMCP, client: httpx2.AsyncClient) -> None:
    @mcp.tool(
        name=LOG_TOOL_NAME,
        description=_DESCRIPTION,
        annotations={
            "title": "Get container logs",
            "read_only_hint": True,
            "destructive_hint": False,
            "open_world_hint": True,
        },
        output_schema=_OUTPUT_SCHEMA,
    )
    async def get_container_logs(
        id: str,
        containerId: str,
        tail: int = DEFAULT_TAIL,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> dict[str, Any]:
        return await fetch_container_logs(
            client,
            environment_id=id,
            container_id=containerId,
            tail=tail,
            max_bytes=max_bytes,
        )
