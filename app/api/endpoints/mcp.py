"""无会话 Streamable HTTP MCP。复用账户 token 和助手工具，写操作只生成提案。"""
from __future__ import annotations

import json
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.endpoints import get_current_user
from app.core.version import APP_VERSION
from app.modules.agent.tools import REGISTRY, TOOL_SCHEMAS, call_tool

VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")


def check_origin(request: Request):
    origin = request.headers.get("origin")
    if origin:
        parsed = urlsplit(origin)
        if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != request.url.netloc.lower():
            raise HTTPException(403, "Origin 不受信任")


router = APIRouter(prefix="/mcp", tags=["mcp"], dependencies=[Depends(check_origin), Depends(get_current_user)])


def error(request_id, code, message, status=200):
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}, status_code=status)


@router.get("")
def get_mcp():
    return Response(status_code=405, headers={"Allow": "POST"})


@router.post("")
async def post_mcp(request: Request):
    if request.headers.get("mcp-protocol-version", "2025-03-26") not in VERSIONS:
        return error(None, -32600, "Unsupported protocol version", 400)
    if "application/json" not in request.headers.get("content-type", ""):
        raise HTTPException(415, "Content-Type 必须为 application/json")
    body_bytes = bytearray()
    async for chunk in request.stream():
        body_bytes.extend(chunk)
        if len(body_bytes) > 65536:
            raise HTTPException(413, "请求过大")
    try:
        body = json.loads(body_bytes)
    except (ValueError, UnicodeError):
        return error(None, -32700, "Parse error", 400)
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str):
        return error(None, -32600, "Invalid Request", 400)
    request_id = body.get("id")
    if isinstance(request_id, (dict, list, bool)):
        return error(None, -32600, "Invalid request id", 400)
    method = body["method"]
    params = body.get("params", {})
    if not isinstance(params, dict):
        return error(request_id, -32602, "Invalid params")
    if "id" not in body:
        if method.startswith("notifications/"):
            return Response(status_code=202)
        return error(None, -32600, "Requests require an id", 400)
    if method == "initialize":
        version = params.get("protocolVersion")
        result = {"protocolVersion": version if version in VERSIONS else VERSIONS[0],
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "cinefold", "version": APP_VERSION},
                  "instructions": "写操作仅生成提案，须在 cinefold 的工具接入页面确认后执行。播放交给 Emby。"}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": t["function"]["name"], "description": t["function"]["description"],
                             "inputSchema": t["function"]["parameters"]} for t in TOOL_SCHEMAS]}
    elif method == "tools/call":
        name, arguments = params.get("name"), params.get("arguments", {})
        if not isinstance(name, str) or name not in REGISTRY or not isinstance(arguments, dict):
            return error(request_id, -32602, "Unknown tool or invalid arguments")
        schema = next(t["function"]["parameters"] for t in TOOL_SCHEMAS if t["function"]["name"] == name)
        if any(key not in arguments for key in schema.get("required", [])):
            return error(request_id, -32602, "Missing required arguments")
        output = await run_in_threadpool(call_tool, name, arguments)
        parsed = json.loads(output)
        result = {"content": [{"type": "text", "text": output}], "isError": isinstance(parsed, dict) and "error" in parsed}
    else:
        return error(request_id, -32601, "Method not found")
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})
