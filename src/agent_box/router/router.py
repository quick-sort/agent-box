"""Router: classifies incoming messages with the Anthropic SDK + tools.

Project-management tools are exposed to the model, including:
  - ``create_project(name)``
  - ``switch_project(name)``
  - ``list_projects()``
  - ``list_models()``
  - ``switch_model(model)``

If the model calls one, we execute it and reply with a confirmation /
listing. Otherwise the message is forwarded to the currently pinned project
(defaulting to ``_default``).

A fast regex-based pre-check runs before the LLM call for common patterns
like "switch to foo" or "list projects", saving latency and API cost.
"""

from __future__ import annotations

import logging
import os
import re

from anthropic import AsyncAnthropic
from anthropic.types import ToolUseBlock

from ..agents import get_agent_definition
from ..config import settings
from ..models import IncomingMessage
from ..session_manager import DEFAULT_PROJECT_NAME, SessionManager
from .base import BaseRouter, RouteResult

log = logging.getLogger(__name__)


# ── Regex fast-path shortcuts ──
# Compact keyword patterns (no spaces) that act as shortcuts.
# Distinct from natural language so they never conflict with chat.
# Usage: "switchto xxx", "newproject xxx", "listprojects"

_FAST_PATTERNS: list[tuple[re.Pattern[str], str, int]] = [
    (re.compile(r"^listprojects$", re.IGNORECASE), "list_projects", 0),
    (re.compile(r"^newproject\s+(.+)$", re.IGNORECASE), "create_project", 1),
    (re.compile(r"^switchto\s+default$", re.IGNORECASE), "switch_project", 0),
    (re.compile(r"^switchto\s+(.+)$", re.IGNORECASE), "switch_project", 1),
    (re.compile(r"^rmproject\s+(.+)$", re.IGNORECASE), "delete_project", 1),
    (re.compile(r"^switchmodel\s+(.+)$", re.IGNORECASE), "switch_model", 1),
]


def _fast_classify(text: str) -> tuple[str, str] | None:
    """Try to match a project-management command via regex.

    Returns ``(tool_name, extracted_arg)`` or ``None`` if no match.
    For tools without args (like ``list_projects``), the arg is empty string.
    """
    t = text.strip()
    for pattern, tool_name, group_idx in _FAST_PATTERNS:
        m = pattern.search(t)
        if m:
            arg = m.group(group_idx).strip() if group_idx > 0 else ""
            return (tool_name, arg)
    return None


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
                "agent": {
                    "type": "string",
                    "description": (
                        "Optional agent backend. Omit it to use the configured default."
                    ),
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
    {
        "name": "list_models",
        "description": "List all available configured models.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "switch_model",
        "description": "Switch the AI model for the currently pinned project.",
        "input_schema": {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "description": "Model name to use (e.g., sonnet, opus, haiku, or any model identifier).",
                },
            },
            "required": ["model"],
        },
    },
    {
        "name": "delete_project",
        "description": "Delete a project and its folder permanently.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The project name to delete.",
                },
            },
            "required": ["name"],
        },
    },
]

_SYSTEM_TEMPLATE = """You are the project-management router for a coding assistant. Decide whether the user's message is a project-management command, and if so, call the matching tool. Otherwise reply with the single token: FORWARD.

AVAILABLE MODELS:
{available_models}

MESSAGES THAT ARE PROJECT-MANAGEMENT COMMANDS (call a tool):

  create_project — start/create/initialize a new project workspace:
    - 'new project foo'
    - 'create a project called foo'
    - 'start a new project foo'
    - 'init project foo'
    - '新建一个项目 foo'
    - '创建一个 foo 项目'
    - '开个 foo 项目'
    - '搞个新项目叫 foo'
    - '加一个项目叫 foo'\n\n"
  switch_project — change which existing project is the active one.
  ONLY trigger when the message EXPLICITLY pairs a switch/change word (切换 / 切到 /
  switch to / change project to) with a project name. Bare 用(use) / 打开(open) /
  换(change) + a noun is NOT a switch on its own; if the noun is followed by a real
  task it is FORWARD:
    - 'switch to foo'
    - 'change project to foo'
    - '切换到 foo'
    - '切到 foo 项目'
    - '切换项目到 foo'
    - '回到默认项目'  (→ name='_default')\n\n"
  list_projects — see what projects exist or which one is current:
    - 'list projects'
    - 'show my projects'
    - 'what projects do I have'
    - 'what is the current project'
    - '当前有哪些项目'
    - '有哪些项目'
    - '列出项目'
    - '现在用的是哪个项目'
    - '当前项目是什么'\n\n"
  list_models — list all available configured models:
    - 'list models'
    - 'show available models'
    - '有哪些模型'
    - '列出模型'\n\n"
  switch_model — change the AI model for the current project:
    - 'switch to sonnet'
    - 'use opus model'
    - 'change model to haiku'
    - '切换到 sonnet 模型'
    - '用 haiku 模型'
    - '换个模型'
    - '切换模型'
    - '换成 opus'\n\n"
  delete_project — permanently delete a project and its folder:
    - 'delete project foo'
    - 'remove project foo'
    - '删除项目 foo'
    - '移除项目 foo'
    - '删掉 foo 项目'\n\n"
MESSAGES THAT ARE NOT PROJECT-MANAGEMENT (reply FORWARD, no tool):
  - 'fix the bug in main.py'
  - 'add a function called foo'      (function, not a project)
  - 'create a file foo.py'           (file, not a project)
  - 'what does this code do'
  - 'run the tests'
  - 'commit and push'
  - '帮我修一下 main.py 的 bug'
  - '新建一个文件 foo.py'             (文件, 不是项目)
  - '加一个 foo 函数'                (函数, 不是项目)
  - '解释一下这段代码'
  - '运行测试'
  - '用 pandas 处理这个数据'          (用 + 任务, 不是切项目)
  - '打开这个文件看一下'              (打开 + 文件, 不是切项目)
  - '换个写法'                       (换 + 写法, 不是切项目)\n\n"
When the user names a project that is not in the active list, STILL call the matching tool with that exact name — do not silently substitute a different existing project, and do not fall back to FORWARD just because the name is unknown.
"""


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

    async def _fetch_available_models(self) -> list[str]:
        """Fetch available models by calling list_models API."""
        try:
            resp = await self._client.models.list()
            models = [m.id for m in resp.data]
            log.info("fetched %d available models", len(models))
            return models
        except Exception:  # noqa: BLE001 - external API boundary
            log.warning("failed to fetch available models")
            return []

    async def _get_system_prompt(self) -> str:
        available_models = await self._fetch_available_models()
        return _SYSTEM_TEMPLATE.format(
            available_models=self._format_available_models(available_models)
        )

    def _format_available_models(self, models: list[str]) -> str:
        if not models:
            return "  (unable to fetch available models)"
        return "\n".join(f"  - {m}" for m in models)

    # ── public API ──

    async def route(self, msg: IncomingMessage) -> RouteResult:
        text = msg.text.strip()

        # Fast regex path — skip LLM for common patterns
        fast = _fast_classify(text)
        if fast is not None:
            tool_name, arg = fast
            log.info("regex fast-path matched: %s(%s)", tool_name, arg)
            if tool_name == "create_project":
                return self._handle_create(arg)
            if tool_name == "switch_project":
                if not arg:
                    return self._handle_switch(DEFAULT_PROJECT_NAME)
                return self._handle_switch(arg)
            if tool_name == "list_projects":
                return self._handle_list()
            if tool_name == "delete_project":
                return self._handle_delete(arg)
            if tool_name == "switch_model":
                return await self._handle_switch_model(arg)

        # Fall through to LLM classification
        tool_call = await self._classify(text)
        if tool_call is None:
            return RouteResult(project=self.sessions.get_current())

        inputs = tool_call.input or {}
        name = (inputs.get("name") or "").strip() if isinstance(inputs, dict) else ""
        model = (inputs.get("model") or "").strip() if isinstance(inputs, dict) else ""
        agent_type = (inputs.get("agent") or "").strip() if isinstance(inputs, dict) else ""

        if tool_call.name == "create_project":
            return self._handle_create(name, agent_type or None)
        if tool_call.name == "switch_project":
            return self._handle_switch(name)
        if tool_call.name == "list_projects":
            return self._handle_list()
        if tool_call.name == "list_models":
            return await self._handle_list_models()
        if tool_call.name == "switch_model":
            return await self._handle_switch_model(model)
        if tool_call.name == "delete_project":
            return self._handle_delete(name)
        return RouteResult(project=self.sessions.get_current())

    # ── helpers ──

    async def _classify(self, text: str) -> ToolUseBlock | None:
        projects = self.sessions.list_all()
        project_lines = "\n".join(f"- {p.name}" for p in projects) or "(none)"
        current = self.sessions.get_current()
        current_project = self.sessions.get(current)

        system = await self._get_system_prompt()

        user_block = (
            f"Active projects:\n{project_lines}\n\n"
            f"Currently pinned: {current}\n"
            f"Current model: {current_project.model if current_project else 'unknown'}\n\n"
            f"User message: {text}"
        )

        try:
            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=256,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
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

    def _handle_create(self, name: str, agent_type: str | None = None) -> RouteResult:
        if not name:
            return RouteResult(reply="❌ Project name required.")
        if agent_type:
            try:
                get_agent_definition(agent_type)
            except ValueError:
                return RouteResult(reply=f"❌ Unknown agent: {agent_type}")
            if agent_type not in settings.agents:
                return RouteResult(reply=f"❌ Agent '{agent_type}' is not enabled.")
        existed = self.sessions.get(name) is not None
        project = self.sessions.create(name, agent_type=agent_type)
        self.sessions.set_current(project.name)
        verb = "Switched to existing" if existed else "Created"
        return RouteResult(
            reply=f"✅ {verb} project: {project.name} (agent: {project.agent_type})"
        )

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
            f"{'★ ' if p.name == current else '• '}{p.name}" + (f" ({p.model})" if p.model else "")
            for p in projects
        ]
        return RouteResult(reply=f"Projects ({len(projects)}):\n" + "\n".join(lines))

    def _handle_delete(self, name: str) -> RouteResult:
        if not name:
            return RouteResult(reply="❌ Project name required.")
        if name == DEFAULT_PROJECT_NAME:
            return RouteResult(reply="❌ Cannot delete the default project.")
        if self.sessions.get(name) is None:
            return RouteResult(reply=f"❌ Unknown project: {name}")
        self.sessions.delete(name)
        return RouteResult(reply=f"🗑️ Deleted project: {name}", reset_project=name)

    async def _handle_list_models(self) -> RouteResult:
        current = self.sessions.get_current()
        project = self.sessions.get(current)
        if project is None:
            return RouteResult(reply="❌ No current project.")

        definition = get_agent_definition(project.agent_type)
        try:
            models = (
                await definition.list_models()
                if definition.list_models is not None
                else await self._fetch_available_models()
            )
        except Exception as exc:  # noqa: BLE001 - provider/API boundary
            log.warning("failed to list models for agent %s: %s", project.agent_type, exc)
            return RouteResult(reply=f"❌ Unable to fetch {project.agent_type} models: {exc}")
        if not models:
            return RouteResult(reply="❌ Unable to fetch available models. Check agent configuration.")

        lines = [f"  - {model}" for model in models]
        if project.model:
            lines.append(f"\nCurrent project model: {project.model}")
        return RouteResult(
            reply=f"Available models for {project.agent_type}:\n" + "\n".join(lines)
        )

    async def _handle_switch_model(self, model: str) -> RouteResult:
        if not model:
            return RouteResult(reply="❌ Model name required.")

        current = self.sessions.get_current()
        project = self.sessions.get(current)
        if project is None:
            return RouteResult(reply="❌ No current project.")

        if model.lower() == "default":
            self.sessions.reset_model(current)
            return RouteResult(
                reply=f"✅ Reset {current} to default model",
                reset_agent=True,
                reset_project=current,
            )

        definition = get_agent_definition(project.agent_type)
        if definition.list_models is not None:
            try:
                available = await definition.list_models()
            except Exception as exc:  # noqa: BLE001 - provider/API boundary
                log.warning("model listing failed for %s: %s", project.agent_type, exc)
                return RouteResult(reply=f"❌ Unable to validate {project.agent_type} model: {exc}")
            if model not in available:
                return RouteResult(
                    reply=f"❌ Model '{model}' is not available for {project.agent_type}."
                )
        elif definition.validate_model_with_anthropic:
            test_client = AsyncAnthropic(
                api_key=os.environ.get("ANTHROPIC_API_KEY"),
                base_url=os.environ.get("ANTHROPIC_BASE_URL") or None,
            )
            try:
                await test_client.messages.create(
                    model=model,
                    max_tokens=1,
                    messages=[{"role": "user", "content": "test"}],
                )
            except Exception as exc:  # noqa: BLE001 - provider/API boundary
                log.warning("model %s test failed: %s", model, exc)
                return RouteResult(reply=f"❌ Model '{model}' not available: {exc}")

        self.sessions.set_model(current, model)
        return RouteResult(
            reply=f"✅ Switched {current} to {model}",
            reset_agent=True,
            reset_project=current,
        )
