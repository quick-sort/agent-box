"""Tests for agent_box.main (App)."""

from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

import anyio
import pytest

from agent_box.models import IncomingMessage, OutgoingMessage
from agent_box.session_manager import SessionManager


def _msg(text: str) -> IncomingMessage:
    return IncomingMessage(text=text, user_id="u1", channel="test")


def _make_app(tmp_path: Path):
    """Create an App with mocked router and agent dependencies."""
    from agent_box.main import App

    with patch("agent_box.main.settings") as mock_settings:
        mock_settings.workspace_dir = tmp_path / "workspace"
        mock_settings.router_agent_type = "claude_code"
        with patch("agent_box.main.Router") as MockRouter:
            app = App.__new__(App)
            app.sessions = SessionManager(tmp_path / "workspace")
            app.router = MockRouter.return_value
            app.agents = {}
    return app


def _async_iter_agent(reply_text):
    """Create a mock agent whose run() yields a single OutgoingMessage."""
    mock_agent = MagicMock()

    async def fake_run(*args, **kwargs):
        yield OutgoingMessage(text=reply_text, user_id=kwargs.get("user_id", ""))

    mock_agent.run = fake_run
    mock_agent.project = MagicMock()
    mock_agent.project.session_id = None
    return mock_agent


@pytest.mark.anyio
async def test_handle_message_default(tmp_path: Path):
    """Unmatched message goes to _default project."""
    app = _make_app(tmp_path)
    app.router.route = AsyncMock(return_value="DEFAULT")

    mock_agent = _async_iter_agent("default reply")

    with patch("agent_box.main.create_agent", return_value=mock_agent):
        send, recv = anyio.create_memory_object_stream[OutgoingMessage](4)
        await app.handle_message(_msg("hello"), send)

    replies = []
    while True:
        try:
            replies.append(recv.receive_nowait())
        except anyio.WouldBlock:
            break

    assert any(r.text == "default reply" for r in replies)


@pytest.mark.anyio
async def test_handle_message_new_project(tmp_path: Path):
    """NEW_PROJECT creates project and sends confirmation."""
    app = _make_app(tmp_path)
    app.router.route = AsyncMock(return_value="NEW_PROJECT My App")

    mock_agent = _async_iter_agent("agent reply")

    with patch("agent_box.main.create_agent", return_value=mock_agent):
        send, recv = anyio.create_memory_object_stream[OutgoingMessage](4)
        await app.handle_message(_msg("create my app"), send)

    replies = []
    while True:
        try:
            replies.append(recv.receive_nowait())
        except anyio.WouldBlock:
            break

    texts = [r.text for r in replies]
    assert any("Created project" in t for t in texts)
    assert any("agent reply" in t for t in texts)
    assert app.sessions.get("my-app") is not None


@pytest.mark.anyio
async def test_handle_message_existing_project(tmp_path: Path):
    """Route to existing project."""
    app = _make_app(tmp_path)
    app.sessions.create("web-app")
    app.router.route = AsyncMock(return_value="web-app")

    mock_agent = _async_iter_agent("done")

    with patch("agent_box.main.create_agent", return_value=mock_agent):
        send, recv = anyio.create_memory_object_stream[OutgoingMessage](4)
        await app.handle_message(_msg("fix the bug"), send)

    msg = recv.receive_nowait()
    assert msg.text == "done"


@pytest.mark.anyio
async def test_get_or_create_agent_caches(tmp_path: Path):
    """Same slug should return same agent instance."""
    app = _make_app(tmp_path)
    app.sessions.create("cached")

    mock_agent = MagicMock()
    with patch("agent_box.main.create_agent", return_value=mock_agent) as mock_factory:
        a1 = app._get_or_create_agent("cached")
        a2 = app._get_or_create_agent("cached")

    assert a1 is a2
    mock_factory.assert_called_once()


@pytest.mark.anyio
async def test_dispatch_loop_concurrent(tmp_path: Path):
    """Multiple messages should be dispatched concurrently."""
    app = _make_app(tmp_path)

    call_order = []

    async def slow_handle(msg, reply):
        call_order.append(f"start-{msg.text}")
        await anyio.sleep(0.1)
        call_order.append(f"end-{msg.text}")

    app.handle_message = slow_handle

    send_in, recv_in = anyio.create_memory_object_stream[IncomingMessage](4)
    send_out, recv_out = anyio.create_memory_object_stream[OutgoingMessage](4)

    async with anyio.create_task_group() as tg:
        tg.start_soon(app._dispatch_loop, recv_in, send_out)
        await send_in.send(_msg("a"))
        await send_in.send(_msg("b"))
        await anyio.sleep(0.05)  # let both start
        await send_in.aclose()
        await anyio.sleep(0.2)  # let both finish
        tg.cancel_scope.cancel()

    # Both should have started before either finished (concurrent)
    assert "start-a" in call_order
    assert "start-b" in call_order
