import copy
from types import SimpleNamespace

import httpx2
import pytest
from fastmcp.server.providers.openapi import MCPType

from arcane_mcp.config import load_settings
from arcane_mcp.spec import (
    HARD_EXCLUDE,
    SYNTHETIC_UNTAGGED_TAG,
    SpecFetchError,
    build_component_fn,
    build_route_map_fn,
    fetch_spec,
    normalise_spec,
    streaming_route_keys,
    tool_name,
)

BASE_ENV = {"ARCANE_BASE_URL": "http://arcane.test", "ARCANE_API_KEY": "k", "MCP_AUTH_TOKEN": "t"}


def route(method: str, path: str, tags: list[str] | None, operation_id: str = "x-y"):
    return SimpleNamespace(method=method, path=path, tags=tags, operation_id=operation_id,
                           summary="Sum", description="Desc")


@pytest.mark.parametrize("op,expected", [
    ("list-containers", "list_containers"),
    ("get-image-by-id", "get_image_by_id"),
    ("already_snake", "already_snake"),
    ("Mixed-Case", "mixed_case"),
    ("listEnvironments", "list_environments"),
    ("getVersion", "get_version"),
    ("getImageByID", "get_image_by_id"),
])
def test_tool_name(op, expected):
    assert tool_name(op) == expected


def test_normalise_rewrites_server_and_tags_untagged(spec):
    original = copy.deepcopy(spec)
    out = normalise_spec(spec, "http://arcane.test")
    assert out["servers"] == [{"url": "http://arcane.test/api"}]
    assert spec == original, "input must not be mutated"
    jobs = out["paths"]["/environments/{id}/jobs/{jobId}/restart"]["post"]
    assert jobs["tags"] == [SYNTHETIC_UNTAGGED_TAG]
    containers = out["paths"]["/environments/{id}/containers"]["get"]
    assert containers["tags"] == ["Containers"]


def test_route_map_default_allowlist():
    fn = build_route_map_fn(load_settings(BASE_ENV))
    assert fn(route("GET", "/environments/{id}/containers", ["Containers"]), MCPType.TOOL) is MCPType.TOOL
    assert fn(route("POST", "/environments/{id}/containers/{containerId}/start", ["Containers"]), MCPType.TOOL) is MCPType.TOOL
    assert fn(route("GET", "/users", ["Users"]), MCPType.TOOL) is MCPType.EXCLUDE
    assert fn(route("GET", "/environments/{id}/swarm/info", ["Swarm"]), MCPType.TOOL) is MCPType.EXCLUDE
    assert fn(route("GET", "/x", None), MCPType.TOOL) is MCPType.EXCLUDE


def test_route_map_hard_exclusions_beat_allowlist():
    fn = build_route_map_fn(load_settings(BASE_ENV))
    for path in [
        "/environments/{id}/containers/{containerId}/logs/download",
        "/environments/{id}/images/{name}/export",
        "/environments/{id}/images/{name}/attestations",
        "/environments/{id}/images/upload",
        "/environments/{id}/stream",
    ]:
        assert HARD_EXCLUDE.search(path)
        assert fn(route("GET", path, ["Containers", "Images"]), MCPType.TOOL) is MCPType.EXCLUDE


def test_route_map_exclude_tags_beats_allowlist():
    fn = build_route_map_fn(load_settings({**BASE_ENV, "ARCANE_MCP_EXCLUDE_TAGS": "Images"}))
    assert fn(route("POST", "/environments/{id}/containers/{containerId}/commit", ["Containers", "Images"]), MCPType.TOOL) is MCPType.EXCLUDE


def test_route_map_read_only():
    fn = build_route_map_fn(load_settings({**BASE_ENV, "ARCANE_MCP_READ_ONLY": "true"}))
    assert fn(route("GET", "/environments/{id}/containers", ["Containers"]), MCPType.TOOL) is MCPType.TOOL
    assert fn(route("POST", "/environments/{id}/containers/{containerId}/start", ["Containers"]), MCPType.TOOL) is MCPType.EXCLUDE
    assert fn(route("DELETE", "/environments/{id}/containers/{containerId}", ["Containers"]), MCPType.TOOL) is MCPType.EXCLUDE


def test_route_map_custom_tags():
    fn = build_route_map_fn(load_settings({**BASE_ENV, "ARCANE_MCP_TAGS": "Swarm"}))
    assert fn(route("GET", "/environments/{id}/swarm/info", ["Swarm"]), MCPType.TOOL) is MCPType.TOOL
    assert fn(route("GET", "/environments/{id}/containers", ["Containers"]), MCPType.TOOL) is MCPType.EXCLUDE


def test_component_fn_renames_and_describes():
    comp = SimpleNamespace(name="list-containers", description=None)
    build_component_fn()(route("GET", "/c", ["Containers"], "list-containers"), comp)
    assert comp.name == "list_containers"
    assert comp.description == "Sum. Desc"


def test_component_fn_summary_only():
    comp = SimpleNamespace(name="x", description=None)
    r = route("GET", "/c", ["Containers"], "get-thing")
    r.description = None
    build_component_fn()(r, comp)
    assert comp.description == "Sum"


async def test_fetch_spec_success(make_arcane_client):
    client = make_arcane_client(lambda req: httpx2.Response(200, json={"openapi": "3.1.0", "paths": {}}))
    out = await fetch_spec(client, timeout=5)
    assert out["openapi"] == "3.1.0"


async def test_fetch_spec_retries_then_succeeds(make_arcane_client):
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx2.Response(503)
        return httpx2.Response(200, json={"openapi": "3.1.0", "paths": {}})

    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    out = await fetch_spec(make_arcane_client(handler), timeout=60, sleep=fake_sleep)
    assert out["paths"] == {}
    assert calls["n"] == 3
    assert slept == [1.0, 2.0]


async def test_fetch_spec_gives_up_after_timeout(make_arcane_client):
    def handler(req):
        raise httpx2.ConnectError("refused")

    now = {"t": 0.0}

    async def fake_sleep(s):
        now["t"] += s

    with pytest.raises(SpecFetchError) as exc:
        await fetch_spec(make_arcane_client(handler), timeout=5, sleep=fake_sleep, clock=lambda: now["t"])
    assert "refused" in str(exc.value)


async def test_fetch_spec_requests_openapi_path(make_arcane_client):
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx2.Response(200, json={"openapi": "3.1.0", "paths": {}})

    await fetch_spec(make_arcane_client(handler), timeout=5)
    assert seen == ["http://arcane.test/api/openapi.json"]


def test_route_map_excludes_event_stream_by_media_type():
    """SSE is excluded from the response media type, including paths that do not contain /stream."""
    fn = build_route_map_fn(load_settings(BASE_ENV))
    progress = "/agents/{id}/system/upgrade/progress"
    assert HARD_EXCLUDE.search(progress) is None
    streamed = SimpleNamespace(
        method="GET",
        path=progress,
        tags=["System"],
        operation_id="stream-upgrade-progress",
        responses={
            "200": SimpleNamespace(
                content_schema={"text/event-stream": {}, "application/problem+json": {}}
            )
        },
    )
    charset = SimpleNamespace(
        method="GET",
        path="/agents/{id}/events/live",
        tags=["Events"],
        operation_id="live-events",
        responses={"200": SimpleNamespace(content_schema={"text/event-stream; charset=utf-8": {}})},
    )
    ndjson = SimpleNamespace(
        method="GET",
        path="/agents/{id}/events",
        tags=["Events"],
        operation_id="events-ndjson",
        responses={"200": SimpleNamespace(content_schema={"application/x-ndjson": {}})},
    )
    json_route = SimpleNamespace(
        method="GET",
        path="/agents/{id}/containers",
        tags=["Containers"],
        operation_id="list-containers",
        responses={"200": SimpleNamespace(content_schema={"application/json": {}, "application/problem+json": {}})},
    )
    assert fn(streamed, MCPType.TOOL) is MCPType.EXCLUDE
    assert fn(charset, MCPType.TOOL) is MCPType.EXCLUDE
    assert fn(ndjson, MCPType.TOOL) is MCPType.EXCLUDE
    assert fn(json_route, MCPType.TOOL) is MCPType.TOOL


def test_route_map_excludes_streaming_refs_from_the_raw_spec():
    spec = {
        "paths": {
            "/events": {
                "get": {
                    "operationId": "watch",
                    "tags": ["System"],
                    "responses": {"200": {"$ref": "#/components/responses/Stream"}},
                }
            },
            "/status": {
                "get": {
                    "operationId": "status",
                    "tags": ["System"],
                    "responses": {"200": {"content": {"application/json": {"schema": {"type": "object"}}}}},
                }
            },
        },
        "components": {
            "responses": {
                "Stream": {"description": "sse", "content": {"text/event-stream": {"schema": {"type": "string"}}}}
            }
        },
    }
    assert streaming_route_keys(spec) == frozenset({("GET", "/events")})
    fn = build_route_map_fn(load_settings(BASE_ENV), spec)
    watched = SimpleNamespace(method="GET", path="/events", tags=["System"], operation_id="watch", responses={})
    kept = SimpleNamespace(method="GET", path="/status", tags=["System"], operation_id="status", responses={})
    assert fn(watched, MCPType.TOOL) is MCPType.EXCLUDE
    assert fn(kept, MCPType.TOOL) is MCPType.TOOL
