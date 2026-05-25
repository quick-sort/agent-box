"""Router: classifies incoming messages with the Anthropic SDK + tools.

Three tools are exposed to the model:
  - ``create_project(name)``
  - ``switch_project(name)``
  - ``list_projects()``

If the model calls one, we execute it and reply with a confirmation /
listing. Otherwise the message is forwarded to the currently pinned project
(defaulting to ``_default``).
"""

from __future__ import annotations

import logging
import os

from anthropic import AsyncAnthropic
from anthropic.types import ToolUseBlock

from ..models import IncomingMessage
from ..session_manager import DEFAULT_PROJECT_NAME, SessionManager
from .base import BaseRouter, RouteResult

log = logging.getLogger(__name__)


_TOOLS = [
    {
        "name": "create_project",
        "description": "Create a new project workspace and pin it as the active project.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short identifier for the new project (no spaces preferred).",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "switch_project",
        "description": (
            "Switch the active pinned project to the given name. Pass the project "
            "name verbatim — even if it does not appear in the active list. Use "
            "name='_default' to return to the default project."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The project name the user mentioned (verbatim), or '_default'.",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "list_projects",
        "description": "List all projects and indicate which one is currently pinned.",
        "input_schema": {"type": "object", "properties": {}},
    },
]

_SYSTEM = (
    "You are the project-management router for a coding assistant. Decide "
    "whether the user's message is a project-management command, and if so, "
    "call the matching tool. Otherwise reply with the single token: FORWARD.\n\n"
    "MESSAGES THAT ARE PROJECT-MANAGEMENT COMMANDS (call a tool):\n\n"
    "  create_project — start/create/initialize a new project workspace:\n"
    "    - 'new project foo'\n"
    "    - 'create a project called foo'\n"
    "    - 'start a new project foo'\n"
    "    - 'init project foo'\n"
    "    - '新建一个项目 foo'\n"
    "    - '创建一个 foo 项目'\n"
    "    - '开个 foo 项目'\n"
    "    - '搞个新项目叫 foo'\n"
    "    - '加一个项目叫 foo'\n\n"
    "  switch_project — change which existing project is the active one:\n"
    "    - 'switch to foo'\n"
    "    - 'use foo project'\n"
    "    - 'change to foo'\n"
    "    - 'open foo project'\n"
    "    - 'activate foo'\n"
    "    - '切换到 foo'\n"
    "    - '切到 foo 项目'\n"
    "    - '换到 foo'\n"
    "    - '用 foo 项目'\n"
    "    - '打开 foo 项目'\n"
    "    - '回到默认项目'  (→ name='_default')\n\n"
    "  list_projects — see what projects exist or which one is current:\n"
    "    - 'list projects'\n"
    "    - 'show my projects'\n"
    "    - 'what projects do I have'\n"
    "    - 'what is the current project'\n"
    "    - '当前有哪些项目'\n"
    "    - '有哪些项目'\n"
    "    - '列出项目'\n"
    "    - '现在用的是哪个项目'\n"
    "    - '当前项目是什么'\n\n"
    "MESSAGES THAT ARE NOT PROJECT-MANAGEMENT (reply FORWARD, no tool):\n"
    "  - 'fix the bug in main.py'\n"
    "  - 'add a function called foo'      (function, not a project)\n"
    "  - 'create a file foo.py'           (file, not a project)\n"
    "  - 'what does this code do'\n"
    "  - 'run the tests'\n"
    "  - 'commit and push'\n"
    "  - '帮我修一下 main.py 的 bug'\n"
    "  - '新建一个文件 foo.py'             (文件, 不是项目)\n"
    "  - '加一个 foo 函数'                (函数, 不是项目)\n"
    "  - '解释一下这段代码'\n"
    "  - '运行测试'\n\n"
    "When the user names a project that is not in the active list, STILL call "
    "the matching tool with that exact name — do not silently substitute a "
    "different existing project, and do not fall back to FORWARD just because "
    "the name is unknown."
)


class Router(BaseRouter):
    """Pure LLM + tool router using the Anthropic SDK."""

    def __init__(self, sessions: SessionManager) -> None:
        self.sessions = sessions
        self.sessions.ensure_default()
        self._client = AsyncAnthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
            base_url=os.environ.get("ANTHROPIC_BASE_URL") or None,
        )
        model = (
            os.environ.get("ANTHROPIC_SMALL_FAST_MODEL")
            or os.environ.get("ANTHROPIC_DEFAULT_HAIKU_MODEL")
        )
        if not model:
            raise RuntimeError(
                "Router model not configured: set ANTHROPIC_SMALL_FAST_MODEL "
                "or ANTHROPIC_DEFAULT_HAIKU_MODEL."
            )
        self._model = model
        log.info("router model: %s", self._model)

    # ── public API ──

    async def route(self, msg: IncomingMessage) -> RouteResult:
        tool_call = await self._classify(msg.text.strip())
        if tool_call is None:
            return RouteResult(project=self.sessions.get_current())

        inputs = tool_call.input or {}
        name = (inputs.get("name") or "").strip() if isinstance(inputs, dict) else ""

        if tool_call.name == "create_project":
            return self._handle_create(name)
        if tool_call.name == "switch_project":
            return self._handle_switch(name)
        if tool_call.name == "list_projects":
            return self._handle_list()
        return RouteResult(project=self.sessions.get_current())

    # ── helpers ──

    async def _classify(self, text: str) -> ToolUseBlock | None:
        projects = self.sessions.list_all()
        project_lines = "\n".join(f"- {p.name}" for p in projects) or "(none)"
        current = self.sessions.get_current()

        user_block = (
            f"Active projects:\n{project_lines}\n\n"
            f"Currently pinned: {current}\n\n"
            f"User message: {text}"
        )

        try:
            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=256,
                system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
                tools=_TOOLS,
                tool_choice={"type": "auto"},
                messages=[{"role": "user", "content": user_block}],
            )
        except Exception:
            log.exception("router classification failed; defaulting to forward")
            return None

        for block in resp.content:
            if isinstance(block, ToolUseBlock):
                return block
        return None

    def _handle_create(self, name: str) -> RouteResult:
        if not name:
            return RouteResult(reply="❌ Project name required.")
        existed = self.sessions.get(name) is not None
        project = self.sessions.create(name)
        self.sessions.set_current(project.name)
        verb = "Switched to existing" if existed else "Created"
        return RouteResult(reply=f"✅ {verb} project: {project.name}")

    def _handle_switch(self, name: str) -> RouteResult:
        if not name:
            return RouteResult(reply="❌ Project name required.")
        if name == DEFAULT_PROJECT_NAME:
            self.sessions.ensure_default()
            self.sessions.set_current(DEFAULT_PROJECT_NAME)
            return RouteResult(reply=f"🔀 Pinned to: {DEFAULT_PROJECT_NAME}")
        if self.sessions.get(name) is None:
            return RouteResult(reply=f"❌ Unknown project: {name}")
        self.sessions.set_current(name)
        return RouteResult(reply=f"📌 Pinned to project: {name}")

    def _handle_list(self) -> RouteResult:
        projects = self.sessions.list_all()
        current = self.sessions.get_current()
        if not projects:
            return RouteResult(reply="No projects yet.")
        lines = [
            f"{'★ ' if p.name == current else '• '}{p.name} ({p.agent_type})"
            for p in projects
        ]
        return RouteResult(reply=f"Projects ({len(projects)}):\n" + "\n".join(lines))
