"""Tests for agent_box.router."""

from unittest.mock import AsyncMock, patch

import pytest

from agent_box.models import IncomingMessage
from agent_box.session_manager import SessionManager


def _msg(text: str) -> IncomingMessage:
    return IncomingMessage(text=text, user_id="u1", channel="test")


def _make_router(tmp_projects: SessionManager, agent_response: str = "DEFAULT"):
    """Create a Router with a mocked agent."""
    from agent_box.router.router import Router

    mock_agent = AsyncMock()
    mock_agent.run = AsyncMock(return_value=agent_response)

    with patch("agent_box.router.router.create_agent", return_value=mock_agent):
        router = Router(tmp_projects)

    router._agent = mock_agent
    return router


# ── Explicit commands ──

@pytest.mark.anyio
async def test_new_project_command(tmp_projects: SessionManager):
    router = _make_router(tmp_projects)
    result = await router.route(_msg("/new-project My App"))
    assert result == "NEW_PROJECT My App"


@pytest.mark.anyio
async def test_new_project_underscore(tmp_projects: SessionManager):
    router = _make_router(tmp_projects)
    result = await router.route(_msg("/new_project foo bar"))
    assert result == "NEW_PROJECT foo bar"


@pytest.mark.anyio
async def test_switch_existing(tmp_projects: SessionManager):
    tmp_projects.create("web-app")
    router = _make_router(tmp_projects)
    result = await router.route(_msg("/switch web-app"))
    assert result == "web-app"


@pytest.mark.anyio
async def test_switch_nonexistent(tmp_projects: SessionManager):
    router = _make_router(tmp_projects)
    result = await router.route(_msg("/switch nope"))
    assert result == "DEFAULT"


# ── No projects → DEFAULT ──

@pytest.mark.anyio
async def test_no_projects_returns_default(tmp_projects: SessionManager):
    router = _make_router(tmp_projects)
    result = await router.route(_msg("do something"))
    assert result == "DEFAULT"


# ── Agent classification ──

@pytest.mark.anyio
async def test_agent_routes_to_known_project(tmp_projects: SessionManager):
    tmp_projects.create("my-project")
    router = _make_router(tmp_projects, agent_response="my-project")
    result = await router.route(_msg("update the homepage"))
    assert result == "my-project"


@pytest.mark.anyio
async def test_agent_unknown_slug_returns_default(tmp_projects: SessionManager):
    tmp_projects.create("real-project")
    router = _make_router(tmp_projects, agent_response="nonexistent-slug")
    result = await router.route(_msg("something random"))
    assert result == "DEFAULT"


@pytest.mark.anyio
async def test_agent_suggests_new_project(tmp_projects: SessionManager):
    tmp_projects.create("existing")
    router = _make_router(tmp_projects, agent_response="NEW_PROJECT cool-idea")
    result = await router.route(_msg("start a new project called cool-idea"))
    assert result == "NEW_PROJECT cool-idea"


@pytest.mark.anyio
async def test_agent_multiline_takes_first_line(tmp_projects: SessionManager):
    tmp_projects.create("my-proj")
    router = _make_router(tmp_projects, agent_response="my-proj\nsome extra explanation")
    result = await router.route(_msg("fix the bug"))
    assert result == "my-proj"


# ── .router folder ──

def test_router_creates_dot_router_folder(tmp_projects: SessionManager):
    router = _make_router(tmp_projects)
    assert (tmp_projects.workspace / ".router").is_dir()
    assert router._agent is not None
