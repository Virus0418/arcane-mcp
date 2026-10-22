"""Hand-written get_container_logs: stream a download, return a capped tail."""

from __future__ import annotations

import httpx2
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from arcane_mcp.config import load_settings
from arcane_mcp.logs import (
    DEFAULT_MAX_BYTES,
    DEFAULT_TAIL,
    HARD_MAX_BYTES,
    LOG_TIMEOUT_SECONDS,
    LOG_TOOL_NAME,
    MAX_TAIL,
    LogTailBuffer,
    read_log_tail,
)
from arcane_mcp.safety import CONFIRM_TOOL_NAME, destructive_tool_names
from arcane_mcp.server import build_server
from arcane_mcp.spec import HARD_EXCLUDE
from tests.test_server import ENV, GENERATED_TOOL_COUNT, mock_client

DOWNLOAD = "/environments/{id}/containers/{containerId}/logs/download"


def _chunks(raw: bytes, size: int = 100) -> list[bytes]:
    return [raw[i : i + size] for i in range(0, len(raw), size)] or [b""]


def logs_client(
    body: bytes | list[bytes],
    seen: dict | None = None,
    *,
    status: int = 200,
    error: str = "nope",
) -> httpx2.AsyncClient:
    chunks = [body] if isinstance(body, bytes) else list(body)
    captured = seen if seen is not None else {}

    class _Stream(httpx2.AsyncByteStream):
        async def __aiter__(self):
            for chunk in chunks:
                captured["read"] = captured.get("read", 0) + len(chunk)
                yield chunk

    class _Transport(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            captured["method"] = request.method
            captured["path"] = request.url.raw_path.decode()
            captured["api_key"] = request.headers.get("x-api-key")
            captured["accept"] = request.headers.get("accept")
            captured["timeout"] = request.extensions.get("timeout")
            if status != 200:
                return httpx2.Response(status, json={"success": False, "error": error})
            return httpx2.Response(200, stream=_Stream())

    return httpx2.AsyncClient(
        base_url="http://arcane.test/api",
        headers={"X-API-Key": "test-key", "Accept": "application/json"},
        transport=_Transport(),
    )


async def _call(spec, body, arguments: dict | None = None, **client_kw):
    seen: dict = {}
    settings = load_settings(ENV)
    mcp = build_server(settings, spec, logs_client(body, seen, **client_kw))
    args = {"id": "0", "containerId": "web", **(arguments or {})}
    async with Client(mcp) as client:
        result = await client.call_tool(LOG_TOOL_NAME, args)
    return result.structured_content, seen


def test_hard_exclude_still_matches_log_download():
    assert HARD_EXCLUDE.pattern == r"(/stream|/logs/download|/export|/download|/attestations|/upload|/app-images/)"
    assert HARD_EXCLUDE.search(DOWNLOAD)


def test_ring_stays_within_caps_and_keeps_the_end():
    lines = [f"{i:04d}-{'y' * 40}" for i in range(500)]
    text = "\n".join(lines) + "\n"
    buffer = LogTailBuffer(tail=5, max_bytes=100)
    for offset in range(0, len(text), 7):
        buffer.feed(text[offset : offset + 7])
        assert buffer.line_count <= 5
        assert buffer.byte_size <= 100
    result = buffer.as_dict()
    assert result["bytes"] == buffer.byte_size
    assert result["bytes"] <= 100
    assert result["lines"] <= 5
    assert result["truncated"] is True
    assert "note" in result
    assert result["content"].endswith(lines[-1] + "\n")
    assert lines[0] not in result["content"]
    assert result["success"] is True


def test_single_overlong_line_keeps_a_utf8_suffix():
    buffer = LogTailBuffer(tail=3, max_bytes=4)
    buffer.feed("你好世界")  # 3 bytes per character
    result = buffer.as_dict()
    assert result["truncated"] is True
    assert result["bytes"] <= 4
    assert result["content"] == "界"
    assert result["lines"] == 1


async def test_read_log_tail_joins_chunks_and_replaces_invalid_utf8():
    async def chunks():
        yield b"hel"
        yield b"lo\n\xe2"
        yield b"\x98\x83\n\xff"
        yield b"end"

    result = await read_log_tail(chunks(), tail=10, max_bytes=1000)
    assert result["content"] == "hello\n☃\n\ufffdend"
    assert result["lines"] == 3
    assert result["truncated"] is False
    assert "note" not in result
    assert result["bytes"] == len(result["content"].encode("utf-8"))


async def test_large_stream_returns_only_last_lines_within_byte_cap(spec):
    lines = [f"log-line-{i:05d}" for i in range(3000)]
    raw = ("\n".join(lines) + "\n").encode()
    # 15 bytes per line. tail allows 10 lines (150 bytes) but the byte cap keeps 6 (90).
    result, seen = await _call(
        spec,
        _chunks(raw, 64),
        {"tail": 10, "max_bytes": 100},
    )
    expected = "\n".join(lines[-6:]) + "\n"
    assert result["success"] is True
    assert result["truncated"] is True
    assert result["content"] == expected
    assert result["lines"] == 6
    assert result["lines"] <= 10
    assert result["bytes"] == len(expected.encode())
    assert result["bytes"] <= 100
    assert lines[0] not in result["content"]
    assert result["content"].endswith(lines[-1] + "\n")
    assert "omitted" in result["note"]
    # Oldest-first download: the real tail is only known after the body is scanned.
    assert seen["read"] == len(raw)
    assert seen["method"] == "GET"
    assert seen["path"] == "/api/environments/0/containers/web/logs/download"
    assert seen["api_key"] == "test-key"
    assert seen["accept"] == "text/plain"
    assert seen["timeout"]["read"] == LOG_TIMEOUT_SECONDS
    assert seen["timeout"]["connect"] == LOG_TIMEOUT_SECONDS


async def test_tail_limit_without_hitting_the_byte_cap(spec):
    lines = [f"row-{i}" for i in range(50)]
    raw = ("\n".join(lines) + "\n").encode()
    result, _seen = await _call(spec, _chunks(raw), {"tail": 3, "max_bytes": 10_000})
    expected = "\n".join(lines[-3:]) + "\n"
    assert result["content"] == expected
    assert result["lines"] == 3
    assert result["truncated"] is True
    assert result["bytes"] == len(expected)


async def test_byte_cap_keeps_the_end_when_tail_is_large(spec):
    lines = [f"{i:04d}{'B' * 50}" for i in range(80)]
    raw = ("\n".join(lines) + "\n").encode()
    result, _seen = await _call(spec, raw, {"tail": 50, "max_bytes": 120})
    expected = "\n".join(lines[-2:]) + "\n"
    assert len(expected.encode()) == 110
    assert result["content"] == expected
    assert result["lines"] == 2
    assert result["bytes"] <= 120
    assert result["truncated"] is True
    assert lines[0] not in result["content"]


async def test_small_log_is_not_truncated(spec):
    raw = b"alpha\nbeta\n"
    result, _seen = await _call(spec, raw)
    assert result == {
        "success": True,
        "truncated": False,
        "lines": 2,
        "bytes": len(raw),
        "content": "alpha\nbeta\n",
    }


async def test_partial_last_line_and_empty_body(spec):
    partial, _seen = await _call(spec, b"a\nb\nc", {"tail": 2, "max_bytes": 100})
    assert partial["content"] == "b\nc"
    assert partial["lines"] == 2
    assert partial["truncated"] is True

    empty, _seen = await _call(spec, b"")
    assert empty["content"] == ""
    assert empty["lines"] == 0
    assert empty["bytes"] == 0
    assert empty["truncated"] is False
    assert "note" not in empty


async def test_requested_limits_are_clamped(spec):
    short = [f"s-{i}" for i in range(10)]
    raw = ("\n".join(short) + "\n").encode()
    under, _seen = await _call(spec, raw, {"tail": 9000, "max_bytes": 10**9})
    assert under["truncated"] is False
    assert under["lines"] == 10
    assert under["content"] == "\n".join(short) + "\n"

    wide = [f"{i:04d}{'X' * 300}" for i in range(2000)]
    wide_raw = ("\n".join(wide) + "\n").encode()
    assert len(wide_raw) > HARD_MAX_BYTES
    capped, _seen = await _call(spec, _chunks(wide_raw, 4096), {"tail": 9000, "max_bytes": 10**9})
    assert capped["truncated"] is True
    assert capped["lines"] <= MAX_TAIL
    assert capped["bytes"] <= HARD_MAX_BYTES
    assert capped["content"].endswith(wide[-1] + "\n")
    assert wide[0] not in capped["content"]

    one, _seen = await _call(spec, b"first\nsecond\n", {"tail": 0, "max_bytes": 0})
    assert one["lines"] == 1
    assert one["content"] == "\n"
    assert one["bytes"] == 1
    assert one["truncated"] is True


async def test_tool_schema_defaults_and_read_only_hint(spec):
    mcp = build_server(load_settings(ENV), spec, mock_client([]))
    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    tool = tools[LOG_TOOL_NAME]
    properties = tool.input_schema["properties"]
    assert properties["tail"]["default"] == DEFAULT_TAIL
    assert properties["max_bytes"]["default"] == DEFAULT_MAX_BYTES
    assert "id" in properties
    assert "containerId" in properties
    hints = tool.annotations.model_dump(by_alias=True)
    assert hints["readOnlyHint"] is True
    assert hints["destructiveHint"] is False


async def test_logs_are_not_gated_by_confirmation(spec):
    assert LOG_TOOL_NAME not in destructive_tool_names(spec)
    result, seen = await _call(spec, b"line\n")
    assert result["content"] == "line\n"
    assert "requires_confirmation" not in result
    assert seen["method"] == "GET"


async def test_tool_not_registered_when_logs_disabled(spec):
    settings = load_settings({**ENV, "ARCANE_MCP_ENABLE_LOGS": "false"})
    mcp = build_server(settings, spec, mock_client([]))
    async with Client(mcp) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert LOG_TOOL_NAME not in names
    assert "download_container_logs" not in names
    assert CONFIRM_TOOL_NAME in names
    assert len(names) == GENERATED_TOOL_COUNT + 1


async def test_read_only_mode_keeps_hand_written_logs(spec):
    settings = load_settings({**ENV, "ARCANE_MCP_READ_ONLY": "true"})
    mcp = build_server(settings, spec, mock_client([]))
    async with Client(mcp) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert LOG_TOOL_NAME in names
    assert "download_container_logs" not in names
    assert "list_containers" in names
    assert "start_container" not in names


async def test_blank_ids_do_not_call_arcane(spec):
    seen: dict = {}
    mcp = build_server(load_settings(ENV), spec, logs_client(b"nope\n", seen))
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="required"):
            await client.call_tool(LOG_TOOL_NAME, {"id": "  ", "containerId": "web"})
    assert "method" not in seen


async def test_arcane_http_error_and_timeout(spec):
    mcp = build_server(load_settings(ENV), spec, logs_client(b"", status=404, error="missing"))
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="404"):
            await client.call_tool(LOG_TOOL_NAME, {"id": "0", "containerId": "gone"})

    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow")

    stalled = httpx2.AsyncClient(
        base_url="http://arcane.test/api",
        headers={"X-API-Key": "test-key"},
        transport=httpx2.MockTransport(handler),
    )
    mcp = build_server(load_settings(ENV), spec, stalled)
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="timed out"):
            await client.call_tool(LOG_TOOL_NAME, {"id": "0", "containerId": "web"})


async def test_container_id_is_a_single_path_segment(spec):
    _result, seen = await _call(spec, b"ok\n", {"id": "0", "containerId": "foo/bar"})
    assert seen["path"] == "/api/environments/0/containers/foo%2Fbar/logs/download"
