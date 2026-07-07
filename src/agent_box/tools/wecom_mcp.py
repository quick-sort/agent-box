"""wecom_mcp — SDK MCP tool for calling WeCom MCP Server.

Provides two actions:
  - list: List available tools in a given category (doc, contact, schedule, etc.)
  - call: Call a specific tool method with JSON arguments

The tool communicates with the WeCom MCP Server via:
1. WebSocket: send `aibot_get_mcp_config` to get the HTTP URL for a category
2. HTTP JSON-RPC: call the MCP Server using Streamable HTTP protocol

This module exposes:
  - `create_wecom_mcp_server()` — returns an McpSdkServerConfig for ClaudeAgentOptions
  - `set_ws_client()` / `get_ws_client()` — manage the global WSClient reference
  - `wecom_mcp_enabled` / `set_wecom_mcp_enabled()` — flag for whether wecom channel is active
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from claude_agent_sdk import tool, create_sdk_mcp_server
from claude_agent_sdk.types import McpSdkServerConfig

from wecom_aibot_sdk import WSClient, generate_req_id
from wecom_aibot_sdk.types import WsCmd

log = logging.getLogger(__name__)

# ── Enabled flag ─────────────────────────────────────────────────────────────
# Set by App.run() when wecom channel is in the active channel list.

_enabled: bool = False


def set_wecom_mcp_enabled(enabled: bool) -> None:
    """Set whether wecom_mcp tool should be active."""
    global _enabled
    _enabled = enabled


def is_wecom_mcp_enabled() -> bool:
    """Check if wecom_mcp tool is currently enabled."""
    return _enabled

# ── Global WSClient reference ────────────────────────────────────────────────
# Set by WecomChannel when the WebSocket connection is established.

_ws_client: WSClient | None = None


def set_ws_client(client: WSClient | None) -> None:
    """Store the active WSClient for use by the MCP tool."""
    global _ws_client
    _ws_client = client


def get_ws_client() -> WSClient | None:
    """Get the currently active WSClient."""
    return _ws_client


# ── MCP Config cache ─────────────────────────────────────────────────────────

_mcp_config_cache: dict[str, str] = {}  # category → URL


def clear_mcp_cache() -> None:
    """Clear all cached MCP config URLs."""
    _mcp_config_cache.clear()


async def _get_mcp_url(category: str) -> str:
    """Fetch MCP Server URL for a category via WSClient.

    Sends `aibot_get_mcp_config` command through the WebSocket connection
    to retrieve the HTTP endpoint for the given MCP category.
    """
    if category in _mcp_config_cache:
        return _mcp_config_cache[category]

    client = get_ws_client()
    if not client or not client.is_connected:
        raise RuntimeError("WeCom WebSocket 未连接，无法获取 MCP 配置")

    req_id = generate_req_id("mcp_config")
    response = await client.reply(
        {"headers": {"req_id": req_id}},
        {"biz_type": category},
        "aibot_get_mcp_config",
    )

    errcode = response.get("errcode", -1)
    if errcode != 0:
        raise RuntimeError(
            f"MCP 配置请求失败: errcode={errcode}, errmsg={response.get('errmsg', '')}"
        )

    body = response.get("body") or {}
    url = body.get("url")
    if not url:
        raise RuntimeError(f"MCP 配置响应缺少 url 字段 (category={category!r})")

    _mcp_config_cache[category] = url
    log.info("wecom_mcp: config fetched for category=%s url=%s", category, url)
    return url


# ── Streamable HTTP session management ───────────────────────────────────────

# category → session_id (None = stateless)
_sessions: dict[str, str | None] = {}
_stateless_categories: set[str] = set()

_HTTP_TIMEOUT = 30.0
_INIT_TIMEOUT = 15.0


async def _send_jsonrpc(
    url: str,
    method: str,
    params: dict[str, Any] | None = None,
    session_id: str | None = None,
    timeout: float = _HTTP_TIMEOUT,
) -> tuple[Any, str | None]:
    """Send a JSON-RPC request to the MCP Server.

    Returns (result, new_session_id).
    """
    body: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": generate_req_id("mcp_rpc"),
        "method": method,
    }
    if params is not None:
        body["params"] = params

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=body, headers=headers)

    new_session_id = resp.headers.get("mcp-session-id")

    if resp.status_code == 204 or not resp.text.strip():
        return None, new_session_id

    if not resp.is_success:
        raise RuntimeError(f"MCP HTTP 请求失败: {resp.status_code} {resp.reason_phrase}")

    content_type = resp.headers.get("content-type", "")

    # Handle SSE response
    if "text/event-stream" in content_type:
        result = _parse_sse(resp.text)
        return result, new_session_id

    # Normal JSON response
    data = resp.json()
    if "error" in data and data["error"]:
        err = data["error"]
        raise RuntimeError(f"MCP 调用错误 [{err.get('code', '?')}]: {err.get('message', '')}")
    return data.get("result"), new_session_id


def _parse_sse(text: str) -> Any:
    """Parse SSE response, extract last event's data as JSON-RPC result."""
    lines = text.split("\n")
    current_parts: list[str] = []
    last_data = ""

    for line in lines:
        if line.startswith("data: "):
            current_parts.append(line[6:])
        elif line.startswith("data:"):
            current_parts.append(line[5:])
        elif line.strip() == "" and current_parts:
            last_data = "\n".join(current_parts).strip()
            current_parts = []

    if current_parts:
        last_data = "\n".join(current_parts).strip()

    if not last_data:
        raise RuntimeError("SSE 响应中未包含有效数据")

    rpc = json.loads(last_data)
    if "error" in rpc and rpc["error"]:
        err = rpc["error"]
        raise RuntimeError(f"MCP 调用错误 [{err.get('code', '?')}]: {err.get('message', '')}")
    return rpc.get("result")


async def _ensure_session(category: str, url: str) -> str | None:
    """Ensure MCP session is initialized for a category. Returns session_id."""
    if category in _stateless_categories:
        return None
    if category in _sessions:
        return _sessions[category]

    # Initialize session
    init_params = {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "wecom_mcp", "version": "1.0.0"},
    }
    _, session_id = await _send_jsonrpc(
        url, "initialize", init_params, timeout=_INIT_TIMEOUT
    )

    if not session_id:
        # Stateless server
        _stateless_categories.add(category)
        _sessions[category] = None
        return None

    # Send initialized notification
    notify_body: dict[str, Any] = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Mcp-Session-Id": session_id,
    }
    async with httpx.AsyncClient(timeout=_INIT_TIMEOUT) as client:
        await client.post(url, json=notify_body, headers=headers)

    _sessions[category] = session_id
    log.info("wecom_mcp: session established for category=%s", category)
    return session_id


async def _mcp_request(category: str, method: str, params: dict[str, Any] | None = None) -> Any:
    """High-level MCP request with session management and retry on 404."""
    url = await _get_mcp_url(category)
    session_id = await _ensure_session(category, url)

    try:
        result, new_sid = await _send_jsonrpc(url, method, params, session_id)
        if new_sid:
            _sessions[category] = new_sid
        return result
    except RuntimeError as e:
        # Session expired (404) — rebuild and retry once
        if "404" in str(e):
            _sessions.pop(category, None)
            session_id = await _ensure_session(category, url)
            result, new_sid = await _send_jsonrpc(url, method, params, session_id)
            if new_sid:
                _sessions[category] = new_sid
            return result
        raise


# ── Tool definitions ─────────────────────────────────────────────────────────

@tool(
    "wecom_mcp",
    "调用企业微信 MCP 工具。支持两种操作：\n"
    "  - list: 列出指定品类的所有可用工具及其参数定义\n"
    "  - call: 调用指定品类的某个工具方法\n\n"
    "品类包括: contact(通讯录), doc(文档), schedule(日程), "
    "meeting(会议), todo(待办), msg(消息), smartsheet(智能表格)\n\n"
    "示例:\n"
    "  list contact → 列出通讯录相关的所有工具\n"
    "  call doc createDocument {\"title\": \"会议纪要\"} → 创建文档",
    {
        "action": str,
        "category": str,
        "method": str,
        "args": str,
    },
)
async def wecom_mcp_tool(args: dict[str, Any]) -> dict[str, Any]:
    """Execute a wecom_mcp action (list or call)."""
    action = args.get("action", "")
    category = args.get("category", "")
    method = args.get("method", "")
    raw_args = args.get("args", "")

    if not category:
        return _text_result({"error": "缺少 category 参数"})

    try:
        if action == "list":
            result = await _mcp_request(category, "tools/list")
            tools = (result or {}).get("tools", [])
            return _text_result({
                "category": category,
                "count": len(tools),
                "tools": [
                    {
                        "name": t.get("name"),
                        "description": t.get("description", ""),
                        "inputSchema": t.get("inputSchema"),
                    }
                    for t in tools
                ],
            })

        elif action == "call":
            if not method:
                return _text_result({"error": "action 为 call 时必须提供 method 参数"})

            # Parse args
            call_args: dict[str, Any] = {}
            if raw_args:
                if isinstance(raw_args, str):
                    try:
                        call_args = json.loads(raw_args)
                    except json.JSONDecodeError as e:
                        return _text_result({"error": f"args 不是合法 JSON: {e}"})
                elif isinstance(raw_args, dict):
                    call_args = raw_args

            result = await _mcp_request(category, "tools/call", {
                "name": method,
                "arguments": call_args,
            })
            return _text_result(result)

        else:
            return _text_result({"error": f"未知操作: {action}，支持 list 和 call"})

    except Exception as e:
        log.exception("wecom_mcp: action=%s category=%s method=%s failed", action, category, method)
        return _text_result({"error": str(e)})


def _text_result(data: Any) -> dict[str, Any]:
    """Format result as MCP tool response."""
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}]}


# ── Public API ───────────────────────────────────────────────────────────────

def create_wecom_mcp_server() -> McpSdkServerConfig:
    """Create the wecom_mcp SDK MCP server config for ClaudeAgentOptions."""
    return create_sdk_mcp_server("wecom_mcp", tools=[wecom_mcp_tool])
