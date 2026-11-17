import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from arcane_mcp.safety import (
    CONFIRM_TOOL_NAME,
    ConfirmationError,
    ConfirmationMiddleware,
    ConfirmationStore,
    destructive_action,
    destructive_tool_names,
    is_destructive,
    register_confirm_tool,
)


@pytest.mark.parametrize("method,path,expected", [
    ("DELETE", "/environments/{id}/containers/{containerId}", True),
    ("delete", "/api-keys/{id}", True),
    ("POST", "/environments/{id}/images/prune", True),
    ("POST", "/environments/{id}/projects/{projectId}/down", True),
    ("POST", "/environments/{id}/containers/{containerId}/kill", True),
    ("POST", "/backups/{id}/restore", True),
    ("POST", "/backups/{id}/restore-files", True),
    ("DELETE", "/environments/{id}/projects/{projectId}/destroy", True),
    ("POST", "/environments/{id}/containers/{containerId}/stop", False),
    ("POST", "/environments/{id}/containers/{containerId}/restart", False),
    ("GET", "/environments/{id}/containers", False),
    ("GET", "/environments/{id}/updater/history", False),
    ("POST", "/environments/{id}/projects/{projectId}/downgrade", False),
])
def test_is_destructive(method, path, expected):
    assert is_destructive(method, path) is expected


def test_is_destructive_extra_patterns():
    assert is_destructive("POST", "/environments/{id}/system/reset", (r"/reset$",)) is True
    assert is_destructive("POST", "/environments/{id}/system/reset") is False


def test_destructive_tool_names_from_spec(spec):
    names = destructive_tool_names(spec)
    assert "delete_container" in names
    assert "prune_images" in names
    assert "destroy_project" in names
    assert "kill_container" in names
    assert "list_containers" not in names
    assert "stop_container" not in names
    assert all("-" not in n for n in names)


def test_destructive_tool_names_extra_pattern(spec):
    base = destructive_tool_names(spec)
    more = destructive_tool_names(spec, (r"/stop$",))
    assert "stop_container" in more and "stop_container" not in base


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make_store(**kw):
    clock = FakeClock()
    tokens = iter(["tok1", "tok2", "tok3"])
    store = ConfirmationStore(clock=clock, token_factory=lambda: next(tokens), **kw)
    return store, clock


def test_issue_and_redeem():
    store, _ = make_store()
    token, pending = store.issue("delete_container", {"id": "0", "containerId": "abc"})
    assert token == "tok1"
    assert pending.tool == "delete_container"
    assert pending.expires_at == 1120.0
    got = store.redeem("tok1")
    assert got.arguments == {"id": "0", "containerId": "abc"}


def test_redeem_is_single_use():
    store, _ = make_store()
    token, _ = store.issue("delete_container", {})
    store.redeem(token)
    with pytest.raises(ConfirmationError, match="unknown|already"):
        store.redeem(token)


def test_redeem_unknown():
    store, _ = make_store()
    with pytest.raises(ConfirmationError, match="unknown"):
        store.redeem("nope")


def test_redeem_expired():
    store, clock = make_store(ttl=10)
    token, _ = store.issue("delete_container", {})
    clock.t += 11
    with pytest.raises(ConfirmationError, match="expired"):
        store.redeem(token)


def test_expired_tokens_are_purged_on_issue():
    store, clock = make_store(ttl=10)
    store.issue("a", {})
    clock.t += 11
    store.issue("b", {})
    assert store.pending_count() == 1


# ---- middleware + confirm tool ----


def build_gated_server(store: ConfirmationStore) -> tuple[FastMCP, list]:
    calls: list = []
    mcp = FastMCP("gated", middleware=[ConfirmationMiddleware(store, frozenset({"delete_thing"}))])

    @mcp.tool
    def delete_thing(name: str) -> dict:
        calls.append(("delete", name))
        return {"deleted": name}

    @mcp.tool
    def list_things() -> dict:
        calls.append(("list",))
        return {"things": []}

    register_confirm_tool(mcp, store)
    return mcp, calls


async def test_non_destructive_passes_through():
    store, _ = make_store()
    mcp, calls = build_gated_server(store)
    async with Client(mcp) as c:
        r = await c.call_tool("list_things", {})
    assert r.structured_content == {"things": []}
    assert calls == [("list",)]


async def test_destructive_returns_confirmation_and_does_not_run():
    store, _ = make_store()
    mcp, calls = build_gated_server(store)
    async with Client(mcp) as c:
        r = await c.call_tool("delete_thing", {"name": "web"})
    body = r.structured_content
    assert body["requires_confirmation"] is True
    assert body["token"] == "tok1"
    assert body["operation"] == "delete_thing"
    assert body["arguments"] == {"name": "web"}
    assert body["expires_in"] == 120
    assert CONFIRM_TOOL_NAME in body["message"]
    assert calls == []


async def test_confirm_runs_original_and_is_single_use():
    store, _ = make_store()
    mcp, calls = build_gated_server(store)
    async with Client(mcp) as c:
        first = await c.call_tool("delete_thing", {"name": "web"})
        token = first.structured_content["token"]
        second = await c.call_tool(CONFIRM_TOOL_NAME, {"token": token})
        assert second.structured_content == {"deleted": "web"}
        assert calls == [("delete", "web")]
        with pytest.raises(ToolError, match="unknown|already"):
            await c.call_tool(CONFIRM_TOOL_NAME, {"token": token})


async def test_confirm_unknown_token_is_tool_error():
    store, _ = make_store()
    mcp, _ = build_gated_server(store)
    async with Client(mcp) as c:
        with pytest.raises(ToolError, match="unknown"):
            await c.call_tool(CONFIRM_TOOL_NAME, {"token": "bogus"})


async def test_confirm_expired_token_is_tool_error():
    store, clock = make_store(ttl=10)
    mcp, _ = build_gated_server(store)
    async with Client(mcp) as c:
        first = await c.call_tool("delete_thing", {"name": "web"})
        clock.t += 11
        with pytest.raises(ToolError, match="expired"):
            await c.call_tool(CONFIRM_TOOL_NAME, {"token": first.structured_content["token"]})


async def test_confirm_tool_is_listed():
    store, _ = make_store()
    mcp, _ = build_gated_server(store)
    async with Client(mcp) as c:
        names = {t.name for t in await c.list_tools()}
    assert CONFIRM_TOOL_NAME in names


def test_destructive_action_matches_kill_down_and_restore_only():
    assert destructive_action({"action": " Kill "}) == "kill"
    assert destructive_action({"action": "DOWN"}) == "down"
    assert destructive_action({"action": "restore"}) == "restore"
    assert destructive_action({"action": "restoreFiles"}) == "restorefiles"
    assert destructive_action({"action": "restore-files"}) == "restore-files"
    assert destructive_action({"action": "start"}) is None
    assert destructive_action({"action": "up"}) is None
    assert destructive_action({"action": "stopall"}) is None
    assert destructive_action({"action": 1}) is None
    assert destructive_action({}) is None
    assert destructive_action(None) is None


def build_action_server(store: ConfirmationStore) -> tuple[FastMCP, list]:
    calls: list = []
    mcp = FastMCP("actions", middleware=[ConfirmationMiddleware(store, frozenset())])

    @mcp.tool
    def container_action(id: str, containerId: str, action: str) -> dict:
        calls.append(("container", action))
        return {"ran": action}

    @mcp.tool
    def project_action(id: str, projectId: str, action: str) -> dict:
        calls.append(("project", action))
        return {"ran": action}

    register_confirm_tool(mcp, store)
    return mcp, calls


async def test_action_token_is_bound_to_the_original_request():
    store, _ = make_store()
    mcp, calls = build_action_server(store)
    async with Client(mcp) as c:
        started = await c.call_tool(
            "container_action", {"id": "0", "containerId": "abc", "action": "start"}
        )
        assert started.structured_content == {"ran": "start"}
        kill = await c.call_tool(
            "container_action", {"id": "0", "containerId": "abc", "action": "Kill"}
        )
        body = kill.structured_content
        assert body["requires_confirmation"] is True
        assert body["action"] == "kill"
        assert body["arguments"] == {"id": "0", "containerId": "abc", "action": "Kill"}
        down = await c.call_tool(
            "project_action", {"id": "0", "projectId": "web", "action": "down"}
        )
        assert down.structured_content["arguments"]["action"] == "down"
        assert calls == [("container", "start")]
        confirmed = await c.call_tool(CONFIRM_TOOL_NAME, {"token": body["token"]})
        assert confirmed.structured_content == {"ran": "Kill"}
        confirmed_down = await c.call_tool(
            CONFIRM_TOOL_NAME, {"token": down.structured_content["token"]}
        )
        assert confirmed_down.structured_content == {"ran": "down"}
    assert calls == [("container", "start"), ("container", "Kill"), ("project", "down")]


async def test_hidden_tool_is_not_offered_a_confirmation_token():
    store, _ = make_store()
    mcp = FastMCP(
        "hidden",
        middleware=[ConfirmationMiddleware(store, frozenset({"delete_thing"}), exposed=frozenset({"list_things"}))],
    )

    @mcp.tool
    def list_things() -> dict:
        return {"things": []}

    register_confirm_tool(mcp, store)
    async with Client(mcp) as client:
        listed = await client.call_tool("list_things", {})
        assert listed.structured_content == {"things": []}
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool("container_action", {"id": "0", "containerId": "abc", "action": "kill"})
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool("delete_thing", {"name": "web"})
    assert store.pending_count() == 0
