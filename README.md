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
2. Operations whose tag is in `ARCANE_MCP_TAGS` become tools named after their `operationId` in snake_case (`list_containers`, `container_action`, `list_environments`, ...). Paths matching `/stream`, `/logs/download`, `/export`, `/download`, `/attestations`, `/upload`, or `/app-images/` are always excluded. A success response of `text/event-stream` (or another unsupported stream such as `application/x-ndjson`) is excluded too, even when the path does not contain `/stream`. That drops Arcane 3 `stream_upgrade_progress` (`GET /agents/{id}/system/upgrade/progress`). JSON tools stay.
3. Every tool call is proxied to Arcane with your `X-API-Key`.

Most tools take an environment `id`. The local Docker host is `"0"`; remote agents have UUIDs (`list_environments`).

`get_container_logs` is hand-written rather than generated. See [Container logs](#container-logs).

## Container logs

`get_container_logs` reads the container log download named in the loaded spec (permission `containers:logs`) and returns a tail of the text file. Arcane 2.x is `GET /api/environments/{id}/containers/{containerId}/logs/download`. Arcane 3.x is `GET /api/agents/{id}/containers/{containerId}/logs/download`. The operation id is still `download-container-logs`. If a spec contains both templates, major version 3 selects `/agents` and every other version keeps `/environments`, so an Arcane 2.17 server is not pointed at `/agents`. The route has no `tail` query. It stays matched by the OpenAPI hard-exclude list, so auto-generation never registers `download_container_logs` and never streams the attachment into the client.

The tool streams the body and keeps a ring of the most recent lines. Older lines are dropped as soon as `tail` or `max_bytes` is exceeded, so the whole file is not buffered and then sliced. Invalid UTF-8 bytes are replaced. The Arcane request uses a 30 second HTTP timeout.

| Argument | Default | Limit |
|---|---|---|
| `id` | | Environment id (`"0"` local, UUID for an edge agent) |
| `containerId` | | Container id or name |
| `tail` | `200` | Clamped to 1..2000 lines |
| `max_bytes` | `131072` (128 KiB) | Hard cap `524288` (512 KiB) |

The result is JSON: `success`, `truncated`, `lines`, `bytes`, `content`, and `note` when output was truncated. `truncated` is true when older lines were dropped to satisfy `tail` or `max_bytes`. The tool is read-only and is not behind `confirm_operation`. Set `ARCANE_MCP_ENABLE_LOGS=false` to skip registering it.

## Safety

Destructive operations do not execute on the first call. That is any `DELETE`, any path ending in `prune`, `destroy`, `kill`, `down`, `restore`, or `restore-files`, and any call whose `action` argument is `kill`, `down`, `restore`, `restorefiles`, or `restore-files` (comparison ignores case and surrounding space). Arcane 3 uses the action form for container `kill`, project `down`, and non-default backup `restore` / `restorefiles`. `start`, `stop`, `restart`, `pause`, `unpause`, and deploy `up` / `redeploy` / `pull` are not gated.

The token stores the tool name and the exact arguments. `confirm_operation` replays that request and has no `action` parameter, so a kill token cannot be turned into start or down. A different action needs its own token. The gate only does this for tools the route map actually exposed. `ARCANE_MCP_READ_ONLY=true` drops non-GET operations, and calling `container_action` with `action=kill` on that server is an unknown tool rather than a confirmation prompt that could be replayed. They return:

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
| `ARCANE_MCP_ENABLE_LOGS` | no | `true` | Register hand-written `get_container_logs` (read-only tail; does not un-exclude `/logs/download`) |
| `ARCANE_MCP_DESTRUCTIVE_PATTERNS` | no | | Extra regexes matched against paths |
| `ARCANE_MCP_SPEC_TIMEOUT` | no | `60` | Seconds to retry the spec fetch |
| `ARCANE_MCP_LOG_LEVEL` | no | `INFO` | Log level |

With the default tags, Arcane 2.12 yields 137 generated tools plus `confirm_operation` and `get_container_logs` (139). Arcane 3.0.1 yields 131 tools with the same settings: 129 generated tools, plus the same two hand-written tools. A pre-fix load of that spec produced 132 because it still exposed `stream_upgrade_progress`; the media-type filter removes that one SSE tool and does not remove the JSON tools.

## Arcane 3.0

Tool names and arguments follow the live spec. There is no compatibility alias layer. Restarting against Arcane 3 is enough for path and method changes that FastMCP generates. The hand-written log path, the action confirmation gate, and the event-stream filter are in this server so a 2.x spec and a 3.x spec can both be served without a config change.

`deploy_project` is `POST /agents/{id}/projects/{projectId}/deploy` and **`action` is required**: `up`, `redeploy`, or `pull`. `up` is the old deploy. `redeploy` replaces `redeploy_project`. `pull` replaces `pull_project_images`.

These four tools take the environment id as `id__path`. FastMCP renames the path parameter because the JSON body also has `id`. Passing the old `id` argument writes it into the body and leaves `{id}` in the URL. This server does not wrap or rename them.

| Tool | `id__path` | body `id` |
|---|---|---|
| `update_project` | environment id | project id |
| `update_environment` | environment id | environment id in the body |
| `create_volume` | environment id | volume id |
| `ignore_vulnerability` | environment id | ignored-vulnerability id |

### Tools that disappear on Arcane 3

Twenty-nine default tools from the 2.x surface are gone. Twenty-four have a v3 successor. Five do not, and this server does not invent a stand-in for them.

| Arcane 2 tool | Arcane 3 call |
|---|---|
| `start_container` | `container_action` `action=start` |
| `stop_container` | `container_action` `action=stop` |
| `restart_container` | `container_action` `action=restart` |
| `kill_container` | `container_action` `action=kill` (confirmation required) |
| `pause_container` | `container_action` `action=pause` |
| `unpause_container` | `container_action` `action=unpause` |
| `down_project` | `project_action` `action=down` (confirmation required) |
| `restart_project` | `project_action` `action=restart` |
| `archive_project` | `project_action` `action=archive` |
| `unarchive_project` | `project_action` `action=unarchive` |
| `update_project_services` | `project_action` `action=updateservices` |
| `pull_project_images` | `deploy_project` `action=pull` |
| `redeploy_project` | `deploy_project` `action=redeploy` |
| `connect_network_container` | `network_container_action` `action=connect` |
| `disconnect_network_container` | `network_container_action` `action=disconnect` |
| `cancel_activity` | `activity_action` `action=cancel` |
| `check_all_images` | `check_images` `action=all` |
| `check_multiple_images` | `check_images` `action=batch` |
| `start_all_containers` | `system_containers_action` `action=startall` |
| `start_all_stopped_containers` | `system_containers_action` `action=startstopped` |
| `stop_all_containers` | `system_containers_action` `action=stopall` |
| `bulk_ignore_vulnerabilities` | `bulk_ignored_vulnerabilities_action` `action=ignore` |
| `bulk_unignore_vulnerabilities` | `bulk_ignored_vulnerabilities_action` `action=unignore` |
| `download_edge_mtlsca` | `download_agent_mtlsca` |
| `get_dashboard` | removed, no equivalent |
| `pair_agent` | removed, no equivalent (`enroll_agent` is tag Enrollment and is not a replacement) |
| `update_heartbeat` | removed, no equivalent |
| `trigger_update_all` | removed, no equivalent (`start_upgrade_job` is a different contract) |
| `update_all_status` | removed, no equivalent (`get_current_upgrade_job` is a different contract) |

`list_environments` keeps its name. Its URL becomes `GET /agents`. `get_container_logs` keeps its name and selects `/agents` from a 3.x spec.

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

Tests run against committed OpenAPI snapshots with a mocked Arcane backend (`httpx2.MockTransport`). `tests/fixtures/openapi.json` is Arcane 2.12.0. `tests/fixtures/openapi-v3.0.1.json` is the official Arcane 3.0.1 export from `getarcaneapp/arcane` release `v3.0.1` (`c614e0d2df6d6bcb6188c1af249f457000c7e96c`), command `arcane openapi -f json` on `arcane_linux_amd64`. SHA-256 `a2381f0425261e73b0a5d87b30d3eee12d62e27a8713de6edbddbd9b49d61013`. It is not a hand-written document.

## Credits

Inspired by [MikeCase/arcane-mcp](https://github.com/MikeCase/arcane-mcp), a hand-curated stdio server. This project instead
generates tools from the OpenAPI spec and adds HTTP transport, auth and Docker packaging. MIT licensed.
