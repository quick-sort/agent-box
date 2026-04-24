"""Main entry point: wires channels → router → agents."""

from __future__ import annotations

import logging
import sys

import anyio

from .agents import create_agent
from .agents.base import BaseAgent
from .config import settings
from .models import IncomingMessage, OutgoingMessage
from .session_manager import SessionManager
from .router.router import Router

log = logging.getLogger(__name__)


class App:
    def __init__(self) -> None:
        self.sessions = SessionManager(settings.workspace)
        self.router = Router(self.sessions)
        self.agents: dict[str, BaseAgent] = {}

    def _get_or_create_agent(self, slug: str) -> BaseAgent:
        if slug not in self.agents:
            project = self.sessions.get(slug)
            assert project is not None
            self.agents[slug] = create_agent(project.agent_type, project)
        return self.agents[slug]

    async def handle_message(
        self, msg: IncomingMessage, reply: anyio.abc.ObjectSendStream[OutgoingMessage]
    ) -> None:
        if msg.text.strip() == "/list":
            projects = self.sessions.list_all()
            if not projects:
                text = "No projects yet."
            else:
                lines = [f"• {p.slug} — {p.name} ({p.agent_type})" for p in projects]
                text = f"Projects ({len(projects)}):\n" + "\n".join(lines)
            await reply.send(OutgoingMessage(text=text, user_id=msg.user_id))
            return

        slug = await self.router.route(msg)

        if slug == "SWITCH_AUTO":
            await reply.send(OutgoingMessage(text="🔀 Switched to auto routing", user_id=msg.user_id))
            return
        if slug.startswith("SWITCH "):
            name = slug.removeprefix("SWITCH ").strip()
            await reply.send(OutgoingMessage(text=f"📌 Pinned to project: {name}", user_id=msg.user_id))
            return

        if slug.startswith("NEW_PROJECT "):
            name = slug.removeprefix("NEW_PROJECT ").strip()
            project = self.sessions.create(name)
            slug = project.slug
            await reply.send(OutgoingMessage(text=f"✅ Created project: {project.name}", user_id=msg.user_id))

        if slug == "DEFAULT":
            slug = "_default"
            self.sessions.ensure_default()

        agent = self._get_or_create_agent(slug)
        async for out_msg in agent.run(msg.text, user_id=msg.user_id):
            await reply.send(out_msg)
        # Persist session_id if the agent updated it
        self.sessions.update_session_id(slug, agent.project.session_id or "")

    async def run(self, channel_type: str = "weixin") -> None:
        send_out, recv_out = anyio.create_memory_object_stream[OutgoingMessage](16)
        send_in, recv_in = anyio.create_memory_object_stream[IncomingMessage](16)

        if channel_type == "tui":
            from .channels.tui import TuiChannel
            channel = TuiChannel(send_in)
        else:
            from .channels.weixin import WeixinChannel
            channel = WeixinChannel(send_in)

        async with anyio.create_task_group() as tg:
            async def _run_then_cancel() -> None:
                await channel.start()
                tg.cancel_scope.cancel()

            tg.start_soon(_run_then_cancel)
            tg.start_soon(self._dispatch_loop, recv_in, send_out)
            tg.start_soon(channel.send_loop, recv_out)

        # Clean up agent subprocesses
        for agent in list(self.agents.values()):
            try:
                await agent.close()
            except Exception:
                pass

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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    channel = "tui" if "--tui" in sys.argv else "weixin"
    app = App()
    try:
        anyio.run(app.run, channel)
    except KeyboardInterrupt:
        pass
    # Suppress "Event loop is closed" from subprocess GC at shutdown
    from asyncio import base_subprocess
    base_subprocess.BaseSubprocessTransport.__del__ = lambda self: None
