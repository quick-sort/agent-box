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
async def test_ask_user_question_with_prompt_field(sample_project: ProjectInfo):
    """AskUserQuestion using 'prompt' field instead of 'question' should still work."""
    from claude_agent_sdk import AssistantMessage, ToolUseBlock

    mock_client = AsyncMock()
    mock_client.query = AsyncMock()

    async def fake_receive():
        yield AssistantMessage(
            content=[
                ToolUseBlock(
                    id="toolu_prompt",
                    name="AskUserQuestion",
                    input={"prompt": "What file should I edit?"},
                )
            ],
            model="test",
            session_id="sess-prompt",
        )

    mock_client.receive_response = fake_receive

    agent = ClaudeCodeAgent(sample_project)
    agent._client = mock_client
    msgs = [m async for m in agent.run("edit something")]

    assert len(msgs) == 1
    assert "What file should I edit?" in msgs[0].text


@pytest.mark.anyio
async def test_ask_user_question_empty_input(sample_project: ProjectInfo):
    """AskUserQuestion with empty input should show default message."""
    from claude_agent_sdk import AssistantMessage, ToolUseBlock

    mock_client = AsyncMock()
    mock_client.query = AsyncMock()

    async def fake_receive():
        yield AssistantMessage(
            content=[
                ToolUseBlock(
                    id="toolu_empty",
                    name="AskUserQuestion",
                    input={},
                )
            ],
            model="test",
            session_id="sess-empty",
        )

    mock_client.receive_response = fake_receive

    agent = ClaudeCodeAgent(sample_project)
    agent._client = mock_client
    msgs = [m async for m in agent.run("do something")]

    assert len(msgs) == 1
    # Should show default message instead of empty
    assert "(agent requires your input)" in msgs[0].text or msgs[0].text == ""


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


# ── _format_question tests ──


def test_format_question_with_question_field():
    from claude_agent_sdk import ToolUseBlock
    from agent_box.agents.claude_code import ClaudeCodeAgent

    block = ToolUseBlock(
        id="t1",
        name="AskUserQuestion",
        input={"question": "Which file?", "options": [{"label": "A"}, {"label": "B"}]},
    )
    result = ClaudeCodeAgent._format_question(block)
    assert result == "Which file?\n\n  1. A\n  2. B"


def test_format_question_with_prompt_field():
    """Should support 'prompt' as alternative to 'question'."""
    from claude_agent_sdk import ToolUseBlock
    from agent_box.agents.claude_code import ClaudeCodeAgent

    block = ToolUseBlock(
        id="t2",
        name="AskUserQuestion",
        input={"prompt": "Select a mode"},
    )
    result = ClaudeCodeAgent._format_question(block)
    assert result == "Select a mode"


def test_format_question_empty_returns_default():
    """Empty input should return default message."""
    from claude_agent_sdk import ToolUseBlock
    from agent_box.agents.claude_code import ClaudeCodeAgent

    block = ToolUseBlock(
        id="t3",
        name="AskUserQuestion",
        input={},
    )
    result = ClaudeCodeAgent._format_question(block)
    assert result == "(agent requires your input)"


def test_format_question_with_questions_array():
    """Claude Code's actual AskUserQuestion format has questions array."""
    from claude_agent_sdk import ToolUseBlock
    from agent_box.agents.claude_code import ClaudeCodeAgent

    block = ToolUseBlock(
        id="t4",
        name="AskUserQuestion",
        input={
            "questions": [
                {
                    "question": "Which library should we use?",
                    "header": "Library",
                    "options": [
                        {"label": "Option A", "description": "First choice"},
                        {"label": "Option B", "description": "Second choice"},
                    ],
                }
            ]
        },
    )
    result = ClaudeCodeAgent._format_question(block)
    assert "[Library]" in result
    assert "Which library should we use?" in result
    assert "1. Option A — First choice" in result
    assert "2. Option B — Second choice" in result


def test_format_question_multiple_questions():
    """Test handling multiple questions in one AskUserQuestion."""
    from claude_agent_sdk import ToolUseBlock
    from agent_box.agents.claude_code import ClaudeCodeAgent

    block = ToolUseBlock(
        id="t5",
        name="AskUserQuestion",
        input={
            "questions": [
                {
                    "question": "First question?",
                    "options": [{"label": "A"}, {"label": "B"}],
                },
                {
                    "question": "Second question?",
                    "options": [{"label": "X"}, {"label": "Y"}],
                },
            ]
        },
    )
    result = ClaudeCodeAgent._format_question(block)
    assert "First question?" in result
    assert "Second question?" in result


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


# ── SEND_FILE marker parsing ──


def test_parse_send_file_markers_single():
    from agent_box.agents.claude_code import _parse_send_file_markers

    text, paths = _parse_send_file_markers(
        "Here is the chart:\n[SEND_FILE:/tmp/chart.png]\nHope you like it!"
    )
    assert paths == ["/tmp/chart.png"]
    assert "[SEND_FILE:" not in text
    assert "Here is the chart:" in text
    assert "Hope you like it!" in text


def test_parse_send_file_markers_multiple():
    from agent_box.agents.claude_code import _parse_send_file_markers

    text, paths = _parse_send_file_markers(
        "Charts:\n[SEND_FILE:/tmp/a.png]\n[SEND_FILE:/tmp/b.pdf]"
    )
    assert paths == ["/tmp/a.png", "/tmp/b.pdf"]
    assert "[SEND_FILE:" not in text


def test_parse_send_file_markers_none():
    from agent_box.agents.claude_code import _parse_send_file_markers

    text, paths = _parse_send_file_markers("No markers here.")
    assert paths == []
    assert text == "No markers here."


# ── run() with SEND_FILE markers ──


@pytest.mark.anyio
async def test_run_yields_file_from_send_file_marker(sample_project: ProjectInfo):
    """When agent text contains [SEND_FILE:path], yield a separate OutgoingMessage with file_path."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    mock_client = AsyncMock()
    mock_client.query = AsyncMock()

    async def fake_receive():
        yield AssistantMessage(
            content=[TextBlock(text="Here's the chart:\n[SEND_FILE:/tmp/chart.png]")],
            model="test",
        )
        yield ResultMessage(
            subtype="result", is_error=False, duration_ms=100, duration_api_ms=90,
            num_turns=1, total_cost_usd=0.01, usage=None, session_id="s1",
        )

    mock_client.receive_response = fake_receive

    agent = ClaudeCodeAgent(sample_project)
    agent._client = mock_client
    msgs = [m async for m in agent.run("make a chart")]

    # Should have: text (without marker), file_path message, result
    file_msgs = [m for m in msgs if m.data and "file_path" in m.data]
    assert len(file_msgs) == 1
    assert file_msgs[0].data["file_path"] == "/tmp/chart.png"

    # Text message should not contain the marker
    text_msgs = [m for m in msgs if m.text and m.type.value == "text"]
    assert any("chart" in m.text for m in text_msgs)
    assert not any("[SEND_FILE:" in m.text for m in text_msgs)


# ── system_prompt injection ──


def test_build_options_includes_system_prompt(sample_project: ProjectInfo):
    opts = agent._build_options() if False else ClaudeCodeAgent(sample_project)._build_options()
    assert opts.system_prompt is not None
    assert isinstance(opts.system_prompt, dict)
    assert opts.system_prompt["type"] == "preset"
    assert opts.system_prompt["preset"] == "claude_code"
    assert "append" in opts.system_prompt
    assert "SEND_FILE" in opts.system_prompt["append"]


# ── Context window limit recovery ──


def test_is_context_limit_error_true():
    from claude_agent_sdk import ResultMessage
    from agent_box.agents.claude_code import _is_context_limit_error

    msg = ResultMessage(
        subtype="success", is_error=True, duration_ms=0, duration_api_ms=0,
        num_turns=1, session_id="s1",
        errors=["API Error: The model has reached its context window limit."],
    )
    assert _is_context_limit_error(msg) is True


def test_is_context_limit_error_true_in_result():
    from claude_agent_sdk import ResultMessage
    from agent_box.agents.claude_code import _is_context_limit_error

    msg = ResultMessage(
        subtype="success", is_error=True, duration_ms=0, duration_api_ms=0,
        num_turns=1, session_id="s1",
        result="context window limit exceeded",
    )
    assert _is_context_limit_error(msg) is True


def test_is_context_limit_error_false_not_error():
    from claude_agent_sdk import ResultMessage
    from agent_box.agents.claude_code import _is_context_limit_error

    msg = ResultMessage(
        subtype="success", is_error=False, duration_ms=0, duration_api_ms=0,
        num_turns=1, session_id="s1",
    )
    assert _is_context_limit_error(msg) is False


def test_is_context_limit_error_false_other_error():
    from claude_agent_sdk import ResultMessage
    from agent_box.agents.claude_code import _is_context_limit_error

    msg = ResultMessage(
        subtype="success", is_error=True, duration_ms=0, duration_api_ms=0,
        num_turns=1, session_id="s1",
        errors=["API Error: Rate limit exceeded"],
    )
    assert _is_context_limit_error(msg) is False


def test_format_recent_rounds():
    from claude_agent_sdk import SessionMessage
    from agent_box.agents.claude_code import _format_recent_rounds

    messages = [
        SessionMessage(
            type="user", uuid="u1", session_id="s1",
            message={"role": "user", "content": [{"type": "text", "text": "create a FastAPI project"}]},
            parent_tool_use_id=None,
        ),
        SessionMessage(
            type="assistant", uuid="a1", session_id="s1",
            message={"role": "assistant", "content": [{"type": "text", "text": "Created main.py with FastAPI app"}]},
            parent_tool_use_id=None,
        ),
        SessionMessage(
            type="user", uuid="u2", session_id="s1",
            message={"role": "user", "content": [{"type": "text", "text": "add a /health endpoint"}]},
            parent_tool_use_id=None,
        ),
        SessionMessage(
            type="assistant", uuid="a2", session_id="s1",
            message={"role": "assistant", "content": [{"type": "text", "text": "Added /health endpoint"}]},
            parent_tool_use_id=None,
        ),
    ]

    result = _format_recent_rounds(messages, n=2)
    assert "add a /health endpoint" in result
    assert "Added /health endpoint" in result
    assert "create a FastAPI project" in result
    assert "自动恢复上下文" in result


def test_format_recent_rounds_empty():
    from agent_box.agents.claude_code import _format_recent_rounds

    assert _format_recent_rounds([], n=2) == ""


@pytest.mark.anyio
async def test_run_triggers_context_limit_recovery(sample_project: ProjectInfo):
    """When ResultMessage has context window limit error, recovery should trigger."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    mock_client = AsyncMock()
    mock_client.query = AsyncMock()
    call_count = {"queries": 0, "receives": 0}

    async def fake_receive():
        call_count["receives"] += 1
        if call_count["receives"] == 1:
            # First call: context limit error
            yield ResultMessage(
                subtype="success", is_error=True, duration_ms=0, duration_api_ms=0,
                num_turns=1, session_id="sess-ctx",
                errors=["The model has reached its context window limit."],
            )
        elif call_count["receives"] == 2:
            # Second call (compact result): success
            yield ResultMessage(
                subtype="success", is_error=False, duration_ms=500, duration_api_ms=400,
                num_turns=1, session_id="sess-ctx", total_cost_usd=0.01, usage=None,
            )
        else:
            # Third call (re-played prompt): success with response
            yield AssistantMessage(content=[TextBlock(text="Recovered response")], model="test")
            yield ResultMessage(
                subtype="success", is_error=False, duration_ms=1000, duration_api_ms=900,
                num_turns=2, session_id="sess-ctx", total_cost_usd=0.02, usage=None,
            )

    mock_client.receive_response = fake_receive

    with patch("agent_box.agents.claude_code.get_session_messages", return_value=[]):
        agent = ClaudeCodeAgent(sample_project)
        agent._client = mock_client
        msgs = [m async for m in agent.run("continue the task")]

    # Should have: recovery notice + response text + result
    texts = [m.text for m in msgs]
    assert any("自动压缩" in t for t in texts)
    assert any("Recovered response" in t for t in texts)

    # Should have queried: original prompt, /compact, then the replay
    assert mock_client.query.call_count == 3
    calls = [c.args[0] for c in mock_client.query.call_args_list]
    assert calls[0] == "continue the task"
    assert calls[1] == "/compact"
    assert "continue the task" in calls[2]


@pytest.mark.anyio
async def test_run_context_limit_compact_fails(sample_project: ProjectInfo):
    """If /compact also fails, should report error to user."""
    from claude_agent_sdk import ResultMessage

    mock_client = AsyncMock()
    mock_client.query = AsyncMock()
    call_count = {"receives": 0}

    async def fake_receive():
        call_count["receives"] += 1
        if call_count["receives"] == 1:
            # First call: context limit error
            yield ResultMessage(
                subtype="success", is_error=True, duration_ms=0, duration_api_ms=0,
                num_turns=1, session_id="sess-ctx",
                errors=["The model has reached its context window limit."],
            )
        else:
            # Compact also fails
            yield ResultMessage(
                subtype="success", is_error=True, duration_ms=0, duration_api_ms=0,
                num_turns=1, session_id="sess-ctx",
                errors=["compact failed"],
            )

    mock_client.receive_response = fake_receive

    agent = ClaudeCodeAgent(sample_project)
    agent._client = mock_client
    msgs = [m async for m in agent.run("test")]

    texts = [m.text for m in msgs]
    assert any("自动压缩" in t for t in texts)
    assert any("压缩失败" in t for t in texts)

