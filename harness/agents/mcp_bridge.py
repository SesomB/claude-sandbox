"""Serves a set of harness tools over MCP (streamable HTTP) to a coding-agent CLI.

The CLI (Claude Code, OpenCode) runs its own agent loop, but with its built-in tools
disabled it can only act through these tools, which are the harness's scoped,
validated tools. That keeps file scoping, gates and red→green enforcement intact.

Only what a tools-only server needs is implemented: initialize, tools/list,
tools/call and ping, answered as plain JSON (no server-sent events). The server binds
to 127.0.0.1 on a random port, under a random path token.
"""

from __future__ import annotations

import asyncio
import secrets
import socket
from collections.abc import Awaitable, Callable
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from harness.models.base import ToolSpec

ToolHandler = Callable[[str, dict[str, Any]], Awaitable[tuple[str, bool]]]

DEFAULT_PROTOCOL = "2025-06-18"


class McpBridge:
    def __init__(self, tools: list[ToolSpec], handler: ToolHandler, *, name: str = "harness"):
        self.tools = tools
        self.handler = handler
        self.name = name
        self._token = secrets.token_urlsafe(24)
        self._session = secrets.token_hex(16)
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._port = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}/mcp/{self._token}"

    # -- lifecycle ----------------------------------------------------------------------

    async def __aenter__(self) -> McpBridge:
        app = Starlette(
            routes=[Route("/mcp/{token}", self._endpoint, methods=["GET", "POST", "DELETE"])]
        )
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        self._port = sock.getsockname()[1]
        config = uvicorn.Config(app, log_level="warning", lifespan="off")
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve(sockets=[sock]))
        while not self._server.started:
            if self._task.done():
                self._task.result()  # surface the startup error
            await asyncio.sleep(0.01)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            await self._task

    # -- protocol -------------------------------------------------------------------------

    async def _endpoint(self, request: Request) -> Response:
        if not secrets.compare_digest(request.path_params["token"], self._token):
            return Response(status_code=404)
        if request.method == "GET":  # no server-initiated stream
            return Response(status_code=405)
        if request.method == "DELETE":
            return Response(status_code=200)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(_error(None, -32700, "parse error"), status_code=400)
        messages = body if isinstance(body, list) else [body]
        replies = [r for m in messages if (r := await self.handle(m)) is not None]
        if not replies:
            return Response(status_code=202)
        return JSONResponse(
            replies if isinstance(body, list) else replies[0],
            headers={"Mcp-Session-Id": self._session},
        )

    async def handle(self, msg: Any) -> dict[str, Any] | None:
        """One JSON-RPC message → its response (None for notifications)."""
        if not isinstance(msg, dict) or "method" not in msg:
            return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid")
        if "id" not in msg:
            return None  # notifications/initialized, notifications/cancelled, ...
        mid, method, params = msg["id"], msg["method"], msg.get("params") or {}
        if method == "initialize":
            return _result(
                mid,
                {
                    "protocolVersion": params.get("protocolVersion") or DEFAULT_PROTOCOL,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": self.name, "version": "1"},
                },
            )
        if method == "ping":
            return _result(mid, {})
        if method == "tools/list":
            return _result(
                mid,
                {
                    "tools": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "inputSchema": t.input_schema,
                        }
                        for t in self.tools
                    ]
                },
            )
        if method == "tools/call":
            name = str(params.get("name", ""))
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                return _error(mid, -32602, "arguments must be an object")
            text, is_error = await self.handler(name, args)
            return _result(mid, {"content": [{"type": "text", "text": text}], "isError": is_error})
        return _error(mid, -32601, f"method not found: {method}")


def _result(mid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}
