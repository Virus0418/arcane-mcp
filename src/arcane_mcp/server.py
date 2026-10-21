"""Assemble the FastMCP server and run it over Streamable HTTP."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

import httpx2
import uvicorn
from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider, RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier, StaticTokenVerifier
from fastmcp.server.providers.openapi import OpenAPIProvider
from pydantic import AnyHttpUrl
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from arcane_mcp import __version__
from arcane_mcp.config import (
    DEFAULT_OAUTH_JWT_ALGORITHM_FALLBACK,
    ConfigError,
    Settings,
    load_settings,
)
from arcane_mcp.safety import ConfirmationMiddleware, ConfirmationStore, destructive_tool_names, register_confirm_tool
from arcane_mcp.spec import SpecFetchError, build_component_fn, build_route_map_fn, fetch_spec, normalise_spec

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Tools for managing Docker via Arcane. Most tools take an environment `id`; the local Docker host is "
    'environment "0" and remote agents have UUIDs (see list_environments). Destructive tools return '
    "requires_confirmation=true with a token instead of executing; call confirm_operation(token) to proceed."
)

MCP_HTTP_PATH = "/mcp"
CORS_ALLOW_METHODS = ("GET", "POST", "DELETE", "OPTIONS")
CORS_ALLOW_HEADERS = (
    "Accept",
    "Authorization",
    "Content-Type",
    "Last-Event-ID",
    "mcp-protocol-version",
    "mcp-session-id",
    "Mcp-Method",
    "Mcp-Name",
)
CORS_EXPOSE_HEADERS = ("mcp-session-id",)


def make_arcane_client(settings: Settings) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        base_url=f"{settings.arcane_base_url}/api",
        headers={"X-API-Key": settings.arcane_api_key, "Accept": "application/json"},
        timeout=60.0,
    )


_EC_ALG_CURVE = {"ES256": "P-256", "ES384": "P-384", "ES512": "P-521"}
_CURVE_EC_ALG = {curve: alg for alg, curve in _EC_ALG_CURVE.items()}


def fetch_jwks(jwks_uri: str, *, client: httpx2.Client | None = None) -> dict[str, Any]:
    close = client is None
    http = client or httpx2.Client(timeout=5.0)
    try:
        response = http.get(jwks_uri)
        response.raise_for_status()
        data = response.json()
    finally:
        if close:
            http.close()
    if not isinstance(data, dict):
        raise ValueError("JWKS response must be a JSON object")
    return data


def _jwk_algorithm(key: dict[str, Any]) -> str | None:
    if key.get("use") == "enc":
        return None
    alg = key.get("alg")
    if isinstance(alg, str) and alg.strip():
        return alg.strip()
    kty = key.get("kty")
    if kty == "RSA":
        return "RS256"
    if kty == "EC":
        return _CURVE_EC_ALG.get(str(key.get("crv") or ""))
    if kty == "OKP":
        crv = key.get("crv")
        if crv == "Ed25519":
            return "Ed25519"
        if crv == "Ed448":
            return "Ed448"
    return None


def algorithm_from_jwks(jwks: dict[str, Any]) -> str | None:
    keys = jwks.get("keys")
    if not isinstance(keys, list):
        return None
    for key in keys:
        if isinstance(key, dict):
            alg = _jwk_algorithm(key)
            if alg:
                return alg
    return None


def _kty_for_algorithm(algorithm: str) -> str | None:
    if algorithm.startswith(("RS", "PS")):
        return "RSA"
    if algorithm.startswith("ES"):
        return "EC"
    if algorithm.startswith("HS"):
        return "oct"
    if algorithm in {"EdDSA", "Ed25519", "Ed448"}:
        return "OKP"
    return None


def jwks_supports_algorithm(jwks: dict[str, Any], algorithm: str) -> bool:
    keys = jwks.get("keys")
    if not isinstance(keys, list):
        return False
    expected_kty = _kty_for_algorithm(algorithm)
    expected_crv = _EC_ALG_CURVE.get(algorithm)
    for key in keys:
        if not isinstance(key, dict) or key.get("use") == "enc":
            continue
        if key.get("alg") and key["alg"] != algorithm:
            continue
        if expected_kty and key.get("kty") != expected_kty:
            continue
        if expected_crv and key.get("crv") not in {None, expected_crv}:
            continue
        if key.get("kty") or key.get("alg"):
            return True
    return False


def resolve_oauth_jwt_algorithm(
    settings: Settings,
    *,
    jwks_client: httpx2.Client | None = None,
) -> str:
    """Resolve JWTVerifier algorithm. `auto` reads JWKS; Logto fallback is ES384."""
    jwks_uri = settings.oauth_jwks_uri
    configured = settings.oauth_jwt_algorithm or "auto"
    jwks: dict[str, Any] | None = None
    if jwks_uri:
        try:
            jwks = fetch_jwks(jwks_uri, client=jwks_client)
        except Exception as exc:
            logger.warning("Failed to fetch JWKS from %s: %s", jwks_uri, exc)

    if configured == "auto":
        picked = algorithm_from_jwks(jwks) if jwks is not None else None
        algorithm = picked or DEFAULT_OAUTH_JWT_ALGORITHM_FALLBACK
        if picked:
            logger.info("OAuth JWT algorithm auto-detected from JWKS: %s", algorithm)
        else:
            logger.warning(
                "OAuth JWT algorithm auto-detect failed; falling back to %s",
                algorithm,
            )
    else:
        algorithm = configured

    keys = jwks.get("keys") if isinstance(jwks, dict) else None
    if isinstance(keys, list) and keys and not jwks_supports_algorithm(jwks or {}, algorithm):
        logger.warning(
            "JWKS at %s has %d keys but none match algorithm %s",
            jwks_uri,
            len(keys),
            algorithm,
        )
    return algorithm


def make_auth_provider(
    settings: Settings,
    *,
    jwks_client: httpx2.Client | None = None,
    token_http_client: httpx2.AsyncClient | None = None,
) -> AuthProvider:
    """Build MCP ingress auth. Arcane API auth (ARCANE_API_KEY) is separate."""
    if settings.mcp_auth_mode == "oauth":
        issuer = settings.oauth_issuer_url
        audience = settings.oauth_audience
        jwks_uri = settings.oauth_jwks_uri
        public_base = settings.mcp_public_base_url
        if not issuer or not audience or not jwks_uri or not public_base:
            raise ConfigError("oauth mode is missing issuer, audience, JWKS URI, or public base URL")
        scopes = list(settings.oauth_scopes)
        algorithm = resolve_oauth_jwt_algorithm(settings, jwks_client=jwks_client)
        verifier = JWTVerifier(
            jwks_uri=jwks_uri,
            issuer=issuer,
            audience=audience,
            algorithm=algorithm,
            required_scopes=scopes or None,
            http_client=token_http_client,
        )
        logger.info(
            "OAuth auth: issuer=%s audience=%s jwks=%s scopes=%s algorithm=%s",
            issuer,
            audience,
            jwks_uri,
            ",".join(scopes) if scopes else "(none)",
            algorithm,
        )
        return RemoteAuthProvider(
            token_verifier=verifier,
            authorization_servers=[AnyHttpUrl(issuer)],
            base_url=public_base,
            scopes_supported=scopes or None,
            resource_name="arcane",
        )

    if not settings.mcp_auth_token:
        raise ConfigError("MCP_AUTH_TOKEN is required in bearer mode")
    return StaticTokenVerifier(
        tokens={settings.mcp_auth_token: {"client_id": "arcane-mcp-client", "scopes": []}}
    )


def build_server(
    settings: Settings,
    spec: dict[str, Any],
    client: httpx2.AsyncClient,
    *,
    auth: bool | AuthProvider = True,
) -> FastMCP:
    normalised = normalise_spec(spec, settings.arcane_base_url)
    provider = OpenAPIProvider(
        normalised,
        client,
        route_map_fn=build_route_map_fn(settings),
        mcp_component_fn=build_component_fn(),
        validate_output=False,
    )

    middleware = []
    store: ConfirmationStore | None = None
    if settings.confirm_destructive:
        store = ConfirmationStore()
        destructive = destructive_tool_names(normalised, settings.destructive_patterns)
        middleware.append(ConfirmationMiddleware(store, destructive))
        logger.info("Confirmation gate active for %d destructive tools", len(destructive))

    if auth is True:
        auth_provider: AuthProvider | None = make_auth_provider(settings)
    elif auth is False:
        auth_provider = None
    else:
        auth_provider = auth

    mcp = FastMCP(
        "arcane",
        instructions=INSTRUCTIONS,
        version=__version__,
        auth=auth_provider,
        middleware=middleware,
        providers=[provider],
    )
    if store is not None:
        register_confirm_tool(mcp, store)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        tools = await mcp.list_tools(run_middleware=False)
        return JSONResponse(
            {"status": "ok", "version": __version__, "arcane": settings.arcane_base_url, "tools": len(tools)}
        )

    return mcp


def build_http_app(mcp: FastMCP, settings: Settings) -> ASGIApp:
    """Streamable HTTP ASGI app at /mcp, with CORS outside auth so preflight and 401s get ACAO."""
    origins = list(settings.mcp_http_allowed_origins)
    app = mcp.http_app(
        path=MCP_HTTP_PATH,
        allowed_origins=origins or None,
    )
    if not origins:
        return app
    logger.info("CORS allowed origins: %s", ",".join(origins))
    # Wrap after construction so CORS is outermost: OPTIONS never hits the /mcp
    # route (GET/POST/DELETE only → 405), and 401s still include ACAO.
    return CORSMiddleware(
        app,
        allow_origins=origins,
        allow_methods=list(CORS_ALLOW_METHODS),
        allow_headers=list(CORS_ALLOW_HEADERS),
        expose_headers=list(CORS_EXPOSE_HEADERS),
    )


def _starlette_app(app: ASGIApp) -> ASGIApp:
    """Unwrap CORSMiddleware to reach the FastMCP Starlette app (lifespan, router)."""
    inner = app
    while isinstance(inner, CORSMiddleware):
        inner = inner.app
    return inner


async def build_server_from_live_spec(settings: Settings) -> FastMCP:
    client = make_arcane_client(settings)
    spec = await fetch_spec(client, timeout=settings.spec_timeout)
    logger.info("Arcane %s at %s", spec.get("info", {}).get("version", "?"), settings.arcane_base_url)
    return build_server(settings, spec, client)


def main() -> None:
    try:
        settings = load_settings(os.environ)
    except ConfigError as exc:
        print(f"arcane-mcp: {exc}", file=sys.stderr)
        sys.exit(2)
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        mcp = asyncio.run(build_server_from_live_spec(settings))
    except SpecFetchError as exc:
        logger.error("%s", exc)
        sys.exit(1)
    tool_count = len(asyncio.run(mcp.list_tools(run_middleware=False)))
    logger.info("Serving %d tools on http://%s:%d%s", tool_count, settings.mcp_host, settings.mcp_port, MCP_HTTP_PATH)
    # mcp.run() can pass Starlette middleware, but that lands inside auth/routing so
    # OPTIONS /mcp still 405s without ACAO. Wrap http_app() instead and serve with uvicorn.
    app = build_http_app(mcp, settings)
    uvicorn.run(app, host=settings.mcp_host, port=settings.mcp_port, log_level=settings.log_level.lower())


if __name__ == "__main__":
    main()
