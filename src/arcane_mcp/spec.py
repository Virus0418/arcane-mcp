"""Fetch, normalise, filter and name Arcane's OpenAPI operations."""

from __future__ import annotations

import asyncio
import copy
import logging
import re
import time
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import httpx2
from fastmcp.server.providers.openapi import MCPType
from fastmcp.utilities.openapi.models import HTTPRoute

from arcane_mcp.config import Settings

logger = logging.getLogger(__name__)

SYNTHETIC_UNTAGGED_TAG = "Jobs"
HARD_EXCLUDE = re.compile(r"(/stream|/logs/download|/export|/download|/attestations|/upload|/app-images/)")
# Success responses with these media types are streams FastMCP would otherwise
# expose as ordinary tools. Path substrings such as /stream miss
# /agents/{id}/system/upgrade/progress (text/event-stream, tag System).
STREAMING_MEDIA_TYPES = frozenset(
    {
        "text/event-stream",
        "application/x-ndjson",
        "application/stream+json",
        "application/json-seq",
        "application/x-json-stream",
    }
)
_HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
_MAX_BACKOFF = 10.0


class SpecFetchError(RuntimeError):
    """Raised when the OpenAPI spec cannot be fetched within the timeout."""


async def fetch_spec(
    client: httpx2.AsyncClient,
    *,
    timeout: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """GET /openapi.json (relative to the client's base_url), retrying with backoff until timeout."""
    deadline = clock() + timeout
    delay = 1.0
    attempt = 0
    while True:
        attempt += 1
        try:
            response = await client.get("/openapi.json")
            response.raise_for_status()
            spec = response.json()
            if not isinstance(spec, dict) or "paths" not in spec:
                raise SpecFetchError("Response is not an OpenAPI document")
            logger.info("Fetched OpenAPI spec (attempt %d, %d paths)", attempt, len(spec["paths"]))
            return spec
        except SpecFetchError:
            raise
        except Exception as exc:  # noqa: BLE001 - any transport/HTTP/JSON error is retryable
            last_error = f"{type(exc).__name__}: {exc}"
            if clock() >= deadline:
                raise SpecFetchError(f"Could not fetch OpenAPI spec after {attempt} attempts: {last_error}") from exc
            logger.warning("Spec fetch attempt %d failed (%s); retrying in %.0fs", attempt, last_error, delay)
            await sleep(delay)
            delay = min(delay * 2, _MAX_BACKOFF)


def normalise_spec(spec: dict[str, Any], base_url: str) -> dict[str, Any]:
    """Return a copy with servers pointed at base_url/api and untagged operations tagged."""
    out = copy.deepcopy(spec)
    out["servers"] = [{"url": f"{base_url}/api"}]
    for path_item in out.get("paths", {}).values():
        for method, operation in path_item.items():
            if method.lower() in _HTTP_METHODS and isinstance(operation, dict) and not operation.get("tags"):
                operation["tags"] = [SYNTHETIC_UNTAGGED_TAG]
    return out


def tool_name(operation_id: str) -> str:
    """Convert kebab-case or camelCase operationIds to snake_case (list-containers, listEnvironments -> list_...)."""
    with_breaks = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", operation_id)
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9_]", "_", with_breaks.lower())).strip("_")


def is_streaming_media_type(media: str) -> bool:
    """True for SSE and other streaming response types, ignoring parameters."""
    base = media.split(";", 1)[0].strip().lower()
    return base in STREAMING_MEDIA_TYPES


def _resolve_local_ref(spec: dict[str, Any], node: Any, seen: frozenset[str] = frozenset()) -> Any:
    if not isinstance(node, dict) or "$ref" not in node:
        return node
    ref = node["$ref"]
    if not isinstance(ref, str) or not ref.startswith("#/") or ref in seen:
        return node
    current: Any = spec
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or part not in current:
            return node
        current = current[part]
    return _resolve_local_ref(spec, current, seen | {ref})


def _success_payloads(responses: dict[str, Any]) -> list[Any]:
    success: list[Any] = []
    default = None
    for status, payload in responses.items():
        code = str(status)
        if code == "default":
            default = payload
        elif code.startswith("2"):
            success.append(payload)
    if success:
        return success
    if default is not None:
        return [default]
    return []


def _operation_has_streaming_response(spec: dict[str, Any], operation: dict[str, Any]) -> bool:
    responses = operation.get("responses") or {}
    if not isinstance(responses, dict):
        return False
    for payload in _success_payloads(responses):
        resolved = _resolve_local_ref(spec, payload)
        if not isinstance(resolved, dict):
            continue
        content = resolved.get("content") or {}
        if isinstance(content, dict) and any(is_streaming_media_type(media) for media in content):
            return True
    return False


def streaming_route_keys(spec: dict[str, Any] | None) -> frozenset[tuple[str, str]]:
    """(METHOD, path) pairs whose success response declares an unsupported stream."""
    if not spec:
        return frozenset()
    found: set[tuple[str, str]] = set()
    for path, path_item in spec.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            if _operation_has_streaming_response(spec, operation):
                found.add((method.upper(), path))
    return frozenset(found)


def _content_media_types(response: Any) -> list[str]:
    schema = getattr(response, "content_schema", None)
    if schema is None and isinstance(response, dict):
        raw = response.get("content_schema")
        if raw is None:
            raw = response.get("content")
        schema = raw
    if isinstance(schema, dict):
        return [str(media) for media in schema]
    return []


def _route_declares_stream(route: Any) -> bool:
    """Inspect the parsed route, which keeps every media type on the success response."""
    responses = getattr(route, "responses", None) or {}
    if not isinstance(responses, dict):
        return False
    for response in responses.values():
        if any(is_streaming_media_type(media) for media in _content_media_types(response)):
            return True
    return False


def build_route_map_fn(
    settings: Settings,
    spec: dict[str, Any] | None = None,
) -> Callable[[HTTPRoute, MCPType], MCPType]:
    allow = set(settings.tags)
    deny = set(settings.exclude_tags)
    read_only = settings.read_only
    streaming = streaming_route_keys(spec)

    def route_map(route: HTTPRoute, _default: MCPType) -> MCPType:
        tags = set(route.tags or [])
        if (
            HARD_EXCLUDE.search(route.path)
            or (route.method.upper(), route.path) in streaming
            or _route_declares_stream(route)
        ):
            return MCPType.EXCLUDE
        if tags & deny:
            return MCPType.EXCLUDE
        if read_only and route.method.upper() != "GET":
            return MCPType.EXCLUDE
        if tags & allow:
            return MCPType.TOOL
        return MCPType.EXCLUDE

    return route_map


def exposed_tool_names(spec: dict[str, Any], settings: Settings) -> frozenset[str]:
    """Tool names ``build_route_map_fn`` would actually register from this spec.

    The confirmation gate uses this so a read-only server does not offer a token
    for a mutation the route map already dropped. Every default-exposed Arcane
    operation has an operationId; the component hook names those with ``tool_name``.
    """
    route_map = build_route_map_fn(settings, spec)
    names: set[str] = set()
    for path, path_item in spec.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            operation_id = operation.get("operationId")
            if not operation_id:
                continue
            route = SimpleNamespace(
                method=method.upper(),
                path=path,
                tags=operation.get("tags") or [],
                operation_id=operation_id,
                responses={},
            )
            if route_map(route, MCPType.TOOL) is MCPType.TOOL:
                names.add(tool_name(operation_id))
    return frozenset(names)


def build_component_fn() -> Callable[[HTTPRoute, Any], None]:
    def component(route: HTTPRoute, comp: Any) -> None:
        if route.operation_id:
            comp.name = tool_name(route.operation_id)
        summary = (route.summary or "").strip().rstrip(".")
        description = (route.description or "").strip()
        if summary and description:
            comp.description = f"{summary}. {description}"
        elif summary or description:
            comp.description = summary or description

    return component
