"""Tests for agent_box.main (App)."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest

from agent_box.models import IncomingMessage, OutgoingMessage
from agent_box.router.base import RouteResult
from agent_box.session_manager import SessionManager


def _msg(text: str) -> IncomingMessage:
    return IncomingMessage(text=text, user_id="u1", channel="test")


def _make_app(tmp_path: Path):
    """Create an App with mocked router and agent dependencies."""
    from agent_box.main import App

    with patch("agent_box.main.settings") as mock_settings:
        mock_settings.workspace_dir = tmp_path / "workspace"
        with patch("agent_box.main.Router") as MockRouter:
            app = App.__new__(App)
            app.sessions = SessionManager(tmp_path / "workspace")
            app.router = MockRouter.return_value
            app.agents = {}
    return app


def _async_iter_agent(reply_text):
    """Mock agent whose run() yields one OutgoingMessage."""
    mock_agent = MagicMock()

    async def fake_run(*args, **kwargs):
        yield OutgoingMessage(text=reply_text, user_id=kwargs.get("user_id", ""))

    mock_agent.run = fake_run
    mock_agent.project = MagicMock()
    mock_agent.project.session_id = None
    return mock_agent


@pytest.mark.anyio
async def test_handle_message_forwards_to_default(tmp_path: Path):
    """When router has no command, message is forwarded to pinned (default) project."""
    app = _make_app(tmp_path)
    app.router.route = AsyncMock(return_value=RouteResult(project="_default"))

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
async def test_handle_message_router_reply_short_circuits(tmp_path: Path):
    """If router returns a reply, App sends it and does not forward."""
    app = _make_app(tmp_path)
    app.router.route = AsyncMock(return_value=RouteResult(reply="✅ Created project: foo"))

    with patch("agent_box.main.create_agent") as mock_factory:
        send, recv = anyio.create_memory_object_stream[OutgoingMessage](4)
        await app.handle_message(_msg("create a new project called foo"), send)

    msg = recv.receive_nowait()
    assert "Created project" in msg.text
    mock_factory.assert_not_called()


@pytest.mark.anyio
async def test_handle_message_forwards_to_pinned(tmp_path: Path):
    """Forward to the project name returned by the router."""
    app = _make_app(tmp_path)
    app.sessions.create("web-app")
    app.sessions.set_current("web-app")
    app.router.route = AsyncMock(return_value=RouteResult(project="web-app"))

    mock_agent = _async_iter_agent("done")

    with patch("agent_box.main.create_agent", return_value=mock_agent):
        send, recv = anyio.create_memory_object_stream[OutgoingMessage](4)
        await app.handle_message(_msg("fix the bug"), send)

    msg = recv.receive_nowait()
    assert msg.text == "done"


@pytest.mark.anyio
async def test_get_or_create_agent_caches(tmp_path: Path):
    """Same project name should return same agent instance."""
    app = _make_app(tmp_path)
    app.sessions.create("cached")

    mock_agent = MagicMock()
    with patch("agent_box.main.create_agent", return_value=mock_agent) as mock_factory:
        a1 = app._get_or_create_agent("cached")
        a2 = app._get_or_create_agent("cached")

    assert a1 is a2
    mock_factory.assert_called_once()


@pytest.mark.anyio
async def test_dispatch_loop_merges_queued_messages(tmp_path: Path):
    """Messages queued during a turn are coalesced into one request."""
    app = _make_app(tmp_path)

    release = anyio.Event()
    received: list[str] = []

    async def handle(msg, reply):
        received.append(msg.text)
        if msg.text == "first":
            await release.wait()

    app.handle_message = handle

    send_in, recv_in = anyio.create_memory_object_stream[IncomingMessage](4)
    send_out, _recv_out = anyio.create_memory_object_stream[OutgoingMessage](4)

    async with anyio.create_task_group() as tg:
        tg.start_soon(app._dispatch_loop, recv_in, send_out)
        await send_in.send(_msg("first"))
        await anyio.sleep(0.05)  # worker picks up "first" and blocks on release
        await send_in.send(_msg("second"))
        await send_in.send(_msg("third"))
        await anyio.sleep(0.1)  # "second"/"third" enqueue while "first" runs
        release.set()
        await anyio.sleep(0.2)  # worker drains + merges the queued messages
        assert received == ["first", "second\nthird"]
        await send_in.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_dispatch_loop_parallel_across_conversations(tmp_path: Path):
    """Messages in different conversations are handled concurrently."""
    app = _make_app(tmp_path)

    gate = anyio.Event()
    entered: list[str] = []

    async def handle(msg, reply):
        entered.append(msg.text)
        await gate.wait()

    app.handle_message = handle

    send_in, recv_in = anyio.create_memory_object_stream[IncomingMessage](4)
    send_out, _recv_out = anyio.create_memory_object_stream[OutgoingMessage](4)

    async with anyio.create_task_group() as tg:
        tg.start_soon(app._dispatch_loop, recv_in, send_out)
        await send_in.send(IncomingMessage(text="a", user_id="u1", channel="test"))
        await send_in.send(IncomingMessage(text="b", user_id="u2", channel="test"))
        await anyio.sleep(0.1)
        # Both workers must have entered their handler (they run in parallel).
        assert set(entered) == {"a", "b"}
        gate.set()
        await send_in.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_dispatch_loop_stop_bypasses_queue(tmp_path: Path):
    """A stop command is dispatched immediately, not queued behind a running turn."""
    app = _make_app(tmp_path)

    release = anyio.Event()
    stop_called = anyio.Event()

    async def handle(msg, reply):
        if (msg.text or "").strip().casefold() == "stop":
            stop_called.set()
            return
        await release.wait()

    app.handle_message = handle

    send_in, recv_in = anyio.create_memory_object_stream[IncomingMessage](4)
    send_out, _recv_out = anyio.create_memory_object_stream[OutgoingMessage](4)

    async with anyio.create_task_group() as tg:
        tg.start_soon(app._dispatch_loop, recv_in, send_out)
        await send_in.send(_msg("task"))
        await anyio.sleep(0.05)
        await send_in.send(_msg("stop"))
        await anyio.sleep(0.05)
        # "stop" must be handled even though "task" is still blocking the worker.
        assert stop_called.is_set()
        release.set()
        await send_in.aclose()
        tg.cancel_scope.cancel()


# ── Project tag when user switches away ──


@pytest.mark.anyio
async def test_handle_message_tags_when_project_switched(tmp_path: Path):
    """When user has switched to another project, agent messages get tagged."""
    app = _make_app(tmp_path)
    app.sessions.create("proj-a")
    app.sessions.set_current("proj-a")
    app.router.route = AsyncMock(return_value=RouteResult(project="proj-a"))

    mock_agent = _async_iter_agent("done")

    with patch("agent_box.main.create_agent", return_value=mock_agent):
        send, recv = anyio.create_memory_object_stream[OutgoingMessage](4)
        # Simulate: user switched to proj-b while proj-a is still running
        app.sessions.create("proj-b")
        app.sessions.set_current("proj-b")
        await app.handle_message(_msg("fix the bug"), send)

    msg = recv.receive_nowait()
    assert msg.text == "[proj-a] done"


@pytest.mark.anyio
async def test_handle_message_no_tag_when_same_project(tmp_path: Path):
    """When current project matches, no tag is added."""
    app = _make_app(tmp_path)
    app.sessions.create("proj-a")
    app.sessions.set_current("proj-a")
    app.router.route = AsyncMock(return_value=RouteResult(project="proj-a"))

    mock_agent = _async_iter_agent("done")

    with patch("agent_box.main.create_agent", return_value=mock_agent):
        send, recv = anyio.create_memory_object_stream[OutgoingMessage](4)
        await app.handle_message(_msg("fix the bug"), send)

    msg = recv.receive_nowait()
    assert msg.text == "done"


# ── Channel specs (multi-instance) ──


def test_create_channel_wecom_spec(tmp_path: Path):
    from agent_box.channels.wecom import WecomChannel
    from agent_box.config import WecomBotConfig
    from agent_box.models import ChannelSpec

    app = _make_app(tmp_path)
    config = WecomBotConfig(name="prod", bot_id="b", secret="s")
    spec = ChannelSpec(type="wecom", instance_id="wecom:prod", config=config)

    with patch("agent_box.channels.wecom.settings") as mock_settings:
        mock_settings.config_dir = tmp_path
        send_in, _ = anyio.create_memory_object_stream[IncomingMessage](4)
        ch = app._create_channel(spec, send_in)

    assert isinstance(ch, WecomChannel)
    assert ch.channel_id == "wecom:prod"


def test_create_channel_qq_still_works(tmp_path: Path):
    from agent_box.channels.qq import QQChannel
    from agent_box.models import ChannelSpec

    app = _make_app(tmp_path)
    send_in, _ = anyio.create_memory_object_stream[IncomingMessage](4)
    ch = app._create_channel(ChannelSpec(type="qq", instance_id="qq"), send_in)
    assert isinstance(ch, QQChannel)


@pytest.mark.anyio
async def test_route_outbound_dispatches_by_instance_id(tmp_path: Path):
    """Outgoing messages are routed to the channel matching msg.channel."""
    app = _make_app(tmp_path)

    send_a, recv_a = anyio.create_memory_object_stream[OutgoingMessage](4)
    send_b, recv_b = anyio.create_memory_object_stream[OutgoingMessage](4)

    ch_a = MagicMock()
    ch_a._outbound_send = send_a
    ch_b = MagicMock()
    ch_b._outbound_send = send_b
    channels = {"wecom:prod": ch_a, "wecom:test": ch_b}

    send_out, recv_out = anyio.create_memory_object_stream[OutgoingMessage](4)
    async with anyio.create_task_group() as tg:
        tg.start_soon(app._route_outbound, recv_out, channels)
        await send_out.send(OutgoingMessage(text="hello", user_id="u1", channel="wecom:test"))
        await send_out.aclose()

    msg = recv_b.receive_nowait()
    assert msg.text == "hello"
    with pytest.raises((anyio.WouldBlock, anyio.EndOfStream)):
        recv_a.receive_nowait()
