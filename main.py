import asyncio
import base64
import contextlib
import copy
import contextvars
import hashlib
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx
import mcp.server.stdio
import uvicorn
from mcp import types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.types import Receive, Scope, Send


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("nextcloud-mcp")


NEXTCLOUD_URL = os.getenv("NEXTCLOUD_URL", "http://nc31-app-1:80").rstrip("/")
NEXTCLOUD_USERNAME = os.getenv("NEXTCLOUD_USERNAME")
NEXTCLOUD_APP_TOKEN = os.getenv("NEXTCLOUD_APP_TOKEN")
API_VIEWER_URL = f"{NEXTCLOUD_URL}/apps/ocs_api_viewer"

MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "streamable-http")
DISCOVERY_TIMEOUT_SECONDS = float(os.getenv("DISCOVERY_TIMEOUT_SECONDS", "30"))

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
STATUS_TOOL_NAME = "nextcloud_discovery_status"
INTERNAL_HEADER_PARAMS = {"ocs-apirequest"}
SERVER_NAME = "nextcloud-live-instance-mcp"
NEXTCLOUD_USERNAME_HEADER = "x-nextcloud-username"
NEXTCLOUD_APP_TOKEN_HEADER = "x-nextcloud-apptoken"


@dataclass(slots=True)
class OperationDefinition:
    name: str
    app_id: str
    app_name: str
    method: str
    path: str
    summary: str
    description: str
    input_schema: dict[str, Any]
    path_params: list[str]
    query_params: list[str]
    header_params: list[str]
    body_mode: str
    body_fields: list[str]
    body_content_type: str | None


@dataclass(slots=True)
class DiscoveryState:
    operations: dict[str, OperationDefinition] = field(default_factory=dict)
    apps: list[dict[str, Any]] = field(default_factory=list)
    last_refresh: str | None = None
    last_error: str | None = None


@dataclass(slots=True)
class AuthContext:
    auth_header: str | None
    source: str
    cache_key: str


REQUEST_HEADERS: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "request_headers",
    default={},
)
DISCOVERY_STATE = DiscoveryState()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def unique_names(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def normalize_tool_name(app_id: str, operation_id: str | None, method: str, path: str) -> str:
    raw = operation_id or f"{method}_{path}"
    normalized = "".join(ch if ch.isalnum() else "_" for ch in f"{app_id}_{raw}")
    normalized = "_".join(part for part in normalized.lower().split("_") if part)
    return normalized or f"{app_id}_{method}"


def clone_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    return copy.deepcopy(schema or {})


def enrich_schema(schema: dict[str, Any], description: str | None) -> dict[str, Any]:
    enriched = clone_schema(schema)
    if description and "description" not in enriched:
        enriched["description"] = description
    return enriched


def merge_parameters(
    path_parameters: list[dict[str, Any]],
    operation_parameters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for parameter in path_parameters + operation_parameters:
        location = parameter.get("in")
        name = parameter.get("name")
        if not location or not name:
            continue
        merged[(location, name)] = parameter
    return list(merged.values())


def preferred_content_type(content: dict[str, Any]) -> str | None:
    if not content:
        return None
    candidates = [
        "application/json",
        "application/merge-patch+json",
        "application/x-www-form-urlencoded",
        "multipart/form-data",
    ]
    for candidate in candidates:
        if candidate in content:
            return candidate
    for content_type in content:
        if "json" in content_type:
            return content_type
    return next(iter(content), None)


def build_input_schema(
    parameters: list[dict[str, Any]],
    request_body: dict[str, Any] | None,
) -> tuple[dict[str, Any], str, list[str], str | None, list[str], list[str], list[str]]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    path_params: list[str] = []
    query_params: list[str] = []
    header_params: list[str] = []

    for parameter in parameters:
        name = parameter["name"]
        location = parameter["in"]
        if location == "header" and name.lower() in INTERNAL_HEADER_PARAMS:
            continue
        schema = enrich_schema(parameter.get("schema", {}), parameter.get("description"))
        properties[name] = schema
        if parameter.get("required"):
            required.append(name)
        if location == "path":
            path_params.append(name)
        elif location == "query":
            query_params.append(name)
        elif location == "header":
            header_params.append(name)

    body_mode = "none"
    body_fields: list[str] = []
    body_content_type: str | None = None

    if request_body:
        content = request_body.get("content", {})
        body_content_type = preferred_content_type(content)
        body_schema = clone_schema(content.get(body_content_type, {}).get("schema", {}))

        can_flatten = (
            body_content_type is not None
            and "json" in body_content_type
            and body_schema.get("type") == "object"
            and isinstance(body_schema.get("properties"), dict)
            and not body_schema.get("additionalProperties")
        )

        body_property_names = list(body_schema.get("properties", {}).keys())
        has_conflicts = any(name in properties for name in body_property_names)

        if can_flatten and body_property_names and not has_conflicts:
            body_mode = "flattened_json_object"
            for field_name, field_schema in body_schema.get("properties", {}).items():
                properties[field_name] = field_schema
            body_fields = body_property_names
            required.extend(body_schema.get("required", []))
        else:
            body_mode = "body"
            body_fields = ["body"]
            body_property = enrich_schema(body_schema or {"type": "object"}, request_body.get("description"))
            properties["body"] = body_property
            if request_body.get("required"):
                required.append("body")

    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": unique_names(required),
        "additionalProperties": False,
    }
    return (
        input_schema,
        body_mode,
        body_fields,
        body_content_type,
        path_params,
        query_params,
        header_params,
    )


def make_operation_definition(
    app_meta: dict[str, Any],
    path: str,
    method: str,
    operation: dict[str, Any],
    inherited_parameters: list[dict[str, Any]],
) -> OperationDefinition:
    parameters = merge_parameters(inherited_parameters, operation.get("parameters", []))
    (
        input_schema,
        body_mode,
        body_fields,
        body_content_type,
        path_params,
        query_params,
        header_params,
    ) = build_input_schema(parameters, operation.get("requestBody"))

    summary = operation.get("summary") or f"{method.upper()} {path}"
    description = operation.get("description") or summary

    return OperationDefinition(
        name=normalize_tool_name(app_meta["id"], operation.get("operationId"), method, path),
        app_id=app_meta["id"],
        app_name=app_meta.get("name", app_meta["id"]),
        method=method.upper(),
        path=path,
        summary=summary,
        description=description,
        input_schema=input_schema,
        path_params=path_params,
        query_params=query_params,
        header_params=header_params,
        body_mode=body_mode,
        body_fields=body_fields,
        body_content_type=body_content_type,
    )


def encode_basic_auth(username: str, app_token: str) -> str:
    token = base64.b64encode(f"{username}:{app_token}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def hash_auth_value(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def default_auth_context() -> AuthContext:
    if NEXTCLOUD_USERNAME and NEXTCLOUD_APP_TOKEN:
        auth_header = encode_basic_auth(NEXTCLOUD_USERNAME, NEXTCLOUD_APP_TOKEN)
        return AuthContext(
            auth_header=auth_header,
            source="env_basic",
            cache_key=f"env:{hash_auth_value(auth_header)}",
        )
    return AuthContext(auth_header=None, source="none", cache_key="anonymous")


def request_auth_context() -> AuthContext:
    headers = REQUEST_HEADERS.get()
    username = headers.get(NEXTCLOUD_USERNAME_HEADER)
    app_token = headers.get(NEXTCLOUD_APP_TOKEN_HEADER)
    if username and app_token:
        auth_header = encode_basic_auth(username, app_token)
        return AuthContext(
            auth_header=auth_header,
            source="request_basic_headers",
            cache_key=f"request:{hash_auth_value(auth_header)}",
        )

    return AuthContext(auth_header=None, source="none", cache_key="anonymous")


def build_nextcloud_headers(
    auth_context: AuthContext,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    headers = {
        "Accept": "application/json, */*",
        "OCS-APIRequest": "true",
    }
    if auth_context.auth_header:
        headers["Authorization"] = auth_context.auth_header
    if extra_headers:
        headers.update(extra_headers)
    return headers


async def fetch_json(client: httpx.AsyncClient, url: str, auth_context: AuthContext) -> Any:
    response = await client.get(url, headers=build_nextcloud_headers(auth_context))
    response.raise_for_status()
    return response.json()


async def discover_operations(auth_context: AuthContext) -> DiscoveryState:
    if not auth_context.auth_header:
        raise RuntimeError(
            "Missing server discovery credentials. Set `NEXTCLOUD_USERNAME` "
            "and `NEXTCLOUD_APP_TOKEN` in the MCP server environment."
        )

    logger.info("Discovering Nextcloud APIs from %s", API_VIEWER_URL)
    timeout = httpx.Timeout(DISCOVERY_TIMEOUT_SECONDS)

    async with httpx.AsyncClient(timeout=timeout) as client:
        apps = await fetch_json(client, f"{API_VIEWER_URL}/apps", auth_context)
        operations: dict[str, OperationDefinition] = {}
        discovered_apps: list[dict[str, Any]] = []

        for app_meta in apps:
            app_id = app_meta.get("id")
            if not app_id:
                continue

            try:
                openapi = await fetch_json(client, f"{API_VIEWER_URL}/apps/{app_id}", auth_context)
            except Exception as exc:
                logger.warning("Skipping app %s: %s", app_id, exc)
                continue

            paths = openapi.get("paths", {})
            if not isinstance(paths, dict) or not paths:
                logger.info("Skipping app %s without usable OpenAPI paths", app_id)
                continue

            app_operation_count = 0
            for path, path_item in paths.items():
                if not isinstance(path_item, dict):
                    continue

                inherited_parameters = path_item.get("parameters", [])
                for method, operation in path_item.items():
                    if method not in HTTP_METHODS or not isinstance(operation, dict):
                        continue

                    definition = make_operation_definition(
                        app_meta=app_meta,
                        path=path,
                        method=method,
                        operation=operation,
                        inherited_parameters=inherited_parameters,
                    )
                    operations[definition.name] = definition
                    app_operation_count += 1

            discovered_apps.append(
                {
                    "id": app_id,
                    "name": app_meta.get("name", app_id),
                    "operation_count": app_operation_count,
                }
            )

    state = DiscoveryState(
        operations=operations,
        apps=sorted(discovered_apps, key=lambda item: item["id"]),
        last_refresh=now_iso(),
        last_error=None,
    )
    logger.info(
        "Discovered %d apps and %d MCP tools",
        len(state.apps),
        len(state.operations),
    )
    return state


def current_state() -> DiscoveryState:
    return DISCOVERY_STATE


async def refresh_state(force: bool = False) -> DiscoveryState:
    global DISCOVERY_STATE

    if (
        not force
        and (DISCOVERY_STATE.last_refresh is not None or DISCOVERY_STATE.last_error is not None)
    ):
        return DISCOVERY_STATE

    try:
        state = await discover_operations(default_auth_context())
    except Exception as exc:
        state = DiscoveryState(last_error=str(exc))
        logger.warning("Discovery failed for auth source %s: %s", default_auth_context().source, exc)
    DISCOVERY_STATE = state
    return DISCOVERY_STATE


def discovery_status_payload() -> dict[str, Any]:
    request_context = request_auth_context()
    discovery_auth_context = default_auth_context()
    state = current_state()
    return {
        "nextcloud_url": NEXTCLOUD_URL,
        "api_viewer_url": API_VIEWER_URL,
        "auth_source": discovery_auth_context.source,
        "auth_configured": discovery_auth_context.auth_header is not None,
        "discovery_auth_source": discovery_auth_context.source,
        "discovery_auth_configured": discovery_auth_context.auth_header is not None,
        "request_auth_source": request_context.source,
        "request_auth_configured": request_context.auth_header is not None,
        "supported_request_headers": [
            "X-Nextcloud-Username",
            "X-Nextcloud-AppToken",
        ],
        "app_count": len(state.apps),
        "tool_count": len(state.operations),
        "apps": state.apps,
        "last_refresh": state.last_refresh,
        "last_error": state.last_error,
    }


def dynamic_tool_description(definition: OperationDefinition) -> str:
    return (
        f"Live Nextcloud API tool for the connected instance. "
        f"App: {definition.app_id} ({definition.app_name}). "
        f"Action: {definition.summary}. "
        f"Use this only for real operations against the configured Nextcloud server, "
        f"not for documentation lookup or local workspace tasks."
    )


def list_tools() -> list[types.Tool]:
    state = current_state()
    static_tools = [
        types.Tool(
            name=STATUS_TOOL_NAME,
            description=(
                "Show the live connected Nextcloud instance, discovery status, "
                "discovered apps, and available MCP tools."
            ),
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
    ]

    dynamic_tools = [
        types.Tool(
            name=definition.name,
            description=dynamic_tool_description(definition),
            inputSchema=definition.input_schema,
        )
        for definition in sorted(state.operations.values(), key=lambda item: item.name)
    ]

    return static_tools + dynamic_tools


def build_request_body(definition: OperationDefinition, arguments: dict[str, Any]) -> tuple[Any, Any, Any]:
    if definition.body_mode == "none":
        return None, None, None

    if definition.body_mode == "flattened_json_object":
        body = {
            field_name: arguments[field_name]
            for field_name in definition.body_fields
            if field_name in arguments
        }
    else:
        body = arguments.get("body")

    if body is None:
        return None, None, None

    content_type = definition.body_content_type or "application/json"
    if "json" in content_type:
        return body, None, None
    if content_type == "application/x-www-form-urlencoded":
        return None, body, None
    if isinstance(body, str):
        return None, None, body
    return None, None, body


def parse_response_body(response: httpx.Response) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "").lower()

    if "json" in content_type:
        try:
            return {"data": response.json()}
        except ValueError:
            return {"data": response.text}

    if content_type.startswith("text/") or "xml" in content_type or "html" in content_type:
        return {"data": response.text}

    return {
        "data_base64": base64.b64encode(response.content).decode("ascii"),
        "encoding": "base64",
    }


async def execute_operation(definition: OperationDefinition, arguments: dict[str, Any]) -> dict[str, Any]:
    auth_context = request_auth_context()
    if not auth_context.auth_header:
        raise ValueError(
            "Missing request credentials for this tool call. Configure MCP HTTP headers "
            "`X-Nextcloud-Username` and `X-Nextcloud-AppToken`. "
            "Server discovery credentials are not used for tool execution."
        )

    actual_path = definition.path
    for parameter_name in definition.path_params:
        if parameter_name not in arguments:
            raise ValueError(f"Missing required path parameter: {parameter_name}")
        actual_path = actual_path.replace(
            f"{{{parameter_name}}}",
            quote(str(arguments[parameter_name]), safe=""),
        )

    query_params = {
        parameter_name: arguments[parameter_name]
        for parameter_name in definition.query_params
        if parameter_name in arguments and arguments[parameter_name] is not None
    }
    headers = build_nextcloud_headers(auth_context)
    for parameter_name in definition.header_params:
        if parameter_name in arguments and arguments[parameter_name] is not None:
            headers[parameter_name] = str(arguments[parameter_name])

    json_body, form_body, raw_body = build_request_body(definition, arguments)
    if definition.body_content_type and "Content-Type" not in headers:
        headers["Content-Type"] = definition.body_content_type

    url = f"{NEXTCLOUD_URL}{actual_path}"
    timeout = httpx.Timeout(DISCOVERY_TIMEOUT_SECONDS)

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.request(
            method=definition.method,
            url=url,
            params=query_params,
            headers=headers,
            json=json_body,
            data=form_body,
            content=raw_body,
        )

    payload = {
        "ok": response.is_success,
        "status_code": response.status_code,
        "app_id": definition.app_id,
        "tool_name": definition.name,
        "method": definition.method,
        "path": definition.path,
        "resolved_url": url,
        "response_headers": dict(response.headers),
    }
    payload.update(parse_response_body(response))
    return payload


server = Server(
    SERVER_NAME,
)


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    await refresh_state()
    return list_tools()


@server.call_tool()
async def handle_call_tool(
    tool_name: str,
    arguments: dict[str, Any],
) -> types.CallToolResult | dict[str, Any]:
    if tool_name == STATUS_TOOL_NAME:
        return discovery_status_payload()

    state = await refresh_state()
    definition = state.operations.get(tool_name)
    if definition is None:
        raise ValueError(f"Unknown tool: {tool_name}")

    payload = await execute_operation(definition, arguments)
    return payload


session_manager = StreamableHTTPSessionManager(
    app=server,
    event_store=None,
    json_response=True,
    stateless=True,
)


async def handle_streamable_http(scope: Scope, receive: Receive, send: Send) -> None:
    headers = {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }
    token = REQUEST_HEADERS.set(headers)
    try:
        await session_manager.handle_request(scope, receive, send)
    finally:
        REQUEST_HEADERS.reset(token)


async def healthcheck(_request) -> JSONResponse:
    return JSONResponse(
        {
            "name": "Nextcloud Live Instance MCP",
            "server_name": SERVER_NAME,
            "transport": "streamable-http",
            "mcp_path": "/mcp",
            "default_auth_configured": default_auth_context().auth_header is not None,
            **discovery_status_payload(),
        },
        status_code=200,
    )


@contextlib.asynccontextmanager
async def lifespan(_app: Starlette):
    async with session_manager.run():
        await refresh_state(force=True)
        yield


app = Starlette(
    debug=os.getenv("DEBUG", "").lower() == "true",
    routes=[
        Route("/", endpoint=healthcheck, methods=["GET"]),
        Mount("/mcp", app=handle_streamable_http),
    ],
    lifespan=lifespan,
)

app = CORSMiddleware(
    app,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
    expose_headers=["Mcp-Session-Id"],
)


async def run_stdio() -> None:
    await refresh_state(force=True)
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    if MCP_TRANSPORT == "stdio":
        asyncio.run(run_stdio())
        return

    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)


if __name__ == "__main__":
    main()
