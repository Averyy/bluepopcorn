"""FastAPI application for BluePopcorn MCP HTTP server."""

from __future__ import annotations

import json
import logging
import sys
import traceback
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import Request, Response
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from ...prompts import LLMS_TXT
from ...seerr import SeerrClient
from .. import _log
from ..config import Config, load_config
from ..server import create_server
from .middleware import get_client_ip, hash_api_key, verify_bearer_auth

log = logging.getLogger(__name__)


def create_app(config: Config | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    if config is None:
        config = load_config()

    # Parse API keys (comma-separated for multiple keys)
    api_key_hashes: set[str] = set()
    api_key_count = 0
    if config.api_key:
        for key in config.api_key.split(","):
            key = key.strip()
            if key:
                api_key_hashes.add(hash_api_key(key))
                api_key_count += 1

    if not api_key_hashes:
        _log("ERROR: MCP_API_KEY is required for HTTP mode. Set it in .env or environment.")
        _log("HTTP mode exposes seerr_request which triggers real downloads — open HTTP is not safe.")
        sys.exit(1)

    # Create SeerrClient and MCP server
    seerr = SeerrClient(
        base_url=config.seerr_url,
        api_key=config.seerr_api_key,
        timeout=config.http_timeout,
        min_rating_votes=config.min_rating_votes,
    )
    mcp_server = create_server(config, seerr)
    session_manager = StreamableHTTPSessionManager(
        app=mcp_server,
        stateless=True,
        json_response=True,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _log("bluepopcorn MCP HTTP server starting")
        _log(f"Bearer token authentication enabled ({api_key_count} key(s))")
        _log(f"Seerr: {config.seerr_url}")

        async with session_manager.run():
            app.state.session_manager = session_manager
            yield

        _log("Shutting down...")
        await seerr.close()

    app = FastAPI(
        title="bluepopcorn",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    app.state.config = config
    app.state.api_key_hashes = api_key_hashes

    # Health check (no auth)
    @app.get("/health")
    async def health():
        return {
            "status": "healthy",
            "service": "bluepopcorn-mcp",
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }

    # llms.txt (no auth, per llmstxt.org spec)
    @app.get("/llms.txt", response_class=PlainTextResponse)
    async def llms_txt():
        return LLMS_TXT

    MAX_BODY_SIZE = 1_048_576  # 1MB

    # MCP endpoint (requires auth)
    @app.post("/mcp")
    async def mcp_endpoint(request: Request):
        client_ip = get_client_ip(request)

        # Verify authentication
        auth_error = verify_bearer_auth(request, api_key_hashes)
        if auth_error:
            _log(f"Auth failed: {auth_error} from {client_ip}")
            return JSONResponse(
                status_code=401,
                content={
                    "jsonrpc": "2.0",
                    "error": {"code": -32001, "message": auth_error},
                    "id": None,
                },
                headers={"WWW-Authenticate": "Bearer"},
            )

        try:
            # Pre-check Content-Length before buffering body
            content_length = request.headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) > MAX_BODY_SIZE:
                        return JSONResponse(
                            status_code=413,
                            content={
                                "jsonrpc": "2.0",
                                "error": {"code": -32000, "message": "Request body too large (max 1MB)"},
                                "id": None,
                            },
                        )
                except ValueError:
                    pass

            # Read body
            body_bytes = await request.body()
            if len(body_bytes) > MAX_BODY_SIZE:
                return JSONResponse(
                    status_code=413,
                    content={
                        "jsonrpc": "2.0",
                        "error": {"code": -32000, "message": "Request body too large (max 1MB)"},
                        "id": None,
                    },
                )

            # Log method for debugging
            try:
                body_json = json.loads(body_bytes)
                method = str(body_json.get("method", "unknown"))[:200].translate(str.maketrans("", "", "\r\n\t\x00"))
                _log(f"MCP method={method} from {client_ip}")
            except Exception:
                _log(f"Non-JSON body from {client_ip}")

            # Delegate to StreamableHTTPSessionManager
            manager = request.app.state.session_manager

            response_body: list[bytes] = []
            response_status = [200]
            response_headers_list: list[list[tuple[bytes, bytes]]] = [[]]

            async def collect_send(message):
                if message["type"] == "http.response.start":
                    response_status[0] = message.get("status", 200)
                    response_headers_list[0] = message.get("headers", [])
                elif message["type"] == "http.response.body":
                    body = message.get("body", b"")
                    if body:
                        response_body.append(body)

            body_consumed = False

            async def receive_with_body():
                nonlocal body_consumed
                if not body_consumed:
                    body_consumed = True
                    return {"type": "http.request", "body": body_bytes, "more_body": False}
                return {"type": "http.disconnect"}

            await manager.handle_request(request.scope, receive_with_body, collect_send)

            content = b"".join(response_body)
            # Build headers dict, joining duplicates with comma per HTTP spec
            headers: dict[str, str] = {}
            for k, v in response_headers_list[0]:
                name = k.decode()
                value = v.decode()
                if name in headers:
                    headers[name] = f"{headers[name]}, {value}"
                else:
                    headers[name] = value

            return Response(
                content=content,
                status_code=response_status[0],
                headers=headers,
            )

        except Exception as e:
            _log(f"Error handling MCP request: {e}")
            traceback.print_exc(file=sys.stderr)
            return JSONResponse(
                status_code=500,
                content={
                    "jsonrpc": "2.0",
                    "error": {"code": -32603, "message": "Internal server error"},
                    "id": None,
                },
            )

    # MCP protocol discovery
    @app.head("/mcp")
    async def mcp_head():
        return Response(
            status_code=200,
            headers={
                "MCP-Protocol-Version": "2025-06-18",
                "Allow": "POST, HEAD",
            },
        )

    # Reject GET on /mcp
    @app.get("/mcp")
    async def mcp_get():
        return JSONResponse(
            status_code=405,
            content={
                "jsonrpc": "2.0",
                "error": {"code": -32000, "message": "Method not allowed. Use POST or HEAD."},
                "id": None,
            },
            headers={"Allow": "POST, HEAD"},
        )

    # Catch-all 404
    @app.exception_handler(404)
    async def not_found(request, exc):
        return JSONResponse(
            status_code=404,
            content={
                "jsonrpc": "2.0",
                "error": {"code": -32000, "message": "Not found"},
                "id": None,
            },
        )

    return app


async def run_http_server(config: Config | None = None) -> None:
    """Run the HTTP server."""
    import uvicorn

    if config is None:
        config = load_config()

    app = create_app(config)

    server_config = uvicorn.Config(
        app,
        host=config.http_host,
        port=config.http_port,
        log_level="warning",
    )
    server = uvicorn.Server(server_config)

    _log(f"bluepopcorn MCP HTTP server listening on {config.http_host}:{config.http_port}")

    await server.serve()
