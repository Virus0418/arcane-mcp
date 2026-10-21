# arcane-mcp

A Docker-hostable [MCP](https://modelcontextprotocol.io) server for [Arcane](https://getarcane.app).
It reads your Arcane instance's OpenAPI spec at startup and exposes the Docker-management API
(containers, images, volumes, networks, Compose projects, environments, system) as MCP tools over
Streamable HTTP. Because tools are generated from the live spec, they track your Arcane version automatically.

## Quick start

Prebuilt multi-arch images (amd64, arm64) are published to GitHub Container Registry on every push to `main`
and every `v*` tag: `ghcr.io/lukeeexd/arcane-mcp`. No clone or local build is needed.

```bash
mkdir arcane-mcp && cd arcane-mcp
curl -fsSLO https://raw.githubusercontent.com/lukeeexd/arcane-mcp/main/compose.yaml
curl -fsSL  https://raw.githubusercontent.com/lukeeexd/arcane-mcp/main/.env.example -o .env
# edit .env: set ARCANE_BASE_URL, ARCANE_API_KEY, MCP_AUTH_TOKEN
docker compose up -d
curl http://localhost:8000/health
```

Or without compose:

```bash
docker run -d --name arcane-mcp --restart unless-stopped -p 8000:8000 \
  -e ARCANE_BASE_URL=https://arcane.example.com \
  -e ARCANE_API_KEY=arc_xxx \
  -e MCP_AUTH_TOKEN=change-me \
  ghcr.io/lukeeexd/arcane-mcp:latest
```

Tags: `latest` tracks `main`; releases are tagged `1.2.3` and `1.2`; every build also gets `sha-<short>`.
To build locally instead, run `docker build -t arcane-mcp:local .` and point `image:` in `compose.yaml` at it.

Create the Arcane API key in Arcane under your profile, or via `POST /api/auth/me/api-keys`.
Generate `MCP_AUTH_TOKEN` with `openssl rand -hex 32`.

Without Docker:

```bash
uv sync
ARCANE_BASE_URL=... ARCANE_API_KEY=... MCP_AUTH_TOKEN=... uv run arcane-mcp
```

## Connecting a client

Claude Code:

```bash
claude mcp add --transport http arcane http://<host>:8000/mcp --header "Authorization: Bearer <MCP_AUTH_TOKEN>"
```

Any other MCP client: Streamable HTTP endpoint `http://<host>:8000/mcp`, header `Authorization: Bearer <MCP_AUTH_TOKEN>`.

ChatGPT and other OAuth MCP clients should use **oauth** mode (below) instead of a static bearer header.

## How it works

1. On boot the server fetches `ARCANE_BASE_URL/api/openapi.json`, retrying for `ARCANE_MCP_SPEC_TIMEOUT` seconds and exiting non-zero if Arcane never answers (Docker restarts it).
2. Operations whose tag is in `ARCANE_MCP_TAGS` become tools named after their `operationId` in snake_case (`list_containers`, `start_container`, `list_environments`, ...). Streaming and binary-download endpoints are always excluded.
3. Every tool call is proxied to Arcane with your `X-API-Key`.

Most tools take an environment `id`. The local Docker host is `"0"`; remote agents have UUIDs (`list_environments`).

## Safety

Destructive operations (any `DELETE`, plus paths ending in `prune`, `destroy`, `kill`, `down`, `restore`, `restore-files`)
do not execute on the first call. They return:

```json
{"requires_confirmation": true, "token": "3f9a...", "expires_in": 120, "operation": "delete_container", "arguments": {}}
```

The client must then call `confirm_operation(token)`. Tokens are single-use and expire after 120 seconds.
Disable with `ARCANE_MCP_CONFIRM_DESTRUCTIVE=false`; add patterns with `ARCANE_MCP_DESTRUCTIVE_PATTERNS`.

## Authentication

MCP ingress auth is separate from Arcane API auth (`ARCANE_BASE_URL` + `ARCANE_API_KEY`). Pick **one** mode per process with `MCP_AUTH_MODE` (alias `ARCANE_MCP_AUTH_MODE`):

- **`bearer`** (default): static token via FastMCP `StaticTokenVerifier`. Requires `MCP_AUTH_TOKEN`. Clients send `Authorization: Bearer <MCP_AUTH_TOKEN>`.
- **`oauth`**: JWT access tokens from Logto via FastMCP `JWTVerifier` + `RemoteAuthProvider`. Does **not** require `MCP_AUTH_TOKEN`. Unauthenticated `/mcp` returns **401** with `WWW-Authenticate` pointing at OAuth Protected Resource Metadata (PRM) so ChatGPT can discover Logto OIDC (CIMD).

Do not mix bearer and oauth on the same process or `/mcp` endpoint. Run two instances if you have both kinds of clients.

### OAuth (Logto / ChatGPT)

In Logto, create an **API Resource** whose indicator matches `OAUTH_AUDIENCE` (default `{MCP_PUBLIC_BASE_URL}/mcp`), enable **CIMD / dynamic apps** on the tenant, and issue access tokens for that resource with the `mcp` scope (or `OAUTH_SCOPES`). JWKS is `{OAUTH_ISSUER_URL}/jwks`. Logto signs access tokens with **ES384** (EC P-384); `OAUTH_JWT_ALGORITHM` defaults to `auto`, which reads that `alg` from JWKS at startup (fallback `ES384` if JWKS is unreachable). FastMCP's `JWTVerifier` otherwise defaults to RS256 and skips the EC key, which yields `401 invalid_token`.

```bash
docker run -d --name arcane-mcp --restart unless-stopped -p 8000:8000 \
  -e ARCANE_BASE_URL=https://arcane.example.com \
  -e ARCANE_API_KEY=arc_xxx \
  -e MCP_AUTH_MODE=oauth \
  -e MCP_PUBLIC_BASE_URL=https://mcp.example.com \
  -e OAUTH_ISSUER_URL=https://your-tenant.logto.app/oidc \
  -e OAUTH_AUDIENCE=https://mcp.example.com/mcp \
  -e OAUTH_SCOPES=mcp \
  ghcr.io/lukeeexd/arcane-mcp:latest
```

Point ChatGPT at `https://mcp.example.com/mcp`. It should receive 401 + PRM at `/.well-known/oauth-protected-resource/mcp`, then complete OIDC against Logto. `GET /health` stays unauthenticated in both modes.

Browser clients (ChatGPT) send a CORS preflight `OPTIONS /mcp`. In oauth mode the HTTP app allows `https://chatgpt.com`, `https://chat.openai.com`, and `https://www.chatgpt.com` by default via `MCP_HTTP_ALLOWED_ORIGINS`. That list is reflected in `Access-Control-Allow-Origin`; `*` is not the default. Bearer mode adds no CORS headers unless you set the same variable.

## Configuration

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ARCANE_BASE_URL` | yes | | Arcane URL without `/api` |
| `ARCANE_API_KEY` | yes | | Arcane API key (sent as `X-API-Key`; not MCP ingress) |
| `MCP_AUTH_MODE` | no | `bearer` | `bearer` or `oauth` (alias `ARCANE_MCP_AUTH_MODE`) |
| `MCP_AUTH_TOKEN` | bearer | | Static bearer token clients must present |
| `MCP_PUBLIC_BASE_URL` | oauth | | Public origin of this MCP (no `/mcp`). Alias `FASTMCP_SERVER_AUTH_BASE_URL` |
| `OAUTH_ISSUER_URL` | oauth | | Logto OIDC issuer ending in `/oidc` (`/oidc` is appended if missing) |
| `OAUTH_AUDIENCE` | oauth | `{MCP_PUBLIC_BASE_URL}/mcp` | JWT `aud` / Logto API Resource indicator |
| `OAUTH_SCOPES` | no | `mcp` | Required token scopes (comma-separated) |
| `OAUTH_JWKS_URI` | no | `{issuer}/jwks` | JWKS override |
| `OAUTH_JWT_ALGORITHM` | no | `auto` | JWT alg for `JWTVerifier` (`auto` reads JWKS, Logto fallback `ES384`). Alias `MCP_OAUTH_JWT_ALGORITHM` |
| `MCP_HTTP_ALLOWED_ORIGINS` | no | ChatGPT origins in oauth; empty in bearer | Comma-separated browser origins for CORS on `/mcp` (alias `ARCANE_MCP_HTTP_ALLOWED_ORIGINS`). Empty disables CORS. Prefer an explicit list over `*` |
| `MCP_HOST` | no | `0.0.0.0` | Bind address |
| `MCP_PORT` | no | `8000` | Bind port |
| `ARCANE_MCP_TAGS` | no | see `.env.example` | OpenAPI tags to expose |
| `ARCANE_MCP_EXCLUDE_TAGS` | no | | Tags to hide even if allowed |
| `ARCANE_MCP_READ_ONLY` | no | `false` | Expose only GET operations |
| `ARCANE_MCP_CONFIRM_DESTRUCTIVE` | no | `true` | Two-step confirmation gate |
| `ARCANE_MCP_DESTRUCTIVE_PATTERNS` | no | | Extra regexes matched against paths |
| `ARCANE_MCP_SPEC_TIMEOUT` | no | `60` | Seconds to retry the spec fetch |
| `ARCANE_MCP_LOG_LEVEL` | no | `INFO` | Log level |

With the default tags, Arcane 2.12 yields 137 generated tools plus `confirm_operation`.

Available tags in Arcane 2.12: Containers, Images, Volumes, Networks, Projects, Project Workspace, Environments, System,
Dashboard, Activities, Events, Ports, Updater, Image Updates, Vulnerabilities, Health, Version, Swarm, GitOps Syncs,
Templates, Volume Backup, Volume Workspace, Builds, Container Registries, Notifications, Webhooks, Customize, Users, Roles,
API Keys, Auth, OIDC, Passkeys, MFA, Settings, System Backups, S3 Destinations, Federated Credentials, Variables,
Diagnostics, JobSchedules, Jobs, Uploads, Mobile Push, Application Images, Stream.

## Security notes

Intended for a LAN or an internal Docker network. If you expose it publicly, put TLS in front of it,
keep the confirmation gate on, and consider `ARCANE_MCP_READ_ONLY=true` or a narrower tag list.
`GET /health` is unauthenticated and reports only the tool count and Arcane URL. Public oauth mode
needs a reachable `MCP_PUBLIC_BASE_URL` so clients can complete Logto discovery, and CORS origins
that match the browser client (`MCP_HTTP_ALLOWED_ORIGINS`) so ChatGPT can finish MCP action discovery
after OAuth.

## Development

```bash
uv sync --extra dev
uv run pytest -q
```

Tests run against a committed snapshot of the Arcane 2.12.0 spec in `tests/fixtures/openapi.json` with a mocked Arcane backend.

## Credits

Inspired by [MikeCase/arcane-mcp](https://github.com/MikeCase/arcane-mcp), a hand-curated stdio server. This project instead
generates tools from the OpenAPI spec and adds HTTP transport, auth and Docker packaging. MIT licensed.
