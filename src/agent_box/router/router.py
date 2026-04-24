"""Router that uses a configurable agent to classify messages to projects."""

from __future__ import annotations

import logging
import re

from ..agents import create_agent
from ..config import settings
from ..models import IncomingMessage, MessageType, ProjectInfo
from ..session_manager import SessionManager
from .base import BaseRouter

log = logging.getLogger(__name__)

_CMD_NEW = re.compile(r"^/new[_-]project\s+(.+)", re.IGNORECASE)
_CMD_SWITCH = re.compile(r"^/switch\s+(.+)", re.IGNORECASE)


class Router(BaseRouter):
    """Routes messages using a real agent with a .router folder."""

    def __init__(self, sessions: SessionManager) -> None:
        self.sessions = sessions
        self._pinned_slug: str | None = None
        router_project = ProjectInfo(
            slug=".router", name="router", path=str(sessions.router_dir),
            model=settings.router_model,
        )
        self._agent = create_agent(settings.router_agent_type, router_project)

    async def route(self, msg: IncomingMessage) -> str:
        if m := _CMD_NEW.match(msg.text):
            return f"NEW_PROJECT {m.group(1).strip()}"
        if m := _CMD_SWITCH.match(msg.text):
            slug = m.group(1).strip()
            if slug.lower() == "auto":
                self._pinned_slug = None
                return "SWITCH_AUTO"
            if self.sessions.get(slug):
                self._pinned_slug = slug
                return f"SWITCH {slug}"
            return "DEFAULT"

        # If pinned to a project, skip AI routing
        if self._pinned_slug and self.sessions.get(self._pinned_slug):
            return self._pinned_slug

        project_list = self.sessions.list_all()
        if not project_list:
            return "DEFAULT"

        project_desc = "\n".join(
            f"- {p.slug}: {p.name}" + (f" — {p.description}" if p.description else "")
            for p in project_list
        )
        prompt = (
            f"[SYSTEM] {settings.router_system_prompt}\n\n"
            f"Active projects:\n{project_desc}\n\n"
            f"User message: {msg.text}\n\n"
            "Reply with ONLY the slug, NEW_PROJECT <name>, or DEFAULT."
        )

        result_text = ""
        async for out_msg in self._agent.run(prompt):
            if out_msg.type == MessageType.text:
                result_text += out_msg.text
        answer = result_text.strip().split("\n")[0].strip()
        log.info("router decision: %r → %r", msg.text[:60], answer)

        if answer.startswith("NEW_PROJECT "):
            return answer
        if self.sessions.get(answer):
            return answer
        return "DEFAULT"
