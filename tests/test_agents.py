"""Tests for agent_box.agents."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_box.models import ProjectInfo
from agent_box.agents import create_agent
from agent_box.agents.base import BaseAgent
from agent_box.agents.claude_code import ClaudeCodeAgent


# ── create_agent factory ──

def test_create_agent_claude_code(sample_project: ProjectInfo):
    agent = create_agent("claude_code", sample_project)
    assert isinstance(agent, ClaudeCodeAgent)


def test_create_agent_unknown(sample_project: ProjectInfo):
    with pytest.raises(ValueError):
        create_agent("nonexistent", sample_project)


def test_create_agent_not_enabled(sample_project: ProjectInfo):
    """Agent type exists but not in settings.agents."""
    from agent_box.config import settings
    original = settings.agents
    settings.agents = ["opencode"]  # claude_code not enabled
    try:
        with pytest.raises(ValueError, match="not enabled"):
            create_agent("claude_code", sample_project)
    finally:
        settings.agents = original


# ── BaseAgent ──

def test_base_agent_is_abstract(sample_project: ProjectInfo):
    with pytest.raises(TypeError):
        BaseAgent(sample_project)


def test_base_agent_subclass(sample_project: ProjectInfo):
    class Dummy(BaseAgent):
        async def run(self, prompt: str) -> str:
            return "ok"

    d = Dummy(sample_project)
    assert d.project is sample_project


# ── ClaudeCodeAgent ──

def test_build_options(sample_project: ProjectInfo):
    agent = ClaudeCodeAgent(sample_project)
    opts = agent._build_options()
    assert opts.cwd == sample_project.path
    assert opts.continue_conversation is True
    assert opts.permission_mode == "bypassPermissions"


def test_initial_client_is_none(sample_project: ProjectInfo):
    agent = ClaudeCodeAgent(sample_project)
    assert agent._client is None


@pytest.mark.anyio
async def test_ensure_client_creates_once(sample_project: ProjectInfo):
    agent = ClaudeCodeAgent(sample_project)

    mock_client = AsyncMock()
    with patch("agent_box.agents.claude_code.ClaudeSDKClient", return_value=mock_client):
        c1 = await agent._ensure_client()
        c2 = await agent._ensure_client()

    assert c1 is c2
    mock_client.connect.assert_awaited_once()


@pytest.mark.anyio
async def test_run_collects_text(sample_project: ProjectInfo):
    from claude_code_sdk import AssistantMessage, ResultMessage, TextBlock

    mock_client = AsyncMock()
    mock_client.query = AsyncMock()

    async def fake_receive():
        yield AssistantMessage(content=[TextBlock(text="Hello ")], model="test")
        yield AssistantMessage(content=[TextBlock(text="World")], model="test")
        yield ResultMessage(
            subtype="result",
            is_error=False,
            duration_ms=1000,
            duration_api_ms=900,
            num_turns=1,
            total_cost_usd=0.01,
            usage=None,
            session_id="sess-abc",
        )

    mock_client.receive_response = fake_receive

    agent = ClaudeCodeAgent(sample_project)
    with patch("agent_box.agents.claude_code.ClaudeSDKClient", return_value=mock_client):
        agent._client = mock_client
        result = await agent.run("test prompt")

    mock_client.query.assert_awaited_once_with("test prompt")
    assert result == "Hello \nWorld"
    assert agent.project.session_id == "sess-abc"


@pytest.mark.anyio
async def test_run_no_response(sample_project: ProjectInfo):
    from claude_code_sdk import ResultMessage

    mock_client = AsyncMock()
    mock_client.query = AsyncMock()

    async def fake_receive():
        yield ResultMessage(
            subtype="result", is_error=False, duration_ms=100, duration_api_ms=90,
            num_turns=1, total_cost_usd=0.0, usage=None, session_id="s1",
        )

    mock_client.receive_response = fake_receive

    agent = ClaudeCodeAgent(sample_project)
    agent._client = mock_client
    result = await agent.run("test")
    assert result == "(no response)"


@pytest.mark.anyio
async def test_close(sample_project: ProjectInfo):
    mock_client = AsyncMock()
    agent = ClaudeCodeAgent(sample_project)
    agent._client = mock_client

    await agent.close()
    mock_client.disconnect.assert_awaited_once()
    assert agent._client is None


@pytest.mark.anyio
async def test_close_when_no_client(sample_project: ProjectInfo):
    agent = ClaudeCodeAgent(sample_project)
    await agent.close()  # should not raise
