"""Tests for per-conversation session isolation.

The old design cached one agent per *project*. With multiple conversations
pinned to the same project (multiple groups / single chats → one project),
every conversation shared a single Claude Code session, so context leaked
across conversations and pending-permission state got clobbered.

The fix keys agents by ``(project, conversation_id)`` and moves the
resume ``session_id`` off the shared ``ProjectInfo`` onto the agent
instance (each agent = one conversation = one session).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_box.models import IncomingMessage, OutgoingMessage

# ── ClaudeCodeAgent session is instance-scoped ──


@pytest.mark.anyio
async def test_session_id_not_written_to_shared_project(sample_project):
    """The agent must track its session on the instance, not on ProjectInfo."""
    from claude_agent_sdk import ResultMessage

    from agent_box.agents.claude_code import ClaudeCodeAgent

    mock_client = AsyncMock()
    mock_client.query = AsyncMock()

    async def fake_receive():
        yield ResultMessage(
            subtype="result", is_error=False, duration_ms=1, duration_api_ms=1,
            num_turns=1, total_cost_usd=0.0, usage=None, session_id="sess-1",
        )

    mock_client.receive_response = fake_receive

    agent = ClaudeCodeAgent(sample_project)
    agent._client = mock_client
    [m async for m in agent.run("hi")]

    # Session id lives on the agent instance, NOT the shared project object.
    assert agent._session_id == "sess-1"
    assert sample_project.session_id is None


@pytest.mark.anyio
async def test_two_agents_keep_separate_sessions(sample_project):
    """Two agents on the same project must resume independent sessions."""
    from agent_box.agents.claude_code import ClaudeCodeAgent

    a1 = ClaudeCodeAgent(sample_project)
    a2 = ClaudeCodeAgent(sample_project)

    a1._session_id = "sess-A"
    a2._session_id = "sess-B"

    opts1 = a1._build_options()
    opts2 = a2._build_options()

    assert opts1.resume == "sess-A"
    assert opts2.resume == "sess-B"


# ── App agent cache is keyed by (project, conversation_id) ──


def _make_app(tmp_path):
    from agent_box.main import App
    from agent_box.session_manager import SessionManager

    with patch("agent_box.main.settings") as mock_settings:
        mock_settings.workspace_dir = tmp_path / "workspace"
        with patch("agent_box.main.Router") as MockRouter:
            app = App.__new__(App)
            app.sessions = SessionManager(tmp_path / "workspace")
            app.router = MockRouter.return_value
            app.agents = {}
    return app


def test_get_or_create_agent_separates_conversations(tmp_path):
    """Same project, different conversation_id → different agent instances."""
    app = _make_app(tmp_path)
    app.sessions.create("proj")

    def _new_agent(*a, **k):
        return MagicMock()

    with patch("agent_box.main.create_agent", side_effect=_new_agent) as factory:
        a1 = app._get_or_create_agent("proj", "group-A")
        a2 = app._get_or_create_agent("proj", "group-B")
        a3 = app._get_or_create_agent("proj", "group-A")  # same key → same agent

    assert a1 is not a2
    assert a1 is a3
    assert factory.call_count == 2


def test_get_or_create_agent_same_conversation_caches(tmp_path):
    """Same (project, conversation) returns the same agent instance."""
    app = _make_app(tmp_path)
    app.sessions.create("proj")

    mock_agent = MagicMock()
    with patch("agent_box.main.create_agent", return_value=mock_agent):
        a1 = app._get_or_create_agent("proj", "conv-1")
        a2 = app._get_or_create_agent("proj", "conv-1")

    assert a1 is a2


# ── handle_message passes conversation_id through ──


@pytest.mark.anyio
async def test_handle_message_tags_and_passes_conversation(tmp_path):
    """Outgoing messages from the agent carry the inbound conversation_id."""
    from agent_box.router.base import RouteResult

    app = _make_app(tmp_path)
    app.sessions.create("proj")
    app.router.route = AsyncMock(return_value=RouteResult(project="proj"))

    mock_agent = MagicMock()

    async def fake_run(prompt, user_id="", channel="", conversation_id=None):
        yield OutgoingMessage(
            text="reply",
            user_id=user_id,
            channel=channel,
            conversation_id=conversation_id,
        )

    mock_agent.run = fake_run
    mock_agent.project = MagicMock()
    mock_agent.project.session_id = None

    with patch("agent_box.main.create_agent", return_value=mock_agent):
        send, recv = __import__("anyio").create_memory_object_stream[OutgoingMessage](4)
        await app.handle_message(
            IncomingMessage(
                text="hello", user_id="sender", channel="wecom",
                conversation_id="group-9",
            ),
            send,
        )

    msg = recv.receive_nowait()
    assert msg.conversation_id == "group-9"
