"""Tests for agent_box.agents."""

from unittest.mock import AsyncMock, patch

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
    settings.agents = ["opencode"]
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
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

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
    agent._client = mock_client
    msgs = [m async for m in agent.run("test prompt")]

    mock_client.query.assert_awaited_once_with("test prompt")
    texts = [m.text for m in msgs if m.type.value == "text"]
    assert "Hello" in texts[0]
    assert "World" in texts[1]
    assert agent.project.session_id == "sess-abc"


@pytest.mark.anyio
async def test_run_no_response(sample_project: ProjectInfo):
    from claude_agent_sdk import ResultMessage

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
    msgs = [m async for m in agent.run("test")]
    assert all(m.type.value == "result" for m in msgs)


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
    await agent.close()


# ── AskUserQuestion interception ──


@pytest.mark.anyio
async def test_ask_user_question_intercepted(sample_project: ProjectInfo):
    """When the agent calls AskUserQuestion, run() yields the question as
    text, stores pending state, and returns early — it must NOT deadlock."""
    from claude_agent_sdk import AssistantMessage, ToolUseBlock

    mock_client = AsyncMock()
    mock_client.query = AsyncMock()

    async def fake_receive():
        yield AssistantMessage(
            content=[
                ToolUseBlock(
                    id="toolu_123",
                    name="AskUserQuestion",
                    input={
                        "question": "Which file?",
                        "options": [
                            {"label": "main.py"},
                            {"label": "utils.py"},
                        ],
                    },
                )
            ],
            model="test",
            session_id="sess-ask",
        )
        # The real CLI would block here waiting for tool_result.
        # Our generator should return before reaching this point.

    mock_client.receive_response = fake_receive

    agent = ClaudeCodeAgent(sample_project)
    agent._client = mock_client
    msgs = [m async for m in agent.run("refactor the code")]

    # Question should be yielded as plain text so the channel can deliver it
    assert len(msgs) == 1
    assert msgs[0].type.value == "text"
    assert "Which file?" in msgs[0].text
    assert "main.py" in msgs[0].text

    # Pending state should be saved
    assert agent._pending_ask is not None
    assert agent._pending_ask["tool_use_id"] == "toolu_123"
    assert agent._pending_ask["session_id"] == "sess-ask"
    assert agent.has_pending_question is True


@pytest.mark.anyio
async def test_ask_user_question_resume(sample_project: ProjectInfo):
    """Second run() with a pending ask should send tool_result, not a new query."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    mock_client = AsyncMock()
    mock_client._transport = AsyncMock()
    mock_client.query = AsyncMock()

    async def fake_receive():
        yield AssistantMessage(content=[TextBlock(text="OK, editing main.py")], model="test")
        yield ResultMessage(
            subtype="result", is_error=False, duration_ms=500, duration_api_ms=400,
            num_turns=2, total_cost_usd=0.02, usage=None, session_id="sess-ask",
        )

    mock_client.receive_response = fake_receive

    agent = ClaudeCodeAgent(sample_project)
    agent._client = mock_client
    agent._pending_ask = {
        "tool_use_id": "toolu_123",
        "session_id": "sess-ask",
    }

    msgs = [m async for m in agent.run("main.py")]

    # Should NOT have called query() — instead sends tool_result via transport
    mock_client.query.assert_not_awaited()
    mock_client._transport.write.assert_awaited_once()
    written = mock_client._transport.write.call_args[0][0]
    assert "tool_result" in written
    assert "toolu_123" in written
    assert "main.py" in written

    # Pending ask should be cleared
    assert agent._pending_ask is None
    assert agent.has_pending_question is False

    # Should have yielded the assistant response and result
    texts = [m.text for m in msgs if m.type.value == "text"]
    assert any("OK, editing main.py" in t for t in texts)


@pytest.mark.anyio
async def test_ask_user_question_no_options(sample_project: ProjectInfo):
    """AskUserQuestion without options should still format the question."""
    from claude_agent_sdk import AssistantMessage, ToolUseBlock

    mock_client = AsyncMock()
    mock_client.query = AsyncMock()

    async def fake_receive():
        yield AssistantMessage(
            content=[
                ToolUseBlock(
                    id="toolu_456",
                    name="AskUserQuestion",
                    input={"question": "What is your name?"},
                )
            ],
            model="test",
            session_id="sess-2",
        )

    mock_client.receive_response = fake_receive

    agent = ClaudeCodeAgent(sample_project)
    agent._client = mock_client
    msgs = [m async for m in agent.run("hello")]

    assert len(msgs) == 1
    assert "What is your name?" in msgs[0].text
    assert agent._pending_ask["tool_use_id"] == "toolu_456"


@pytest.mark.anyio
async def test_close_clears_pending_ask(sample_project: ProjectInfo):
    """close() should clear any pending ask state."""
    mock_client = AsyncMock()
    agent = ClaudeCodeAgent(sample_project)
    agent._client = mock_client
    agent._pending_ask = {"tool_use_id": "toolu_789", "session_id": "s"}

    await agent.close()
    assert agent._pending_ask is None
    assert agent._client is None


@pytest.mark.anyio
async def test_normal_tool_use_not_intercepted(sample_project: ProjectInfo):
    """Regular tool calls should be yielded as brief text summaries."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock

    mock_client = AsyncMock()
    mock_client.query = AsyncMock()

    async def fake_receive():
        yield AssistantMessage(
            content=[
                ToolUseBlock(id="toolu_bash", name="Bash", input={"command": "ls -la"}),
            ],
            model="test",
        )
        yield ResultMessage(
            subtype="result", is_error=False, duration_ms=100, duration_api_ms=90,
            num_turns=1, total_cost_usd=0.01, usage=None, session_id="sess-norm",
        )

    mock_client.receive_response = fake_receive

    agent = ClaudeCodeAgent(sample_project)
    agent._client = mock_client
    msgs = [m async for m in agent.run("list files")]

    # Tool should be yielded as text (not tool_use) so IM channels can send it
    texts = [m for m in msgs if m.type.value == "text"]
    assert any("ls -la" in m.text for m in texts)
    assert agent._pending_ask is None


# ── Tool summary formatting ──


def test_format_tool_summary_bash():
    from claude_agent_sdk import ToolUseBlock
    from agent_box.agents.claude_code import _format_tool_summary

    block = ToolUseBlock(id="t1", name="Bash", input={"command": "npm test"})
    assert _format_tool_summary(block) == "🔧 npm test"


def test_format_tool_summary_bash_shortens_project_path():
    from claude_agent_sdk import ToolUseBlock
    from agent_box.agents.claude_code import _format_tool_summary

    block = ToolUseBlock(
        id="t1", name="Bash",
        input={"command": "cd /home/user/workspace/proj && npm test"},
    )
    result = _format_tool_summary(block, prefixes=("/home/user/workspace/proj",))
    assert result == "🔧 cd . && npm test"


def test_format_tool_summary_bash_shortens_escaped_spaces():
    from claude_agent_sdk import ToolUseBlock
    from agent_box.agents.claude_code import _format_tool_summary

    block = ToolUseBlock(
        id="t1", name="Bash",
        input={"command": "ls /home/user/agent\\ box/proj/src"},
    )
    result = _format_tool_summary(
        block, prefixes=("/home/user/agent box/proj",),
    )
    assert result == "🔧 ls ./src"


def test_shorten_paths_in_cmd():
    from agent_box.agents.claude_code import _shorten_paths_in_cmd

    assert _shorten_paths_in_cmd(
        "cd /home/user/proj && npm test",
        ("/home/user/proj",),
    ) == "cd . && npm test"


def test_shorten_paths_in_cmd_escaped():
    from agent_box.agents.claude_code import _shorten_paths_in_cmd

    assert _shorten_paths_in_cmd(
        "cd /home/user/my\\ project && ls",
        ("/home/user/my project",),
    ) == "cd . && ls"


def test_shorten_paths_in_cmd_quoted():
    """Paths with quoted directory names containing spaces should be shortened."""
    from agent_box.agents.claude_code import _shorten_paths_in_cmd

    assert _shorten_paths_in_cmd(
        'cd /home/user/workspace/"my project"/subdir && npm test',
        ("/home/user/workspace/my project",),
    ) == "cd ./subdir && npm test"


def test_shorten_paths_in_cmd_single_quoted():
    """Single-quoted directory names should be shortened."""
    from agent_box.agents.claude_code import _shorten_paths_in_cmd

    assert _shorten_paths_in_cmd(
        "cd /home/user/workspace/'agent box'/agent-box && ls",
        ("/home/user/workspace/agent box",),
    ) == "cd ./agent-box && ls"


def test_shorten_paths_in_cmd_no_match():
    from agent_box.agents.claude_code import _shorten_paths_in_cmd

    assert _shorten_paths_in_cmd(
        "ls /tmp/other",
        ("/home/user/proj",),
    ) == "ls /tmp/other"


def test_format_tool_summary_bash_long_command():
    from claude_agent_sdk import ToolUseBlock
    from agent_box.agents.claude_code import _format_tool_summary

    long_cmd = "a" * 100
    block = ToolUseBlock(id="t1", name="Bash", input={"command": long_cmd})
    result = _format_tool_summary(block)
    assert result.startswith("🔧 ")
    assert len(result) <= 63  # emoji + 60 chars max


def test_format_tool_summary_read():
    from claude_agent_sdk import ToolUseBlock
    from agent_box.agents.claude_code import _format_tool_summary

    block = ToolUseBlock(id="t2", name="Read", input={"file_path": "src/main.py"})
    assert _format_tool_summary(block) == "📖 src/main.py"


def test_format_tool_summary_edit():
    from claude_agent_sdk import ToolUseBlock
    from agent_box.agents.claude_code import _format_tool_summary

    block = ToolUseBlock(id="t3", name="Edit", input={"file_path": "src/utils.py"})
    assert _format_tool_summary(block) == "✏️ src/utils.py"


def test_format_tool_summary_grep():
    from claude_agent_sdk import ToolUseBlock
    from agent_box.agents.claude_code import _format_tool_summary

    block = ToolUseBlock(id="t4", name="Grep", input={"pattern": "TODO"})
    assert _format_tool_summary(block) == "🔍 TODO"


def test_format_tool_summary_agent():
    from claude_agent_sdk import ToolUseBlock
    from agent_box.agents.claude_code import _format_tool_summary

    block = ToolUseBlock(id="t5", name="Agent", input={"description": "explore codebase"})
    assert _format_tool_summary(block) == "🤖 explore codebase"


def test_format_tool_summary_unknown():
    from claude_agent_sdk import ToolUseBlock
    from agent_box.agents.claude_code import _format_tool_summary

    block = ToolUseBlock(id="t6", name="CustomTool", input={})
    assert _format_tool_summary(block) == "⚙️ CustomTool"


def test_shorten_path_strips_project():
    from agent_box.agents.claude_code import _shorten_path

    assert _shorten_path("/home/user/workspace/proj/src/main.py", ("/home/user/workspace/proj",)) == "src/main.py"


def test_shorten_path_strips_download_dir():
    from agent_box.agents.claude_code import _shorten_path

    dl = "/home/user/.agent-box/channels/weixin/downloads"
    assert _shorten_path(f"{dl}/image-123.jpg", (dl,)) == "image-123.jpg"


def test_shorten_path_no_match():
    from agent_box.agents.claude_code import _shorten_path

    assert _shorten_path("/tmp/other/file.txt", ("/home/user/proj",)) == "/tmp/other/file.txt"


def test_shorten_path_exact_match_no_trailing_slash():
    from agent_box.agents.claude_code import _shorten_path

    # If path == prefix exactly, return as-is (nothing to show after strip)
    assert _shorten_path("/home/user/proj", ("/home/user/proj",)) == "/home/user/proj"


def test_format_tool_summary_with_prefixes():
    from claude_agent_sdk import ToolUseBlock
    from agent_box.agents.claude_code import _format_tool_summary

    block = ToolUseBlock(
        id="t1", name="Read",
        input={"file_path": "/home/user/workspace/proj/src/main.py"},
    )
    result = _format_tool_summary(block, prefixes=("/home/user/workspace/proj",))
    assert result == "📖 src/main.py"


def test_format_tool_summary_edit_with_download_prefix():
    from claude_agent_sdk import ToolUseBlock
    from agent_box.agents.claude_code import _format_tool_summary

    dl = "/home/user/.agent-box/channels/qq/downloads"
    block = ToolUseBlock(
        id="t2", name="Edit",
        input={"file_path": f"{dl}/report.pdf"},
    )
    result = _format_tool_summary(block, prefixes=(dl,))
    assert result == "✏️ report.pdf"


@pytest.mark.anyio
async def test_run_yields_tool_summary_as_text(sample_project: ProjectInfo):
    """Multiple tool calls should each yield a brief text line."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

    mock_client = AsyncMock()
    mock_client.query = AsyncMock()

    async def fake_receive():
        yield AssistantMessage(
            content=[
                ToolUseBlock(id="t1", name="Read", input={"file_path": "a.py"}),
            ],
            model="test",
        )
        yield AssistantMessage(
            content=[
                ToolUseBlock(id="t2", name="Bash", input={"command": "npm test"}),
            ],
            model="test",
        )
        yield AssistantMessage(
            content=[TextBlock(text="Done!")],
            model="test",
        )
        yield ResultMessage(
            subtype="result", is_error=False, duration_ms=1000, duration_api_ms=900,
            num_turns=1, total_cost_usd=0.01, usage=None, session_id="s1",
        )

    mock_client.receive_response = fake_receive

    agent = ClaudeCodeAgent(sample_project)
    agent._client = mock_client
    msgs = [m async for m in agent.run("test")]

    texts = [m.text for m in msgs if m.type.value == "text"]
    assert "📖 a.py" in texts
    assert "🔧 npm test" in texts
    assert "Done!" in texts
