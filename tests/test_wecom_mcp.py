"""Tests for agent_box.tools.wecom_mcp."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp import types as mcp_types

from agent_box.tools import wecom_mcp as wm


@pytest.fixture(autouse=True)
def _reset_wecom_mcp_state():
    yield
    wm._ws_clients.clear()
    wm.clear_mcp_cache()
    wm._sessions.clear()
    wm._stateless_categories.clear()


async def _invoke_tool(server_cfg, arguments):
    """Invoke the wecom_mcp tool through the in-process MCP server."""
    srv = server_cfg["instance"]
    handler = srv.request_handlers[mcp_types.CallToolRequest]
    req = mcp_types.CallToolRequest(
        params=mcp_types.CallToolRequestParams(name="wecom_mcp", arguments=arguments)
    )
    return await handler(req)


# ── _resolve_instance ──


def test_resolve_instance():
    assert wm._resolve_instance("wecom:prod") == "wecom:prod"
    assert wm._resolve_instance("wecom:test") == "wecom:test"
    assert wm._resolve_instance("qq") is None
    assert wm._resolve_instance("tui") is None
    assert wm._resolve_instance("") is None


# ── WSClient registry ──


def test_ws_client_registry_by_instance():
    a, b = object(), object()
    wm.set_ws_client("wecom:a", a)
    wm.set_ws_client("wecom:b", b)
    assert wm.get_ws_client("wecom:a") is a
    assert wm.get_ws_client("wecom:b") is b
    assert wm.get_ws_client("nonexistent") is None


def test_ws_client_registry_clear():
    wm.set_ws_client("wecom:a", object())
    wm.set_ws_client("wecom:a", None)
    assert wm.get_ws_client("wecom:a") is None


def test_get_ws_client_primary_prefers_default():
    default, first = object(), object()
    wm.set_ws_client("wecom:prod", first)
    wm.set_ws_client("wecom:default", default)
    assert wm.get_ws_client() is default


def test_get_ws_client_primary_first_registered():
    first = object()
    wm.set_ws_client("wecom:prod", first)
    assert wm.get_ws_client() is first


def test_get_ws_client_empty_returns_none():
    assert wm.get_ws_client() is None


# ── Cache keyed by (instance, category) ──


@pytest.mark.anyio
async def test_mcp_cache_keyed_by_instance():
    def make_client(url):
        c = MagicMock()
        c.is_connected = True
        c.reply = AsyncMock(return_value={"errcode": 0, "body": {"url": url}})
        return c

    wm.set_ws_client("wecom:a", make_client("http://a/mcp"))
    wm.set_ws_client("wecom:b", make_client("http://b/mcp"))

    url_a = await wm._get_mcp_url("wecom:a", "doc")
    url_b = await wm._get_mcp_url("wecom:b", "doc")

    assert url_a == "http://a/mcp"
    assert url_b == "http://b/mcp"
    assert ("wecom:a", "doc") in wm._mcp_config_cache
    assert ("wecom:b", "doc") in wm._mcp_config_cache


@pytest.mark.anyio
async def test_get_mcp_url_uses_right_client():
    a = MagicMock()
    a.is_connected = True
    a.reply = AsyncMock(return_value={"errcode": 0, "body": {"url": "http://a/mcp"}})
    b = MagicMock()
    b.is_connected = True
    b.reply = AsyncMock(return_value={"errcode": 0, "body": {"url": "http://b/mcp"}})
    wm.set_ws_client("wecom:a", a)
    wm.set_ws_client("wecom:b", b)

    await wm._get_mcp_url("wecom:b", "contact")
    a.reply.assert_not_awaited()
    b.reply.assert_awaited_once()


# ── Tool routing via the agent-bound closure ──


@pytest.mark.anyio
async def test_tool_uses_current_channel_instance():
    with patch.object(wm, "_mcp_request", new=AsyncMock(return_value={"tools": []})) as mock_req:
        cfg = wm.create_wecom_mcp_server(lambda: "wecom:test")
        await _invoke_tool(
            cfg, {"action": "list", "category": "doc", "method": "", "args": ""}
        )

    mock_req.assert_awaited_once()
    instance, category, method = mock_req.await_args.args[:3]
    assert instance == "wecom:test"
    assert category == "doc"
    assert method == "tools/list"


@pytest.mark.anyio
async def test_tool_falls_back_to_primary_for_non_wecom():
    with patch.object(wm, "_mcp_request", new=AsyncMock(return_value={"tools": []})) as mock_req:
        cfg = wm.create_wecom_mcp_server(lambda: "qq")
        await _invoke_tool(
            cfg, {"action": "list", "category": "doc", "method": "", "args": ""}
        )

    instance, category, method = mock_req.await_args.args[:3]
    assert instance is None
    assert category == "doc"


@pytest.mark.anyio
async def test_tool_call_action_forwards_method():
    with patch.object(wm, "_mcp_request", new=AsyncMock(return_value={"ok": True})) as mock_req:
        cfg = wm.create_wecom_mcp_server(lambda: "wecom:prod")
        await _invoke_tool(
            cfg,
            {
                "action": "call",
                "category": "doc",
                "method": "createDocument",
                "args": '{"title": "会议纪要"}',
            },
        )

    instance, category, method, params = mock_req.await_args.args[:4]
    assert instance == "wecom:prod"
    assert method == "tools/call"
    assert params["name"] == "createDocument"
    assert params["arguments"] == {"title": "会议纪要"}
