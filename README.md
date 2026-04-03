# Dynamic MCP Server for Nextcloud

This server exposes a live Nextcloud instance as an MCP server - flexibile for all apps installed.

Instead of shipping a fixed tool list, it queries the Nextcloud `ocs_api_viewer` app at startup, reads the OpenAPI descriptions for installed apps, and turns those operations into MCP tools dynamically. The result is an MCP endpoint that reflects the APIs available on the connected Nextcloud instance.

## What The Server Does

- Connects to a Nextcloud instance defined by `NEXTCLOUD_URL`
- Uses server discovery credentials at startup and per-request credentials for tool execution
- Reads installed app APIs from `NEXTCLOUD_URL/apps/ocs_api_viewer`
- Creates MCP tools dynamically from the discovered OpenAPI operations
- Proxies tool calls back to the real Nextcloud REST endpoints
- Supports both `streamable-http` and `stdio` MCP transports

One built-in tool is always available:

- `nextcloud_discovery_status`: returns the connected Nextcloud URL, auth mode, discovered apps, tool count, and last refresh/error state

Dynamic tools are named from the Nextcloud app id plus the OpenAPI operation id or path, for example:

```text
files_sharing_get_shares
provisioning_api_create_user
dav_upcoming_events_get_events
```

## Main Service Endpoints

### `GET /`

Health and discovery endpoint. Returns:

- server name
- transport mode
- MCP path
- whether default credentials are configured
- current discovery status

Example:

```bash
curl http://localhost:8000/
```

### `/mcp`

Main MCP endpoint for `streamable-http` clients.

Point Codex, Claude Code, or any other MCP client at:

```text
http://localhost:8000/mcp
```

## Requirements

- Docker and Docker Compose
- A reachable Nextcloud instance
- The Nextcloud `ocs_api_viewer` app enabled on that instance
- A Nextcloud username and app token with permission to access the APIs you want to expose

## Configuration

The server is configured entirely with environment variables.

| Variable | Default | Description |
|---|---|---|
| `NEXTCLOUD_URL` | `http://nc31-app-1:80` | Base URL of the target Nextcloud instance |
| `NEXTCLOUD_USERNAME` | unset | Server-side username used only for startup discovery |
| `NEXTCLOUD_APP_TOKEN` | unset | Server-side app token used only for startup discovery |
| `MCP_HOST` | `0.0.0.0` | Bind host for HTTP mode |
| `MCP_PORT` | `8000` | Bind port for HTTP mode |
| `MCP_TRANSPORT` | `streamable-http` | `streamable-http` or `stdio` |
| `DISCOVERY_TIMEOUT_SECONDS` | `30` | Timeout for discovery and proxied requests |
| `LOG_LEVEL` | `INFO` | Python log level |
| `DEBUG` | unset | Set to `true` to enable Starlette debug mode |

### Authentication Modes

The server supports two auth patterns:

1. Server-level discovery auth via `NEXTCLOUD_USERNAME` and `NEXTCLOUD_APP_TOKEN`
2. Per-request auth via MCP request headers:
   - `X-Nextcloud-Username`
   - `X-Nextcloud-AppToken`

The server-level credentials are used only during startup discovery. Every actual tool call must provide the request headers, and the server does not fall back to the startup admin credentials for execution.

## How To Start The Server

Update docker-compose.yml with your Nextcloud URL and credentials, then run:

```bash
docker compose up --build
```

The server will be available at:

```text
http://localhost:8000/
http://localhost:8000/mcp
```

## How Discovery Works

At startup, the server:

1. Calls `GET /apps/ocs_api_viewer/apps` on the configured Nextcloud instance
2. Loads each app’s OpenAPI document from `GET /apps/ocs_api_viewer/apps/{appId}`
3. Builds an MCP input schema from the operation parameters and request body
4. Registers the operation as a callable MCP tool

Discovery uses the server's default `NEXTCLOUD_USERNAME` and `NEXTCLOUD_APP_TOKEN`.

Every tool execution uses only `X-Nextcloud-Username` and `X-Nextcloud-AppToken`. If those headers are missing, the tool call is rejected instead of falling back to the startup admin account.

If discovery fails, the server still starts and reports the error through `nextcloud_discovery_status` and `GET /`.

## Client Configuration Examples

### Codex

Codex can pass the Nextcloud credentials as HTTP headers:

```bash
codex mcp add nextcloud-live --url http://localhost:8000/mcp
```

Equivalent `~/.codex/config.toml` example:

```toml
[mcp_servers.nextcloud-live]
url = "http://localhost:8000/mcp"
http_headers = { X-Nextcloud-Username = "NEXTCLOUD_USERNAME", X-Nextcloud-AppToken = "NEXTCLOUD_APP_TOKEN" }
```

### Claude Code

CLI example:

```bash
claude mcp add --transport http nextcloud-live http://localhost:8000/mcp
```

Project-scoped `.mcp.json` example using per-user credentials from environment variables:

```json
{
  "mcpServers": {
    "nextcloud-live": {
      "type": "http",
      "url": "http://localhost:8000/mcp",
      "headers": {
        "X-Nextcloud-Username": "${NEXTCLOUD_USERNAME}",
        "X-Nextcloud-AppToken": "${NEXTCLOUD_APP_TOKEN}"
      }
    }
  }
}
```

This is useful when you want one shared MCP server URL but each developer should authenticate to Nextcloud with their own account.

## Example Smoke Checks

Check the HTTP health endpoint:

```bash
curl http://localhost:8000/
```

If you are using Docker Compose:

```bash
docker compose logs -f mcp
```

In an MCP client, call:

- `nextcloud_discovery_status`

Then verify that the discovered tool list includes operations from your enabled Nextcloud apps.
