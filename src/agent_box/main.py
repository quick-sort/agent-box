"""Main entry point: wires channels → router → agents."""

from __future__ import annotations

import logging
import sys

import anyio

from .agents import create_agent
from .agents.base import BaseAgent
from .channels.base import BaseChannel
from .config import settings
from .models import IncomingMessage, OutgoingMessage
from .router.router import Router
from .session_manager import SessionManager

log = logging.getLogger(__name__)


class App:
    def __init__(self) -> None:
        self.sessions = SessionManager(settings.workspace_dir)
        self.router = Router(self.sessions)
        self.agents: dict[str, BaseAgent] = {}

    def _get_or_create_agent(self, name: str) -> BaseAgent:
        if name not in self.agents:
            project = self.sessions.get(name)
            assert project is not None, f"unknown project: {name!r}"
            self.agents[name] = create_agent(project.agent_type, project)
        return self.agents[name]

    def _create_channel(self, channel_type: str, send_in: anyio.abc.ObjectSendStream[IncomingMessage]) -> BaseChannel:
        """Instantiate a channel by type name."""
        if channel_type == "tui":
            from .channels.tui import TuiChannel
            return TuiChannel(send_in)
        elif channel_type == "qq":
            from .channels.qq import QQChannel
            return QQChannel(send_in)
        else:
            from .channels.weixin import WeixinChannel
            return WeixinChannel(send_in)

    async def handle_message(
        self, msg: IncomingMessage, reply: anyio.abc.ObjectSendStream[OutgoingMessage]
    ) -> None:
        result = await self.router.route(msg)

        if result.reply is not None:
            await reply.send(OutgoingMessage(text=result.reply, user_id=msg.user_id, channel=msg.channel))
            return

        project_name = result.project or self.sessions.get_current()
        self.sessions.ensure_default()  # always available as a fallback
        agent = self._get_or_create_agent(project_name)
        async for out_msg in agent.run(msg.text, user_id=msg.user_id, channel=msg.channel):
            await reply.send(out_msg)
        self.sessions.update_session_id(project_name, agent.project.session_id or "")

    async def run(self, channel_types: list[str] | None = None) -> None:
        """Run the app with one or more channels simultaneously.

        Each channel gets its own outbound stream. A router task fans out
        outgoing messages to the correct channel based on ``msg.channel``.
        """
        if not channel_types:
            channel_types = ["weixin"]

        send_in, recv_in = anyio.create_memory_object_stream[IncomingMessage](16)
        send_out, recv_out = anyio.create_memory_object_stream[OutgoingMessage](16)

        # Create all channels
        channels: dict[str, BaseChannel] = {}
        for ct in channel_types:
            channels[ct] = self._create_channel(ct, send_in)

        async with anyio.create_task_group() as tg:
            # Start each channel's inbound listener and outbound sender
            for ct, ch in channels.items():
                # Each channel gets a filtered outbound stream
                ch_send, ch_recv = anyio.create_memory_object_stream[OutgoingMessage](16)
                tg.start_soon(ch.start)
                tg.start_soon(ch.send_loop, ch_recv)
                # Store the send stream for routing
                ch._outbound_send = ch_send  # type: ignore[attr-defined]

            # Route outbound messages to correct channel
            tg.start_soon(self._route_outbound, recv_out, channels)

            # Dispatch inbound messages to handler
            tg.start_soon(self._dispatch_loop, recv_in, send_out)

        for agent in list(self.agents.values()):
            try:
                await agent.close()
            except Exception:
                pass

    async def _route_outbound(
        self,
        recv: anyio.abc.ObjectReceiveStream[OutgoingMessage],
        channels: dict[str, BaseChannel],
    ) -> None:
        """Route outgoing messages to the correct channel's send stream."""
        async for msg in recv:
            ch = channels.get(msg.channel)
            if ch is None:
                log.warning("No channel '%s' for outgoing message, dropping", msg.channel)
                continue
            send_stream: anyio.abc.ObjectSendStream[OutgoingMessage] | None = getattr(ch, "_outbound_send", None)
            if send_stream is not None:
                await send_stream.send(msg)
            else:
                log.warning("Channel '%s' has no outbound stream", msg.channel)

    async def _dispatch_loop(
        self,
        recv_in: anyio.abc.ObjectReceiveStream[IncomingMessage],
        send_out: anyio.abc.ObjectSendStream[OutgoingMessage],
    ) -> None:
        try:
            async with anyio.create_task_group() as tg:
                async for msg in recv_in:
                    tg.start_soon(self.handle_message, msg, send_out.clone())
        except Exception:
            pass
        finally:
            await send_out.aclose()


async def _test_router_repl() -> None:
    """Interactive REPL: read a line, run Router.route(), print the result.

    Uses the real workspace, so create_project / switch_project actually
    mutate ``<workspace>/.router/``.
    """
    sessions = SessionManager(settings.workspace_dir)
    router = Router(sessions)

    print(f"[router REPL] workspace={sessions.workspace}")
    print(f"[router REPL] model={router._model}")
    print(f"[router REPL] current={sessions.get_current()}  "
          f"projects={[p.name for p in sessions.list_all()]}")
    print("Type a message, Ctrl-D / Ctrl-C to quit.\n")

    while True:
        try:
            text = await anyio.to_thread.run_sync(lambda: input("❯ "))
        except (EOFError, KeyboardInterrupt):
            print()
            return
        text = text.strip()
        if not text:
            continue

        msg = IncomingMessage(text=text, user_id="repl", channel="test-router")
        result = await router.route(msg)

        if result.reply is not None:
            print(f"  → reply:   {result.reply}")
        if result.project is not None:
            print(f"  → forward: {result.project}")
        print(f"  (current={sessions.get_current()})\n")


def _setup_logging(channel: str) -> None:
    """Configure logging. TUI mode logs to a file only so the screen stays
    clean; other channels log to both file and stderr."""
    from logging.handlers import RotatingFileHandler

    log_dir = settings.config_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "agent-box.log"

    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    file_handler = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(file_handler)

    # Both "tui" and "test-router" want a clean screen — file only.
    if channel not in ("tui", "test-router"):
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        root.addHandler(stream)


def main() -> None:
    if "--test-router" in sys.argv:
        _setup_logging("test-router")
        try:
            anyio.run(_test_router_repl)
        except KeyboardInterrupt:
            pass
        return

    # Parse channel flags: --qq --weixin --tui
    channel_types: list[str] = []
    if "--qq" in sys.argv:
        channel_types.append("qq")
    if "--tui" in sys.argv:
        channel_types.append("tui")
    if "--weixin" in sys.argv or not channel_types:
        channel_types.append("weixin")

    _setup_logging(",".join(channel_types))
    app = App()
    try:
        anyio.run(app.run, channel_types)
    except KeyboardInterrupt:
        pass
    # Suppress "Event loop is closed" from subprocess GC at shutdown
    from asyncio import base_subprocess
    base_subprocess.BaseSubprocessTransport.__del__ = lambda self: None
