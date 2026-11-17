"""Arcane 3.0.1 compatibility against the official OpenAPI export.

The fixture ``tests/fixtures/openapi-v3.0.1.json`` is the unmodified JSON from
the official ``getarcaneapp/arcane`` release binary ``arcane_linux_amd64`` at
tag ``v3.0.1`` (git ``c614e0d2df6d6bcb6188c1af249f457000c7e96c``), produced by
``./arcane openapi -f json``. SHA-256
``a2381f0425261e73b0a5d87b30d3eee12d62e27a8713de6edbddbd9b49d61013``.
It is not a hand-written spec and not a copy of a live NAS response.
``SetupAPIForSpec`` in that binary uses an empty config, so the document is
the manager-shaped static export.
"""

from __future__ import annotations

import hashlib
import json

import httpx2
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from arcane_mcp.config import DEFAULT_TAGS, load_settings
from arcane_mcp.logs import container_logs_template
from arcane_mcp.safety import CONFIRM_TOOL_NAME, DESTRUCTIVE_ACTIONS
from arcane_mcp.server import build_server
from arcane_mcp.spec import HARD_EXCLUDE, streaming_route_keys
from tests.conftest import FIXTURES
from tests.test_server import ENV

V3_SPEC_SHA256 = "a2381f0425261e73b0a5d87b30d3eee12d62e27a8713de6edbddbd9b49d61013"
# Default tags, confirmation on, logs on, after dropping the one default-tag
# SSE operation (stream_upgrade_progress). The pre-fix audit counted 132 tools
# because that SSE tool was still generated. 132 - 1 = 131. Do not assert 132
# together with the SSE exclusion.
V3_EXPOSED_TOOL_COUNT = 131
LOG_TEMPLATE = "/agents/{id}/containers/{containerId}/logs/download"
UPGRADE_PROGRESS = "/agents/{id}/system/upgrade/progress"

DISAPPEARED = (
    "archive_project",
    "bulk_ignore_vulnerabilities",
    "bulk_unignore_vulnerabilities",
    "cancel_activity",
    "check_all_images",
    "check_multiple_images",
    "connect_network_container",
    "disconnect_network_container",
    "down_project",
    "download_edge_mtlsca",
    "get_dashboard",
    "kill_container",
    "pair_agent",
    "pause_container",
    "pull_project_images",
    "redeploy_project",
    "restart_container",
    "restart_project",
    "start_all_containers",
    "start_all_stopped_containers",
    "start_container",
    "stop_all_containers",
    "stop_container",
    "trigger_update_all",
    "unarchive_project",
    "unpause_container",
    "update_all_status",
    "update_heartbeat",
    "update_project_services",
)
NO_EQUIVALENT = (
    "get_dashboard",
    "pair_agent",
    "update_heartbeat",
    "trigger_update_all",
    "update_all_status",
)
SUCCESSORS = (
    "container_action",
    "project_action",
    "deploy_project",
    "activity_action",
    "check_images",
    "network_container_action",
    "download_agent_mtlsca",
    "system_containers_action",
    "bulk_ignored_vulnerabilities_action",
)
ID_PATH_TOOLS = (
    "update_project",
    "update_environment",
    "create_volume",
    "ignore_vulnerability",
)


def test_v3_fixture_is_the_official_export():
    raw = (FIXTURES / "openapi-v3.0.1.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == V3_SPEC_SHA256
    document = json.loads(raw)
    assert document["openapi"] == "3.1.0"
    assert document["info"]["version"] == "3.0.1"
    assert document["info"]["title"] == "Arcane API"
    assert LOG_TEMPLATE in document["paths"]
    assert "/environments/{id}/containers" not in document["paths"]
    progress = document["paths"][UPGRADE_PROGRESS]["get"]
    assert progress["operationId"] == "stream-upgrade-progress"
    assert "text/event-stream" in progress["responses"]["200"]["content"]
    assert HARD_EXCLUDE.search(UPGRADE_PROGRESS) is None


def test_v3_spec_selects_agents_logs_and_marks_only_real_streams(v3_spec):
    assert container_logs_template(v3_spec) == LOG_TEMPLATE
    streams = streaming_route_keys(v3_spec)
    assert ("GET", UPGRADE_PROGRESS) in streams
    assert ("GET", "/stream") in streams
    default_exposed = []
    for method, path in streams:
        if HARD_EXCLUDE.search(path):
            continue
        operation = v3_spec["paths"][path][method.lower()]
        if set(operation.get("tags") or []) & set(DEFAULT_TAGS):
            default_exposed.append((method, path, operation.get("operationId")))
    assert default_exposed == [("GET", UPGRADE_PROGRESS, "stream-upgrade-progress")]


def _client(seen: list) -> httpx2.AsyncClient:
    def handler(req: httpx2.Request) -> httpx2.Response:
        raw = req.url.raw_path.decode()
        path, _, query = raw.partition("?")
        seen.append(
            {
                "method": req.method,
                "path": path,
                "query": query,
                "body": req.content.decode() if req.content else "",
                "api_key": req.headers.get("x-api-key"),
            }
        )
        if req.url.path.endswith("/logs/download"):
            return httpx2.Response(200, content=b"line-1\nline-2\n")
        return httpx2.Response(200, json={"success": True, "data": []})

    return httpx2.AsyncClient(
        base_url="http://arcane.test/api",
        headers={"X-API-Key": "test-key", "Accept": "application/json"},
        transport=httpx2.MockTransport(handler),
    )


def _server(v3_spec, seen: list, env: dict | None = None):
    return build_server(load_settings(env or ENV), v3_spec, _client(seen), auth=False)


def _schema(tool) -> dict:
    schema = getattr(tool, "input_schema", None)
    if not schema:
        schema = tool.inputSchema
    return schema


def _enum(prop: dict) -> set:
    found: set = set()
    if not isinstance(prop, dict):
        return found
    if "enum" in prop:
        found.update(prop["enum"])
    for key in ("anyOf", "oneOf", "allOf"):
        for item in prop.get(key) or []:
            found |= _enum(item)
    return found


async def _tools(mcp) -> dict:
    async with Client(mcp) as client:
        return {tool.name: tool for tool in await client.list_tools()}


async def test_v3_default_inventory_drops_sse_and_old_names(v3_spec):
    tools = await _tools(_server(v3_spec, []))
    assert len(tools) == V3_EXPOSED_TOOL_COUNT, sorted(tools)
    assert len(DISAPPEARED) == 29
    for name in DISAPPEARED:
        assert name not in tools, name
    for name in SUCCESSORS:
        assert name in tools, name
    for name in NO_EQUIVALENT:
        assert name not in tools
    assert "stream_upgrade_progress" not in tools
    assert "download_container_logs" not in tools
    assert "get_container_logs" in tools
    assert CONFIRM_TOOL_NAME in tools
    assert "list_containers" in tools
    assert "list_environments" in tools
    assert "start_upgrade_job" in tools
    assert "get_current_upgrade_job" in tools
    assert "enroll_agent" not in tools
    description = tools["get_container_logs"].description
    assert LOG_TEMPLATE in description
    confirm = _schema(tools[CONFIRM_TOOL_NAME])
    assert set(confirm["properties"]) == {"token"}
    deploy = _schema(tools["deploy_project"])
    assert "action" in deploy["required"]
    assert _enum(deploy["properties"]["action"]) == {"up", "redeploy", "pull"}
    container = _schema(tools["container_action"])
    project = _schema(tools["project_action"])
    assert {"id", "containerId", "action"} <= set(container["required"])
    assert {"id", "projectId", "action"} <= set(project["required"])
    assert _enum(container["properties"]["action"]) == {
        "start",
        "stop",
        "restart",
        "kill",
        "pause",
        "unpause",
    }
    assert "down" in _enum(_schema(tools["project_action"])["properties"]["action"])
    for name in ID_PATH_TOOLS:
        props = _schema(tools[name])["properties"]
        assert "id__path" in props, name
        assert "id" in props, name


async def test_v3_urls_actions_and_confirmation_binding(v3_spec):
    seen: list = []
    mcp = _server(v3_spec, seen)
    async with Client(mcp) as client:
        listed = await client.call_tool("list_containers", {"id": "0"})
        assert listed.structured_content["success"] is True
        assert seen == [
            {"method": "GET", "path": "/api/agents/0/containers", "query": "", "body": "", "api_key": "test-key"}
        ]

        seen.clear()
        started = await client.call_tool(
            "container_action", {"id": "0", "containerId": "abc", "action": "start"}
        )
        assert started.structured_content["success"] is True
        assert seen == [
            {
                "method": "POST",
                "path": "/api/agents/0/containers/abc",
                "query": "action=start",
                "body": "",
                "api_key": "test-key",
            }
        ]

        seen.clear()
        kill = await client.call_tool(
            "container_action", {"id": "0", "containerId": "abc", "action": "kill"}
        )
        assert kill.structured_content["requires_confirmation"] is True
        assert kill.structured_content["arguments"]["action"] == "kill"
        assert kill.structured_content["action"] == "kill"
        assert seen == []

        started_again = await client.call_tool(
            "container_action", {"id": "0", "containerId": "abc", "action": "start"}
        )
        assert started_again.structured_content["success"] is True
        assert seen == [
            {
                "method": "POST",
                "path": "/api/agents/0/containers/abc",
                "query": "action=start",
                "body": "",
                "api_key": "test-key",
            }
        ]

        seen.clear()
        down = await client.call_tool(
            "project_action", {"id": "0", "projectId": "web", "action": "down"}
        )
        assert down.structured_content["requires_confirmation"] is True
        assert down.structured_content["arguments"]["action"] == "down"
        assert seen == []

        confirmed = await client.call_tool(CONFIRM_TOOL_NAME, {"token": kill.structured_content["token"]})
        assert confirmed.structured_content["success"] is True
        assert seen == [
            {
                "method": "POST",
                "path": "/api/agents/0/containers/abc",
                "query": "action=kill",
                "body": "",
                "api_key": "test-key",
            }
        ]
        with pytest.raises(ToolError, match="unknown|already"):
            await client.call_tool(CONFIRM_TOOL_NAME, {"token": kill.structured_content["token"]})
        assert seen == [
            {
                "method": "POST",
                "path": "/api/agents/0/containers/abc",
                "query": "action=kill",
                "body": "",
                "api_key": "test-key",
            }
        ]

        seen.clear()
        confirmed_down = await client.call_tool(
            CONFIRM_TOOL_NAME, {"token": down.structured_content["token"]}
        )
        assert confirmed_down.structured_content["success"] is True
        assert seen == [
            {
                "method": "POST",
                "path": "/api/agents/0/projects/web",
                "query": "action=down",
                "body": "",
                "api_key": "test-key",
            }
        ]

        seen.clear()
        deployed = await client.call_tool(
            "deploy_project", {"id": "0", "projectId": "web", "action": "redeploy"}
        )
        assert deployed.structured_content["success"] is True
        assert seen == [
            {
                "method": "POST",
                "path": "/api/agents/0/projects/web/deploy",
                "query": "action=redeploy",
                "body": "",
                "api_key": "test-key",
            }
        ]

        seen.clear()
        updated = await client.call_tool(
            "update_project",
            {
                "id__path": "0",
                "projectId": "web",
                "id": "body-id",
                "status": "running",
                "path": "/data/web",
                "updatedAt": "2026-01-01T00:00:00Z",
                "createdAt": "2026-01-01T00:00:00Z",
                "tags": [],
                "runningCount": 0,
                "serviceCount": 1,
                "isArchived": False,
            },
        )
        assert updated.structured_content["success"] is True
        assert seen[0]["method"] == "PATCH"
        assert seen[0]["path"] == "/api/agents/0/projects/web"
        body = json.loads(seen[0]["body"])
        assert body["id"] == "body-id"
        assert "{id}" not in seen[0]["path"]

        seen.clear()
        logs = await client.call_tool(
            "get_container_logs", {"id": "0", "containerId": "foo/bar", "tail": 5}
        )
        assert logs.structured_content["success"] is True
        assert logs.structured_content["content"] == "line-1\nline-2\n"
        assert seen == [
            {
                "method": "GET",
                "path": "/api/agents/0/containers/foo%2Fbar/logs/download",
                "query": "",
                "body": "",
                "api_key": "test-key",
            }
        ]


async def test_restore_actions_stay_gated_when_those_tags_are_enabled(v3_spec):
    seen: list = []
    env = {**ENV, "ARCANE_MCP_TAGS": "System Backups,Volume Backup"}
    mcp = _server(v3_spec, seen, env)
    async with Client(mcp) as client:
        names = {tool.name for tool in await client.list_tools()}
        assert "system_backup_action" in names
        assert "volume_backup_action" in names
        assert "stream_upgrade_progress" not in names
        restore = await client.call_tool(
            "system_backup_action", {"id": "0", "backupId": "bk", "action": "restore"}
        )
        files = await client.call_tool(
            "volume_backup_action",
            {"id": "0", "volumeName": "data", "backupId": "bk", "action": "restorefiles"},
        )
        assert restore.structured_content["requires_confirmation"] is True
        assert files.structured_content["requires_confirmation"] is True
        assert seen == []
        await client.call_tool(CONFIRM_TOOL_NAME, {"token": restore.structured_content["token"]})
        assert seen == [
            {
                "method": "POST",
                "path": "/api/agents/0/backups/bk",
                "query": "action=restore",
                "body": "",
                "api_key": "test-key",
            }
        ]
        seen.clear()
        await client.call_tool(CONFIRM_TOOL_NAME, {"token": files.structured_content["token"]})
        assert seen == [
            {
                "method": "POST",
                "path": "/api/agents/0/volumes/data/backups/bk",
                "query": "action=restorefiles",
                "body": "",
                "api_key": "test-key",
            }
        ]


async def test_read_only_cannot_be_bypassed_with_action(v3_spec):
    seen: list = []
    mcp = _server(v3_spec, seen, {**ENV, "ARCANE_MCP_READ_ONLY": "true"})
    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        assert "list_containers" in tools
        assert "get_container_logs" in tools
        assert "container_action" not in tools
        assert "project_action" not in tools
        assert "deploy_project" not in tools
        assert "system_backup_action" not in tools
        assert "stream_upgrade_progress" not in tools
        for tool in tools.values():
            action = _schema(tool)["properties"].get("action")
            if not action:
                continue
            assert not ({str(item).lower() for item in _enum(action)} & DESTRUCTIVE_ACTIONS)
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool(
                "container_action", {"id": "0", "containerId": "abc", "action": "kill"}
            )
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool(
                "container_action", {"id": "0", "containerId": "abc", "action": "start"}
            )
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool("delete_container", {"id": "0", "containerId": "abc"})
        assert seen == []
        listed = await client.call_tool("list_containers", {"id": "0"})
        assert listed.structured_content["success"] is True
        assert seen[0]["path"] == "/api/agents/0/containers"
        assert seen[0]["method"] == "GET"


async def test_disabling_confirmation_still_sends_the_requested_action(v3_spec):
    seen: list = []
    mcp = _server(v3_spec, seen, {**ENV, "ARCANE_MCP_CONFIRM_DESTRUCTIVE": "false"})
    async with Client(mcp) as client:
        result = await client.call_tool(
            "container_action", {"id": "0", "containerId": "abc", "action": "kill"}
        )
    assert result.structured_content["success"] is True
    assert seen == [
        {
            "method": "POST",
            "path": "/api/agents/0/containers/abc",
            "query": "action=kill",
            "body": "",
            "api_key": "test-key",
        }
    ]
