import pytest

from arcane_mcp.config import DEFAULT_OAUTH_SCOPES, DEFAULT_TAGS, ConfigError, load_settings

REQUIRED = {
    "ARCANE_BASE_URL": "https://arcane.example.com/",
    "ARCANE_API_KEY": "arc_key",
    "MCP_AUTH_TOKEN": "secret",
}

OAUTH_REQUIRED = {
    "ARCANE_BASE_URL": "https://arcane.example.com/",
    "ARCANE_API_KEY": "arc_key",
    "MCP_AUTH_MODE": "oauth",
    "MCP_PUBLIC_BASE_URL": "https://mcp.example.com",
    "OAUTH_ISSUER_URL": "https://tenant.logto.app/oidc",
}


def test_defaults():
    s = load_settings(REQUIRED)
    assert s.arcane_base_url == "https://arcane.example.com"
    assert s.arcane_api_key == "arc_key"
    assert s.mcp_auth_mode == "bearer"
    assert s.mcp_auth_token == "secret"
    assert s.mcp_public_base_url is None
    assert s.oauth_issuer_url is None
    assert s.mcp_host == "0.0.0.0"
    assert s.mcp_port == 8000
    assert s.tags == DEFAULT_TAGS
    assert "Containers" in DEFAULT_TAGS and "Users" not in DEFAULT_TAGS
    assert s.exclude_tags == frozenset()
    assert s.read_only is False
    assert s.confirm_destructive is True
    assert s.destructive_patterns == ()
    assert s.spec_timeout == 60.0
    assert s.log_level == "INFO"


def test_strips_trailing_api_segment():
    s = load_settings({**REQUIRED, "ARCANE_BASE_URL": "http://h:3552/api/"})
    assert s.arcane_base_url == "http://h:3552"


@pytest.mark.parametrize("missing", sorted(REQUIRED))
def test_missing_required_names_variable(missing):
    env = {k: v for k, v in REQUIRED.items() if k != missing}
    with pytest.raises(ConfigError) as exc:
        load_settings(env)
    assert missing in str(exc.value)


def test_blank_required_is_missing():
    with pytest.raises(ConfigError):
        load_settings({**REQUIRED, "ARCANE_API_KEY": "  "})


def test_list_and_bool_parsing():
    s = load_settings({
        **REQUIRED,
        "ARCANE_MCP_TAGS": " Containers, Swarm ,,Images ",
        "ARCANE_MCP_EXCLUDE_TAGS": "Images",
        "ARCANE_MCP_READ_ONLY": "TRUE",
        "ARCANE_MCP_CONFIRM_DESTRUCTIVE": "0",
        "ARCANE_MCP_DESTRUCTIVE_PATTERNS": r"/reset$, /wipe",
        "ARCANE_MCP_SPEC_TIMEOUT": "5",
        "MCP_PORT": "9000",
        "MCP_HOST": "127.0.0.1",
        "ARCANE_MCP_LOG_LEVEL": "debug",
    })
    assert s.tags == frozenset({"Containers", "Swarm", "Images"})
    assert s.exclude_tags == frozenset({"Images"})
    assert s.read_only is True
    assert s.confirm_destructive is False
    assert s.destructive_patterns == (r"/reset$", "/wipe")
    assert s.spec_timeout == 5.0
    assert s.mcp_port == 9000
    assert s.mcp_host == "127.0.0.1"
    assert s.log_level == "DEBUG"


@pytest.mark.parametrize("value", ["yes", "true", "1", "on", "True"])
def test_truthy_values(value):
    assert load_settings({**REQUIRED, "ARCANE_MCP_READ_ONLY": value}).read_only is True


def test_bad_bool_raises():
    with pytest.raises(ConfigError):
        load_settings({**REQUIRED, "ARCANE_MCP_READ_ONLY": "maybe"})


def test_bad_port_raises():
    with pytest.raises(ConfigError):
        load_settings({**REQUIRED, "MCP_PORT": "abc"})


def test_invalid_auth_mode_raises():
    with pytest.raises(ConfigError, match="bearer"):
        load_settings({**REQUIRED, "MCP_AUTH_MODE": "both"})


def test_oauth_does_not_require_mcp_auth_token():
    s = load_settings(OAUTH_REQUIRED)
    assert s.mcp_auth_mode == "oauth"
    assert s.mcp_auth_token is None
    assert s.mcp_public_base_url == "https://mcp.example.com"
    assert s.oauth_issuer_url == "https://tenant.logto.app/oidc"
    assert s.oauth_audience == "https://mcp.example.com/mcp"
    assert s.oauth_jwks_uri == "https://tenant.logto.app/oidc/jwks"
    assert s.oauth_scopes == DEFAULT_OAUTH_SCOPES


def test_oauth_ignores_unused_bearer_token():
    s = load_settings({**OAUTH_REQUIRED, "MCP_AUTH_TOKEN": "ignored"})
    assert s.mcp_auth_token is None


def test_oauth_alias_env_names():
    s = load_settings({
        "ARCANE_BASE_URL": "https://arcane.example.com",
        "ARCANE_API_KEY": "arc_key",
        "ARCANE_MCP_AUTH_MODE": "oauth",
        "FASTMCP_SERVER_AUTH_BASE_URL": "https://mcp.example.com/",
        "MCP_OAUTH_ISSUER_URL": "https://tenant.logto.app",
        "MCP_OAUTH_AUDIENCE": "https://mcp.example.com/mcp/",
        "MCP_OAUTH_SCOPES": "mcp, offline_access",
        "MCP_OAUTH_JWKS_URI": "https://tenant.logto.app/oidc/jwks",
    })
    assert s.mcp_auth_mode == "oauth"
    assert s.mcp_public_base_url == "https://mcp.example.com"
    assert s.oauth_issuer_url == "https://tenant.logto.app/oidc"
    assert s.oauth_audience == "https://mcp.example.com/mcp/"
    assert s.oauth_scopes == ("mcp", "offline_access")
    assert s.oauth_jwks_uri == "https://tenant.logto.app/oidc/jwks"


def test_oauth_appends_oidc_suffix():
    s = load_settings({**OAUTH_REQUIRED, "OAUTH_ISSUER_URL": "https://tenant.logto.app/"})
    assert s.oauth_issuer_url == "https://tenant.logto.app/oidc"
    assert s.oauth_jwks_uri == "https://tenant.logto.app/oidc/jwks"


def test_oauth_explicit_audience_and_scopes():
    s = load_settings({
        **OAUTH_REQUIRED,
        "OAUTH_AUDIENCE": "https://mcp.example.com/",
        "OAUTH_SCOPES": "mcp",
        "OAUTH_JWKS_URI": "https://tenant.logto.app/oidc/jwks.json",
    })
    assert s.oauth_audience == "https://mcp.example.com/"
    assert s.oauth_scopes == ("mcp",)
    assert s.oauth_jwks_uri == "https://tenant.logto.app/oidc/jwks.json"


def test_oauth_empty_scopes_disables_required_scopes():
    s = load_settings({**OAUTH_REQUIRED, "OAUTH_SCOPES": ""})
    assert s.oauth_scopes == ()


@pytest.mark.parametrize("missing", ["MCP_PUBLIC_BASE_URL", "OAUTH_ISSUER_URL"])
def test_oauth_missing_required(missing):
    env = {k: v for k, v in OAUTH_REQUIRED.items() if k != missing}
    with pytest.raises(ConfigError) as exc:
        load_settings(env)
    assert missing in str(exc.value)


def test_oauth_rejects_non_url():
    with pytest.raises(ConfigError, match="http"):
        load_settings({**OAUTH_REQUIRED, "MCP_PUBLIC_BASE_URL": "mcp.example.com"})


def test_bearer_still_requires_token_when_mode_set():
    env = {k: v for k, v in REQUIRED.items() if k != "MCP_AUTH_TOKEN"}
    with pytest.raises(ConfigError, match="MCP_AUTH_TOKEN"):
        load_settings({**env, "MCP_AUTH_MODE": "bearer"})
