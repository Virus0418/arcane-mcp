"""Environment-variable configuration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

DEFAULT_TAGS: frozenset[str] = frozenset({
    "Containers", "Images", "Volumes", "Networks", "Projects", "Project Workspace",
    "Environments", "System", "Dashboard", "Activities", "Events", "Ports", "Updater",
    "Image Updates", "Vulnerabilities", "Health", "Version",
})

DEFAULT_OAUTH_SCOPES: tuple[str, ...] = ("mcp",)

DEFAULT_OAUTH_HTTP_ALLOWED_ORIGINS: tuple[str, ...] = (
    "https://chatgpt.com",
    "https://chat.openai.com",
    "https://www.chatgpt.com",
)

AuthMode = Literal["bearer", "oauth"]

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(ValueError):
    """Raised when required configuration is missing or malformed."""


@dataclass(frozen=True)
class Settings:
    arcane_base_url: str
    arcane_api_key: str
    mcp_auth_mode: AuthMode = "bearer"
    mcp_auth_token: str | None = None
    mcp_public_base_url: str | None = None
    oauth_issuer_url: str | None = None
    oauth_audience: str | None = None
    oauth_jwks_uri: str | None = None
    oauth_scopes: tuple[str, ...] = DEFAULT_OAUTH_SCOPES
    mcp_http_allowed_origins: tuple[str, ...] = ()
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8000
    tags: frozenset[str] = DEFAULT_TAGS
    exclude_tags: frozenset[str] = frozenset()
    read_only: bool = False
    confirm_destructive: bool = True
    destructive_patterns: tuple[str, ...] = ()
    spec_timeout: float = 60.0
    log_level: str = "INFO"


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"Required environment variable {name} is not set")
    return value


def _env(env: Mapping[str, str], *names: str) -> str:
    for name in names:
        value = env.get(name, "").strip()
        if value:
            return value
    return ""


def _required_any(env: Mapping[str, str], *names: str) -> str:
    value = _env(env, *names)
    if not value:
        raise ConfigError(f"Required environment variable {names[0]} is not set")
    return value


def _csv(env: Mapping[str, str], name: str) -> tuple[str, ...]:
    raw = env.get(name, "")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigError(f"{name} must be a boolean (true/false), got {raw!r}")


def _number(env: Mapping[str, str], name: str, default: float, cast: type) -> float | int:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return cast(default)
    try:
        return cast(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _base_url(raw: str) -> str:
    url = raw.strip().rstrip("/")
    if url.endswith("/api"):
        url = url[: -len("/api")]
    return url


def _http_url(raw: str, name: str, *, strip_slash: bool = True) -> str:
    url = raw.strip()
    if strip_slash:
        url = url.rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(f"{name} must be an http(s) URL, got {raw!r}")
    return url


def _auth_mode(env: Mapping[str, str]) -> AuthMode:
    raw = _env(env, "MCP_AUTH_MODE", "ARCANE_MCP_AUTH_MODE").lower() or "bearer"
    if raw not in {"bearer", "oauth"}:
        raise ConfigError(f"MCP_AUTH_MODE must be 'bearer' or 'oauth', got {raw!r}")
    return raw  # type: ignore[return-value]


def _oauth_issuer_url(raw: str, name: str) -> str:
    url = _http_url(raw, name)
    if not url.endswith("/oidc"):
        url = f"{url}/oidc"
    return url


def _origin(raw: str, name: str) -> str:
    origin = raw.strip().rstrip("/")
    if origin == "*":
        return origin
    parsed = urlparse(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError(f"{name} entries must be http(s) origins (or '*'), got {raw!r}")
    return origin


def _http_allowed_origins(env: Mapping[str, str], mode: AuthMode) -> tuple[str, ...]:
    for name in ("MCP_HTTP_ALLOWED_ORIGINS", "ARCANE_MCP_HTTP_ALLOWED_ORIGINS"):
        if name in env:
            return tuple(_origin(part, name) for part in _csv(env, name))
    if mode == "oauth":
        return DEFAULT_OAUTH_HTTP_ALLOWED_ORIGINS
    return ()


def _oauth_scopes(env: Mapping[str, str]) -> tuple[str, ...]:
    for name in ("OAUTH_SCOPES", "MCP_OAUTH_SCOPES"):
        if name in env:
            return _csv(env, name)
    return DEFAULT_OAUTH_SCOPES


def _load_oauth_settings(env: Mapping[str, str]) -> dict[str, object]:
    public_base = _http_url(
        _required_any(env, "MCP_PUBLIC_BASE_URL", "FASTMCP_SERVER_AUTH_BASE_URL"),
        "MCP_PUBLIC_BASE_URL",
    )
    issuer = _oauth_issuer_url(
        _required_any(env, "OAUTH_ISSUER_URL", "MCP_OAUTH_ISSUER_URL"),
        "OAUTH_ISSUER_URL",
    )
    audience_raw = _env(env, "OAUTH_AUDIENCE", "MCP_OAUTH_AUDIENCE")
    audience = (
        _http_url(audience_raw, "OAUTH_AUDIENCE", strip_slash=False)
        if audience_raw
        else f"{public_base}/mcp"
    )
    jwks_raw = _env(env, "OAUTH_JWKS_URI", "MCP_OAUTH_JWKS_URI")
    jwks_uri = _http_url(jwks_raw, "OAUTH_JWKS_URI") if jwks_raw else f"{issuer}/jwks"
    return {
        "mcp_auth_token": None,
        "mcp_public_base_url": public_base,
        "oauth_issuer_url": issuer,
        "oauth_audience": audience,
        "oauth_jwks_uri": jwks_uri,
        "oauth_scopes": _oauth_scopes(env),
    }


def load_settings(env: Mapping[str, str]) -> Settings:
    tags = _csv(env, "ARCANE_MCP_TAGS")
    mode = _auth_mode(env)
    if mode == "oauth":
        auth_fields = _load_oauth_settings(env)
    else:
        auth_fields = {
            "mcp_auth_token": _required(env, "MCP_AUTH_TOKEN"),
            "mcp_public_base_url": None,
            "oauth_issuer_url": None,
            "oauth_audience": None,
            "oauth_jwks_uri": None,
            "oauth_scopes": DEFAULT_OAUTH_SCOPES,
        }
    return Settings(
        arcane_base_url=_base_url(_required(env, "ARCANE_BASE_URL")),
        arcane_api_key=_required(env, "ARCANE_API_KEY"),
        mcp_auth_mode=mode,
        mcp_host=env.get("MCP_HOST", "").strip() or "0.0.0.0",
        mcp_port=int(_number(env, "MCP_PORT", 8000, int)),
        tags=frozenset(tags) if tags else DEFAULT_TAGS,
        exclude_tags=frozenset(_csv(env, "ARCANE_MCP_EXCLUDE_TAGS")),
        read_only=_bool(env, "ARCANE_MCP_READ_ONLY", False),
        confirm_destructive=_bool(env, "ARCANE_MCP_CONFIRM_DESTRUCTIVE", True),
        destructive_patterns=_csv(env, "ARCANE_MCP_DESTRUCTIVE_PATTERNS"),
        spec_timeout=float(_number(env, "ARCANE_MCP_SPEC_TIMEOUT", 60.0, float)),
        log_level=(env.get("ARCANE_MCP_LOG_LEVEL", "").strip() or "INFO").upper(),
        mcp_http_allowed_origins=_http_allowed_origins(env, mode),
        **auth_fields,
    )
