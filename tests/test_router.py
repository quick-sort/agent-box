"""Tests for agent_box.router."""

from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

import pytest

from agent_box.models import IncomingMessage
from agent_box.session_manager import SessionManager


def _msg(text: str) -> IncomingMessage:
    return IncomingMessage(text=text, user_id="u1", channel="test")


def _make_router(tmp_projects: SessionManager, *, tool_call=None):
    """Create a Router whose underlying anthropic client returns a fixed
    response (either a single tool_use block or nothing)."""
    from agent_box.router.router import Router

    with patch("agent_box.router.router.AsyncAnthropic"):
        router = Router(tmp_projects)

    fake_resp = SimpleNamespace(content=[tool_call] if tool_call else [])
    router._client = MagicMock()
    router._client.messages = MagicMock()
    router._client.messages.create = AsyncMock(return_value=fake_resp)
    return router


def _tool(tool_name: str, **inputs):
    """Build a MagicMock that satisfies ``isinstance(_, ToolUseBlock)``."""
    from agent_box.router.router import ToolUseBlock

    fake = MagicMock(spec=ToolUseBlock)
    fake.name = tool_name
    fake.input = inputs
    return fake


# ── Forward (no tool call) ──

@pytest.mark.anyio
async def test_forwards_to_default_when_no_tool_call(tmp_projects: SessionManager):
    router = _make_router(tmp_projects, tool_call=None)
    result = await router.route(_msg("hello agent"))
    assert result.reply is None
    assert result.project == "_default"


@pytest.mark.anyio
async def test_forwards_to_pinned_project(tmp_projects: SessionManager):
    tmp_projects.create("pinned")
    tmp_projects.set_current("pinned")
    router = _make_router(tmp_projects, tool_call=None)
    result = await router.route(_msg("do stuff"))
    assert result.project == "pinned"


# ── create_project tool ──

@pytest.mark.anyio
async def test_create_project_tool(tmp_projects: SessionManager):
    """LLM-classified create_project (not matched by regex fast-path)."""
    router = _make_router(tmp_projects, tool_call=_tool("create_project", name="newp"))
    result = await router.route(_msg("make me a workspace for newp"))
    assert result.reply is not None and "newp" in result.reply
    assert tmp_projects.get("newp") is not None
    assert tmp_projects.get_current() == "newp"
    router._client.messages.create.assert_awaited_once()


@pytest.mark.anyio
async def test_create_project_existing_switches(tmp_projects: SessionManager):
    tmp_projects.create("existing")
    router = _make_router(tmp_projects, tool_call=_tool("create_project", name="existing"))
    result = await router.route(_msg("create existing"))
    assert result.reply is not None
    assert tmp_projects.get_current() == "existing"


@pytest.mark.anyio
async def test_create_project_missing_name(tmp_projects: SessionManager):
    router = _make_router(tmp_projects, tool_call=_tool("create_project", name=""))
    result = await router.route(_msg("create something"))
    assert result.reply is not None and "required" in result.reply


# ── switch_project tool ──

@pytest.mark.anyio
async def test_switch_project_tool(tmp_projects: SessionManager):
    tmp_projects.create("alpha")
    router = _make_router(tmp_projects, tool_call=_tool("switch_project", name="alpha"))
    result = await router.route(_msg("switch to alpha"))
    assert result.reply is not None and "alpha" in result.reply
    assert tmp_projects.get_current() == "alpha"


@pytest.mark.anyio
async def test_switch_unknown_project(tmp_projects: SessionManager):
    router = _make_router(tmp_projects, tool_call=_tool("switch_project", name="ghost"))
    result = await router.route(_msg("switch to ghost"))
    assert result.reply is not None and "Unknown" in result.reply


@pytest.mark.anyio
async def test_switch_to_default(tmp_projects: SessionManager):
    tmp_projects.create("other")
    tmp_projects.set_current("other")
    router = _make_router(tmp_projects, tool_call=_tool("switch_project", name="_default"))
    result = await router.route(_msg("go back to default"))
    assert result.reply is not None
    assert tmp_projects.get_current() == "_default"


@pytest.mark.anyio
async def test_switch_missing_name(tmp_projects: SessionManager):
    router = _make_router(tmp_projects, tool_call=_tool("switch_project", name=""))
    result = await router.route(_msg("switch"))
    assert result.reply is not None and "required" in result.reply


# ── list_projects tool ──

@pytest.mark.anyio
async def test_list_projects_empty_after_ensure_default(tmp_projects: SessionManager):
    # Router ctor ensures the default project exists, so it will appear here.
    router = _make_router(tmp_projects, tool_call=_tool("list_projects"))
    result = await router.route(_msg("what projects do I have?"))
    assert result.reply is not None
    assert "_default" in result.reply
    assert "★" in result.reply


@pytest.mark.anyio
async def test_list_projects_with_pinned(tmp_projects: SessionManager):
    tmp_projects.create("a")
    tmp_projects.create("b")
    tmp_projects.set_current("b")
    router = _make_router(tmp_projects, tool_call=_tool("list_projects"))
    result = await router.route(_msg("list"))
    assert result.reply is not None
    assert "a" in result.reply and "b" in result.reply
    # 'b' should be marked with the star
    star_line = next(line for line in result.reply.splitlines() if line.startswith("★"))
    assert "b" in star_line


# ── Router file layout ──

def test_router_creates_dot_router_folder(tmp_projects: SessionManager):
    _make_router(tmp_projects)
    assert (tmp_projects.workspace / ".router").is_dir()


# ── Regex fast-path ──


def test_fast_classify_switch_to():
    from agent_box.router.router import _fast_classify

    assert _fast_classify("switchto my-project") == ("switch_project", "my-project")
    assert _fast_classify("switchto default") == ("switch_project", "")


def test_fast_classify_create_project():
    from agent_box.router.router import _fast_classify

    assert _fast_classify("newproject api-server") == ("create_project", "api-server")
    assert _fast_classify("newproject webapp") == ("create_project", "webapp")


def test_fast_classify_list_projects():
    from agent_box.router.router import _fast_classify

    assert _fast_classify("listprojects") == ("list_projects", "")


def test_fast_classify_no_match():
    from agent_box.router.router import _fast_classify

    # Natural language — should NOT match (goes to LLM instead)
    assert _fast_classify("switch to my-project") is None
    assert _fast_classify("new project api-server") is None
    assert _fast_classify("list projects") is None
    assert _fast_classify("fix the bug in main.py") is None
    assert _fast_classify("切换到 demo") is None
    assert _fast_classify("hello") is None


@pytest.mark.anyio
async def test_route_uses_fast_path_switch(tmp_projects: SessionManager):
    """Regex match should bypass LLM call entirely."""
    tmp_projects.create("my-proj")
    router = _make_router(tmp_projects, tool_call=None)

    result = await router.route(_msg("switchto my-proj"))
    assert result.reply is not None and "my-proj" in result.reply
    assert tmp_projects.get_current() == "my-proj"
    router._client.messages.create.assert_not_awaited()


@pytest.mark.anyio
async def test_route_uses_fast_path_create(tmp_projects: SessionManager):
    router = _make_router(tmp_projects, tool_call=None)

    result = await router.route(_msg("newproject webapp"))
    assert result.reply is not None and "webapp" in result.reply
    assert tmp_projects.get("webapp") is not None
    router._client.messages.create.assert_not_awaited()


@pytest.mark.anyio
async def test_route_uses_fast_path_list(tmp_projects: SessionManager):
    router = _make_router(tmp_projects, tool_call=None)

    result = await router.route(_msg("listprojects"))
    assert result.reply is not None
    router._client.messages.create.assert_not_awaited()


@pytest.mark.anyio
async def test_route_falls_through_to_llm(tmp_projects: SessionManager):
    """Natural language should still call the LLM."""
    router = _make_router(tmp_projects, tool_call=None)

    result = await router.route(_msg("switch to my-proj"))
    assert result.project == "_default"
    router._client.messages.create.assert_awaited_once()


# ── delete_project ──


def test_fast_classify_delete_project():
    from agent_box.router.router import _fast_classify

    result = _fast_classify("rmproject foo")
    assert result is not None
    assert result[0] == "delete_project"
    assert result[1] == "foo"


@pytest.mark.anyio
async def test_delete_project_fast_path(tmp_projects: SessionManager):
    tmp_projects.create("to-delete")
    router = _make_router(tmp_projects, tool_call=None)

    result = await router.route(_msg("rmproject to-delete"))
    assert result.reply is not None
    assert "Deleted" in result.reply
    assert tmp_projects.get("to-delete") is None
    router._client.messages.create.assert_not_awaited()


@pytest.mark.anyio
async def test_delete_project_tool_call(tmp_projects: SessionManager):
    tmp_projects.create("to-delete")
    router = _make_router(tmp_projects, tool_call=_tool("delete_project", name="to-delete"))

    result = await router.route(_msg("delete project to-delete"))
    assert "Deleted" in result.reply
    assert tmp_projects.get("to-delete") is None


@pytest.mark.anyio
async def test_delete_project_unknown(tmp_projects: SessionManager):
    router = _make_router(tmp_projects, tool_call=_tool("delete_project", name="nope"))

    result = await router.route(_msg("delete project nope"))
    assert "Unknown" in result.reply


@pytest.mark.anyio
async def test_delete_default_project_blocked(tmp_projects: SessionManager):
    router = _make_router(tmp_projects, tool_call=_tool("delete_project", name="_default"))

    result = await router.route(_msg("delete project _default"))
    assert "Cannot delete" in result.reply


# ── switch_model ──


def test_fast_classify_switch_model():
    from agent_box.router.router import _fast_classify

    result = _fast_classify("switchmodel claude-sonnet-4-6")
    assert result is not None
    assert result[0] == "switch_model"
    assert result[1] == "claude-sonnet-4-6"


@pytest.mark.anyio
async def test_switch_model_default_resets_to_none(tmp_projects: SessionManager):
    tmp_projects.ensure_default()
    tmp_projects.set_model("_default", "some-model")
    assert tmp_projects.get("_default").model == "some-model"

    router = _make_router(tmp_projects, tool_call=None)
    result = await router.route(_msg("switchmodel default"))
    assert "Reset" in result.reply
    assert tmp_projects.get("_default").model is None
    router._client.messages.create.assert_not_awaited()


@pytest.mark.anyio
async def test_switch_model_default_via_llm(tmp_projects: SessionManager):
    tmp_projects.set_model("_default", "some-model")
    router = _make_router(tmp_projects, tool_call=_tool("switch_model", model="default"))

    result = await router.route(_msg("reset model to default"))
    assert "Reset" in result.reply
    assert tmp_projects.get("_default").model is None
