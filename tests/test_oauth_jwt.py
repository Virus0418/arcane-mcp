"""OAuth JWT algorithm: Logto ES384 JWKS vs FastMCP JWTVerifier default RS256."""

from __future__ import annotations

import logging
import time

import httpx2
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from joserfc import jwk, jwt
from arcane_mcp.config import load_settings
from arcane_mcp.server import (
    algorithm_from_jwks,
    build_http_app,
    build_server,
    jwks_supports_algorithm,
    make_auth_provider,
    resolve_oauth_jwt_algorithm,
)
from tests.test_cors import asgi_client
from tests.test_server import INIT, MCP_HEADERS, OAUTH_ENV, _oauth_http_auth, mock_client

TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


class ECKeyPair:
    def __init__(self) -> None:
        private_key = ec.generate_private_key(ec.SECP384R1())
        self.private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        self.public_pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self.kid = "logto-ec-p384"
        public = jwk.import_key(self.public_pem, "EC")
        self.public_jwk = public.as_dict(private=False)
        self.public_jwk.update({"alg": "ES384", "use": "sig", "kid": self.kid})

    def create_token(self, *, subject: str, issuer: str, audience: str, scopes: list[str]) -> str:
        payload = {
            "sub": subject,
            "iss": issuer,
            "aud": audience,
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
            "scope": " ".join(scopes),
        }
        signing_key = jwk.import_key(self.private_pem, "EC")
        return jwt.encode(
            {"alg": "ES384", "kid": self.kid},
            payload,
            signing_key,
            algorithms=["ES384"],
        )


def _jwks_clients(jwks: dict):
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/jwks"):
            return httpx2.Response(200, json=jwks)
        return httpx2.Response(404)

    transport = httpx2.MockTransport(handler)
    return httpx2.Client(transport=transport), httpx2.AsyncClient(transport=transport)


def test_algorithm_from_jwks_reads_es384_and_curve():
    assert algorithm_from_jwks({"keys": [{"kty": "EC", "crv": "P-384", "alg": "ES384"}]}) == "ES384"
    assert algorithm_from_jwks({"keys": [{"kty": "EC", "crv": "P-384"}]}) == "ES384"
    assert algorithm_from_jwks({"keys": [{"kty": "RSA", "n": "x"}]}) == "RS256"
    assert algorithm_from_jwks({"keys": []}) is None


def test_jwks_supports_algorithm_requires_matching_curve():
    es384 = {"keys": [{"kty": "EC", "crv": "P-384", "alg": "ES384"}]}
    assert jwks_supports_algorithm(es384, "ES384") is True
    assert jwks_supports_algorithm(es384, "RS256") is False
    assert jwks_supports_algorithm(es384, "ES256") is False


def test_resolve_auto_falls_back_to_es384_when_jwks_fails(caplog):
    settings = load_settings(OAUTH_ENV)
    with caplog.at_level(logging.WARNING, logger="arcane_mcp.server"):
        with httpx2.Client(transport=httpx2.MockTransport(lambda req: httpx2.Response(500))) as client:
            alg = resolve_oauth_jwt_algorithm(settings, jwks_client=client)
    assert alg == "ES384"
    assert any("auto-detect failed" in rec.message for rec in caplog.records)


def test_resolve_auto_reads_jwks_alg():
    pair = ECKeyPair()
    settings = load_settings(OAUTH_ENV)
    sync, _ = _jwks_clients({"keys": [pair.public_jwk]})
    with sync:
        assert resolve_oauth_jwt_algorithm(settings, jwks_client=sync) == "ES384"


def test_warns_when_jwks_keys_do_not_match_algorithm(caplog):
    pair = ECKeyPair()
    settings = load_settings({**OAUTH_ENV, "OAUTH_JWT_ALGORITHM": "RS256"})
    sync, _ = _jwks_clients({"keys": [pair.public_jwk]})
    with caplog.at_level(logging.WARNING, logger="arcane_mcp.server"):
        with sync:
            assert resolve_oauth_jwt_algorithm(settings, jwks_client=sync) == "RS256"
    assert any("none match algorithm RS256" in rec.message for rec in caplog.records)


def test_make_auth_provider_passes_es384_from_jwks():
    pair = ECKeyPair()
    settings = load_settings(OAUTH_ENV)
    sync, async_client = _jwks_clients({"keys": [pair.public_jwk]})
    with sync:
        auth = make_auth_provider(settings, jwks_client=sync, token_http_client=async_client)
    assert isinstance(auth, RemoteAuthProvider)
    assert isinstance(auth.token_verifier, JWTVerifier)
    assert auth.token_verifier.algorithm == "ES384"


def test_make_auth_provider_explicit_rs256():
    settings = load_settings({**OAUTH_ENV, "OAUTH_JWT_ALGORITHM": "RS256"})
    with httpx2.Client(transport=httpx2.MockTransport(lambda req: httpx2.Response(404))) as client:
        auth = make_auth_provider(settings, jwks_client=client)
    assert auth.token_verifier.algorithm == "RS256"


async def test_es384_jwks_initialize_and_tools_list(spec):
    pair = ECKeyPair()
    jwks = {"keys": [pair.public_jwk]}
    sync, async_client = _jwks_clients(jwks)
    settings = load_settings(OAUTH_ENV)
    with sync:
        auth = make_auth_provider(settings, jwks_client=sync, token_http_client=async_client)
    token = pair.create_token(
        subject="chatgpt-user",
        issuer=OAUTH_ENV["OAUTH_ISSUER_URL"],
        audience=OAUTH_ENV["OAUTH_AUDIENCE"],
        scopes=["mcp"],
    )
    mcp = build_server(settings, spec, mock_client([]), auth=auth)
    app = build_http_app(mcp, settings)
    async with asgi_client(app) as http:
        unauth = await http.post("/mcp", json=INIT, headers=MCP_HEADERS)
        assert unauth.status_code == 401

        init = await http.post(
            "/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"}
        )
        assert init.status_code == 200, init.text
        session = init.headers.get("mcp-session-id")
        assert session

        await http.post(
            "/mcp",
            json=INITIALIZED,
            headers={**MCP_HEADERS, "Authorization": f"Bearer {token}", "mcp-session-id": session},
        )
        listed = await http.post(
            "/mcp",
            json=TOOLS_LIST,
            headers={**MCP_HEADERS, "Authorization": f"Bearer {token}", "mcp-session-id": session},
        )
        assert listed.status_code == 200, listed.text
        body = listed.text
        assert "list_containers" in body or "tools" in body.lower()


async def test_rs256_public_key_still_works(spec):
    key_pair, auth = _oauth_http_auth()
    settings = load_settings({**OAUTH_ENV, "OAUTH_JWT_ALGORITHM": "RS256"})
    mcp = build_server(settings, spec, mock_client([]), auth=auth)
    token = key_pair.create_token(
        subject="chatgpt-user",
        issuer=OAUTH_ENV["OAUTH_ISSUER_URL"],
        audience=OAUTH_ENV["OAUTH_AUDIENCE"],
        scopes=["mcp"],
    )
    app = build_http_app(mcp, settings)
    async with asgi_client(app) as http:
        authed = await http.post(
            "/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"}
        )
        assert authed.status_code == 200
        session = authed.headers.get("mcp-session-id")
        listed = await http.post(
            "/mcp",
            json=TOOLS_LIST,
            headers={**MCP_HEADERS, "Authorization": f"Bearer {token}", "mcp-session-id": session or ""},
        )
        assert listed.status_code == 200
