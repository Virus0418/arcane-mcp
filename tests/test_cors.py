"""HTTP CORS for browser MCP clients (ChatGPT action discovery after OAuth)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
from starlette.middleware.cors import CORSMiddleware

from arcane_mcp.config import load_settings
from arcane_mcp.server import (
    CORS_ALLOW_METHODS,
    CORS_EXPOSE_HEADERS,
    _starlette_app,
    build_http_app,
    build_server,
)
from tests.test_server import ENV, INIT, MCP_HEADERS, OAUTH_ENV, _oauth_http_auth, mock_client

CHATGPT = "https://chatgpt.com"
EVIL = "https://evil.example"
PREFLIGHT = {
    "Access-Control-Request-Method": "POST",
    "Access-Control-Request-Headers": "authorization,content-type,mcp-session-id,mcp-protocol-version",
}


@asynccontextmanager
async def asgi_client(app, base_url: str = "https://mcp.example.com") -> AsyncIterator[httpx2.AsyncClient]:
    inner = _starlette_app(app)
    transport = httpx2.ASGITransport(app=app)
    async with inner.router.lifespan_context(inner):
        async with httpx2.AsyncClient(transport=transport, base_url=base_url) as http:
            yield http


def _header_list(value: str | None) -> set[str]:
    return {part.strip().lower() for part in (value or "").split(",") if part.strip()}


def test_build_http_app_wraps_cors_when_origins_set(spec):
    settings = load_settings({**ENV, "MCP_HTTP_ALLOWED_ORIGINS": CHATGPT})
    app = build_http_app(build_server(settings, spec, mock_client([])), settings)
    assert isinstance(app, CORSMiddleware)
    assert app.allow_origins == [CHATGPT]


def test_build_http_app_skips_cors_when_no_origins(spec):
    settings = load_settings(ENV)
    app = build_http_app(build_server(settings, spec, mock_client([])), settings)
    assert not isinstance(app, CORSMiddleware)


async def test_options_allowed_origin_returns_cors_headers(spec):
    settings = load_settings({**ENV, "MCP_HTTP_ALLOWED_ORIGINS": f"{CHATGPT},https://chat.openai.com"})
    app = build_http_app(build_server(settings, spec, mock_client([])), settings)
    async with asgi_client(app) as http:
        r = await http.options("/mcp", headers={"Origin": CHATGPT, **PREFLIGHT})
    assert r.status_code in {200, 204}
    assert r.headers.get("access-control-allow-origin") == CHATGPT
    methods = _header_list(r.headers.get("access-control-allow-methods"))
    assert methods >= {m.lower() for m in CORS_ALLOW_METHODS}
    allowed = _header_list(r.headers.get("access-control-allow-headers"))
    for header in ("authorization", "content-type", "mcp-session-id", "mcp-protocol-version"):
        assert header in allowed, header


async def test_options_disallowed_origin_has_no_acao(spec):
    settings = load_settings({**ENV, "MCP_HTTP_ALLOWED_ORIGINS": CHATGPT})
    app = build_http_app(build_server(settings, spec, mock_client([])), settings)
    async with asgi_client(app) as http:
        r = await http.options("/mcp", headers={"Origin": EVIL, **PREFLIGHT})
    assert r.headers.get("access-control-allow-origin") is None
    assert r.status_code >= 400


async def test_post_401_includes_acao_for_allowed_origin(spec):
    settings = load_settings({**ENV, "MCP_HTTP_ALLOWED_ORIGINS": CHATGPT})
    app = build_http_app(build_server(settings, spec, mock_client([])), settings)
    async with asgi_client(app) as http:
        unauth = await http.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Origin": CHATGPT})
        authed = await http.post(
            "/mcp",
            json=INIT,
            headers={**MCP_HEADERS, "Origin": CHATGPT, "Authorization": "Bearer bearer-secret"},
        )
        blocked = await http.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Origin": EVIL})
    assert unauth.status_code == 401
    assert unauth.headers.get("access-control-allow-origin") == CHATGPT
    assert "mcp-session-id" in _header_list(unauth.headers.get("access-control-expose-headers"))
    assert authed.status_code == 200
    assert authed.headers.get("access-control-allow-origin") == CHATGPT
    assert blocked.status_code == 401
    assert blocked.headers.get("access-control-allow-origin") is None


async def test_oauth_cors_preflight_and_prm_and_jwt(spec):
    key_pair, auth = _oauth_http_auth()
    settings = load_settings(OAUTH_ENV)  # oauth defaults include ChatGPT origins
    assert CHATGPT in settings.mcp_http_allowed_origins
    mcp = build_server(settings, spec, mock_client([]), auth=auth)
    token = key_pair.create_token(
        subject="chatgpt-user",
        issuer=OAUTH_ENV["OAUTH_ISSUER_URL"],
        audience=OAUTH_ENV["OAUTH_AUDIENCE"],
        scopes=["mcp"],
    )
    app = build_http_app(mcp, settings)
    async with asgi_client(app) as http:
        preflight = await http.options("/mcp", headers={"Origin": CHATGPT, **PREFLIGHT})
        assert preflight.status_code in {200, 204}
        assert preflight.headers.get("access-control-allow-origin") == CHATGPT

        denied = await http.options("/mcp", headers={"Origin": EVIL, **PREFLIGHT})
        assert denied.headers.get("access-control-allow-origin") is None

        prm = await http.get("/.well-known/oauth-protected-resource/mcp", headers={"Origin": CHATGPT})
        assert prm.status_code == 200
        assert prm.headers.get("access-control-allow-origin") == CHATGPT
        body = prm.json()
        assert body["resource"].rstrip("/") == "https://mcp.example.com/mcp"
        assert [s.rstrip("/") for s in body["authorization_servers"]] == ["https://tenant.logto.app/oidc"]

        unauth = await http.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Origin": CHATGPT})
        assert unauth.status_code == 401
        assert unauth.headers.get("access-control-allow-origin") == CHATGPT
        www = unauth.headers.get("www-authenticate", "")
        assert www.lower().startswith("bearer")
        assert "resource_metadata=" in www

        authed = await http.post(
            "/mcp",
            json=INIT,
            headers={**MCP_HEADERS, "Origin": CHATGPT, "Authorization": f"Bearer {token}"},
        )
        assert authed.status_code == 200
        assert authed.headers.get("access-control-allow-origin") == CHATGPT
