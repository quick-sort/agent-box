"""Main entry point: wires channels → router → agents."""

from __future__ import annotations

import logging
import math
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

_STOP_COMMANDS = frozenset({"stop", "停止", "停下"})
_ActiveTurn = tuple[str, BaseAgent, anyio.CancelScope]


def _merge_messages(msgs: list[IncomingMessage]) -> IncomingMessage:
    """Coalesce queued messages into one request, keeping the first's metadata."""
    if len(msgs) == 1:
        return msgs[0]
    first = msgs[0]
    return IncomingMessage(
        text="\n".join(m.text for m in msgs if m.text),
        user_id=first.user_id,
        channel=first.channel,
        conversation_id=first.conversation_id,
        raw=first.raw,
    )


class App:
    def __init__(self) -> None:
        self.sessions = SessionManager(settings.workspace_dir)
        self.router = Router(self.sessions)
        self.agents: dict[tuple[str, str], BaseAgent] = {}
        self._agent_locks: dict[str, anyio.Lock] = {}
        self._active_turns: dict[tuple[str, str], _ActiveTurn] = {}
        self.channel_types: list[str] = []

    def _get_project_lock(self, name: str) -> anyio.Lock:
        # Some tests construct App without calling __init__, so initialize the
        # lock registry lazily as well as in the normal constructor.
        locks = getattr(self, "_agent_locks", None)
        if locks is None:
            locks = self._agent_locks = {}
        return locks.setdefault(name, anyio.Lock())

    def _get_active_turns(self) -> dict[tuple[str, str], _ActiveTurn]:
        # Tests may construct App via __new__, so keep this registry lazy too.
        turns = getattr(self, "_active_turns", None)
        if turns is None:
            turns = self._active_turns = {}
        return turns

    def _get_or_create_agent(self, name: str, conversation_id: str | None = None) -> BaseAgent:
        """Get or create the agent for a (project, conversation) pair.

        Each conversation gets its own agent (and thus its own Claude Code
        session), so multiple groups / single chats pinned to the same project
        no longer share context or clobber each other's pending-permission
        state.
        """
        key = (name, conversation_id or "")
        if key not in self.agents:
            project = self.sessions.get(name)
            assert project is not None, f"unknown project: {name!r}"
            self.agents[key] = create_agent(project.agent_type, project)
        return self.agents[key]

    async def _close_agent(self, name: str) -> None:
        """Close and evict every agent for *name* under its serialization lock.

        A project may have one agent per conversation (``(project, conversation)``
        keys), so a project reset must evict all of them.
        """
        async with self._get_project_lock(name):
            keys = [k for k in self.agents if k[0] == name]
            for key in keys:
                agent = self.agents.pop(key, None)
                if agent is not None:
                    await agent.close()

    async def _handle_stop_command(
        self,
        msg: IncomingMessage,
        reply: anyio.abc.ObjectSendStream[OutgoingMessage],
    ) -> None:
        """Cancel this channel/user's active turn without waiting for its project lock."""
        key = (msg.channel, msg.user_id)
        active = self._get_active_turns().pop(key, None)
        if active is None:
            text = "ℹ️ 当前没有正在执行的任务。"
        else:
            project_name, agent, cancel_scope = active
            log.info(
                "stop command: user=%s channel=%s project=%s",
                msg.user_id, msg.channel, project_name,
            )
            # Cancel the handler first so a turn still connecting/starting
            # cannot proceed before its backend object becomes interruptible.
            cancel_scope.cancel()
            await agent.cancel()
            text = "⏹️ 已停止当前任务。"
        await reply.send(OutgoingMessage(text=text, user_id=msg.user_id, channel=msg.channel))

    def _create_channel(self, channel_type: str, send_in: anyio.abc.ObjectSendStream[IncomingMessage]) -> BaseChannel:
        """Instantiate a channel by type name."""
        if channel_type == "tui":
            from .channels.tui import TuiChannel
            return TuiChannel(send_in)
        elif channel_type == "qq":
            from .channels.qq import QQChannel
            return QQChannel(send_in)
        elif channel_type == "wecom":
            from .channels.wecom import WecomChannel
            return WecomChannel(send_in)
        else:
            from .channels.weixin import WeixinChannel
            return WeixinChannel(send_in)

    async def handle_message(
        self, msg: IncomingMessage, reply: anyio.abc.ObjectSendStream[OutgoingMessage]
    ) -> None:
        log.info(
            "handle_message entry: user=%s channel=%s text_preview=%r",
            msg.user_id, msg.channel, (msg.text or "")[:200],
        )
        if (msg.text or "").strip().casefold() in _STOP_COMMANDS:
            await self._handle_stop_command(msg, reply)
            return

        result = await self.router.route(msg)

        if result.reply is not None:
            log.info("router replied directly (no agent call): %r", (result.reply or "")[:200])
            await reply.send(OutgoingMessage(
                text=result.reply, user_id=msg.user_id, channel=msg.channel,
                conversation_id=msg.conversation_id,
            ))
            reset_project = result.reset_project
            if reset_project is None and result.reset_agent:
                reset_project = self.sessions.get_current()
            if reset_project is not None:
                await self._close_agent(reset_project)
            return

        project_name = result.project or self.sessions.get_current()
        self.sessions.ensure_default()  # always available as a fallback
        agent_key = (project_name, msg.conversation_id or "")
        agent_at_arrival = self.agents.get(agent_key)
        permission_pending_at_arrival = (
            getattr(agent_at_arrival, "has_pending_question", False) is True
        )

        # A persistent agent process may only handle one turn at a time. Keep
        # same-project messages ordered while preserving concurrency across
        # different projects.
        async with self._get_project_lock(project_name):
            agent = self._get_or_create_agent(project_name, msg.conversation_id)
            has_pending = getattr(agent, "has_pending_question", False) is True
            log.info(
                "dispatching to agent: project=%s agent_type=%s has_pending_question=%s",
                project_name, type(agent).__name__, has_pending,
            )
            # A message that entered the queue before the permission prompt
            # existed must never be interpreted as approval. Likewise, a
            # duplicate reply must not become a new coding request after a
            # previous reply has already resolved the prompt.
            if has_pending and not permission_pending_at_arrival:
                await reply.send(
                    OutgoingMessage(
                        text=(
                            "⚠️ This message arrived before the agent requested permission "
                            "and was not used as an answer. Please reply to the permission prompt."
                        ),
                        user_id=msg.user_id,
                        channel=msg.channel,
                        conversation_id=msg.conversation_id,
                    )
                )
                return
            if permission_pending_at_arrival and not has_pending:
                await reply.send(
                    OutgoingMessage(
                        text="⚠️ The permission request was already answered; this reply was ignored.",
                        user_id=msg.user_id,
                        channel=msg.channel,
                        conversation_id=msg.conversation_id,
                    )
                )
                return

            key = (msg.channel, msg.user_id)
            completed = False
            with anyio.CancelScope() as cancel_scope:
                turn: _ActiveTurn = (project_name, agent, cancel_scope)
                self._get_active_turns()[key] = turn
                try:
                    async for out_msg in agent.run(
                        msg.text,
                        user_id=msg.user_id,
                        channel=msg.channel,
                    ):
                        # A stop command removes this exact turn before awaiting
                        # backend cancellation, so late stream events are dropped.
                        if self._get_active_turns().get(key) is not turn:
                            continue
                        # 回填会话 id，使回复回到正确的会话（群聊=群 chatid，单聊=user_id）。
                        out_msg.conversation_id = msg.conversation_id
                        if (
                            out_msg.text
                            and out_msg.type.value == "text"
                            and self.sessions.get_current() != project_name
                        ):
                            out_msg = OutgoingMessage(
                                text=f"[{project_name}] {out_msg.text}",
                                user_id=out_msg.user_id,
                                channel=out_msg.channel,
                                type=out_msg.type,
                                data=out_msg.data,
                                conversation_id=msg.conversation_id,
                            )
                        await reply.send(out_msg)
                    completed = True
                finally:
                    # ACP sessions are assigned during lazy startup. Persist even
                    # when a turn fails or pauses for a permission reply.
                    self.sessions.update_session_id(project_name, agent.project.session_id or "")
                    has_pending = getattr(agent, "has_pending_question", False) is True
                    if not (completed and has_pending):
                        turns = self._get_active_turns()
                        if turns.get(key) is turn:
                            turns.pop(key)
        log.info("handle_message done: project=%s", project_name)

    async def run(self, channel_types: list[str] | None = None) -> None:
        """Run the app with one or more channels simultaneously.

        Each channel gets its own outbound stream. A router task fans out
        outgoing messages to the correct channel based on ``msg.channel``.
        """
        if not channel_types:
            channel_types = ["weixin"]

        self.channel_types = channel_types

        # Enable wecom_mcp tool when wecom channel is active
        if "wecom" in channel_types:
            from .tools.wecom_mcp import set_wecom_mcp_enabled
            set_wecom_mcp_enabled(True)

        send_in, recv_in = anyio.create_memory_object_stream[IncomingMessage](16)
        send_out, recv_out = anyio.create_memory_object_stream[OutgoingMessage](16)

        # Create all channels
        channels: dict[str, BaseChannel] = {}
        for ct in channel_types:
            channels[ct] = self._create_channel(ct, send_in)

        try:
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
        finally:
            # Long-lived ACP subprocesses must be released even when a channel
            # task crashes or the application is cancelled.
            for key, agent in list(self.agents.items()):
                try:
                    await agent.close()
                except Exception:
                    log.exception("failed to close agent for %s", key)
            self.agents.clear()

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
        async def _safe_handle(msg: IncomingMessage, reply: anyio.abc.ObjectSendStream[OutgoingMessage]) -> None:
            try:
                await self.handle_message(msg, reply)
            except Exception as exc:
                log.exception("handle_message failed for user=%s channel=%s", msg.user_id, msg.channel)
                try:
                    await reply.send(OutgoingMessage(
                        text=f"❌ 系统错误：{exc}",
                        user_id=msg.user_id,
                        channel=msg.channel,
                        conversation_id=msg.conversation_id,
                    ))
                except Exception:
                    log.debug(
                        "failed to send error reply for user=%s channel=%s",
                        msg.user_id,
                        msg.channel,
                        exc_info=True,
                    )

        # Per-conversation FIFO queues. Each conversation gets its own worker so
        # messages are handled in arrival order within a chat, while distinct
        # conversations stay concurrent.
        queues: dict[str, anyio.abc.ObjectSendStream[IncomingMessage]] = {}

        async def _conversation_worker(
            recv: anyio.abc.ObjectReceiveStream[IncomingMessage],
        ) -> None:
            reply = send_out.clone()
            async for first in recv:
                batch = [first]
                # Coalesce any messages that queued up during the previous turn
                # into a single request so rapid follow-ups aren't run one by one.
                while True:
                    try:
                        batch.append(recv.receive_nowait())
                    except (anyio.WouldBlock, anyio.EndOfStream):
                        break
                await _safe_handle(_merge_messages(batch), reply)

        try:
            async with anyio.create_task_group() as tg:
                async for msg in recv_in:
                    log.info(
                        "dispatch_loop received message: user=%s channel=%s text_preview=%r",
                        msg.user_id, msg.channel, (msg.text or "")[:200],
                    )
                    # Stop commands bypass the FIFO queue so they can cancel the
                    # in-flight turn immediately instead of waiting behind it.
                    if (msg.text or "").strip().casefold() in _STOP_COMMANDS:
                        tg.start_soon(_safe_handle, msg, send_out.clone())
                        continue

                    key = msg.conversation_id or msg.user_id
                    queue = queues.get(key)
                    if queue is None:
                        q_send, q_recv = anyio.create_memory_object_stream[IncomingMessage](
                            max_buffer_size=math.inf
                        )
                        queues[key] = q_send
                        tg.start_soon(_conversation_worker, q_recv)
                        queue = q_send
                    await queue.send(msg)

                # recv_in closed — close the queues so workers drain and exit.
                for q_send in queues.values():
                    await q_send.aclose()
        except Exception:
            log.exception("dispatch loop crashed")
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
    if "--help" in sys.argv or "-h" in sys.argv:
        print("""agent-box — IM → Router → Agent pipeline for managing coding projects via chat

Usage: agent-box [OPTIONS]

Channel options (at least one required, defaults to --weixin):
  --weixin          Enable WeChat (微信) channel
  --wecom           Enable WeCom (企业微信) WebSocket channel
  --qq              Enable QQ Bot channel
  --tui             Enable terminal UI channel (for local testing)

Multiple channels can be enabled simultaneously:
  agent-box --wecom --tui

Other options:
  --test-router     Launch interactive router REPL for testing
  -h, --help        Show this help message

Environment variables (see sample.env):
  WECOM_BOT_ID, WECOM_SECRET         WeCom bot credentials
  QQBOT_APP_ID, QQBOT_CLIENT_SECRET  QQ bot credentials
  WEIXIN_ACCOUNT_ID                  WeChat account ID
  ANTHROPIC_AUTH_TOKEN                Anthropic API key
""")
        return

    if "--test-router" in sys.argv:
        _setup_logging("test-router")
        try:
            anyio.run(_test_router_repl)
        except KeyboardInterrupt:
            pass
        return

    # Parse channel flags: --qq --weixin --tui --wecom
    channel_types: list[str] = []
    if "--qq" in sys.argv:
        channel_types.append("qq")
    if "--tui" in sys.argv:
        channel_types.append("tui")
    if "--wecom" in sys.argv:
        channel_types.append("wecom")
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
