"""Claude Code agent — uses ClaudeSDKClient for persistent, resumable sessions."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import AsyncIterator
from typing import Any

import anyio
from anthropic import AsyncAnthropic

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SessionMessage,
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
    get_session_messages,
)

from ..config import settings
from ..models import MessageType, OutgoingMessage, ProjectInfo
from .base import BaseAgent

log = logging.getLogger(__name__)

# Tool names that require user input. When the agent calls one of these,
# we forward the question to the user via the IM channel and pause the
# generator until the user replies. The next ``run()`` call sends the
# reply back as a ``tool_result`` so the CLI can continue.
#
# ExitPlanMode is included because Claude Code's CLI treats it as
# ``requiresUserInteraction()`` — it blocks waiting for an approval
# tool_result. Without feeding one back, the next user message would
# re-enter the blocked CLI and the plan would be re-emitted in a loop.
_TOOLS_REQUIRING_USER_INPUT = frozenset({"AskUserQuestion", "ExitPlanMode"})

# Hint appended to surfaced plans so the user knows how to respond.
# Reply "Yes" to approve, or "No <feedback>" to send feedback to the agent.
_PLAN_APPROVAL_HINT = "\n\n— 回复 Yes 批准 / No <修改意见> 退回修改"

# Regex to match [SEND_FILE:/path/to/file] markers in agent text output.
_SEND_FILE_RE = re.compile(r"\[SEND_FILE:([^\]]+)\]")

# System prompt appended to Claude Code's default prompt, instructing the
# agent to use [SEND_FILE:path] markers when generating files the user
# should receive.
_SEND_FILE_INSTRUCTION = (
    "When you generate a file that the user should receive (images, charts, "
    "PDFs, documents), include a marker on its own line:\n"
    "[SEND_FILE:/absolute/path/to/file]\n"
    "You can include multiple markers for multiple files. The markers will be "
    "automatically removed from your response and the files will be sent to "
    "the user. Only use this for files the user explicitly asked for or that "
    "are final deliverables — not intermediate temporary files."
)


def _parse_send_file_markers(text: str) -> tuple[str, list[str]]:
    """Extract ``[SEND_FILE:path]`` markers from text.

    Returns ``(cleaned_text, file_paths)``.
    """
    paths = _SEND_FILE_RE.findall(text)
    if not paths:
        return text, []
    cleaned = _SEND_FILE_RE.sub("", text).strip()
    return cleaned, paths


def _build_path_prefixes(project_path: str) -> tuple[str, ...]:
    """Collect directory prefixes to strip from file paths in tool summaries.

    Three categories:
    1. Project workspace — so ``/home/.../project/src/main.py`` → ``src/main.py``
    2. Workspace root — covers commands referencing other projects or the workspace itself
    3. Channel download dirs — so ``~/.agent-box/channels/weixin/downloads/img.jpg`` → ``img.jpg``
    """
    prefixes: list[str] = []
    # Project path (most specific — checked first)
    pp = project_path.rstrip("/")
    if pp:
        prefixes.append(pp)
    # Workspace root — so commands referencing other projects or the workspace
    # dir itself are also shortened (e.g. find /home/.../workspace/ → find .)
    ws = str(settings.workspace_dir.resolve()).rstrip("/")
    if ws and ws != pp:
        prefixes.append(ws)
    # Channel download dirs (WeChat + QQ)
    for ch in ("weixin", "qq"):
        d = str((settings.config_dir / "channels" / ch / "downloads").resolve())
        prefixes.append(d)
    return tuple(prefixes)


def _shorten_path(path: str, prefixes: tuple[str, ...]) -> str:
    """Strip known directory prefixes from a file path.

    Turns ``/home/user/.agent-box/workspace/project/src/main.py``
    into ``src/main.py`` when ``/home/user/.agent-box/workspace/project``
    is in *prefixes*.
    """
    for p in prefixes:
        if path.startswith(p) and len(path) > len(p):
            return path[len(p):].lstrip("/")
    return path


def _shorten_paths_in_cmd(cmd: str, prefixes: tuple[str, ...]) -> str:
    """Replace known path prefixes inside a shell command string.

    Handles:
    - Plain paths: /home/.../workspace/project → .
    - Shell-escaped paths: /home/.../workspace/project\\ box → .
    - Quoted paths: /home/.../workspace/"project name"/subdir → .
    Replaces the project path with ``.`` since that's the cwd.

    Uses ``(?=[/'" ]|$)`` lookahead to avoid matching inside longer path
    components (e.g. ``/workspace/project`` should NOT match ``/workspace/project-other``).
    """
    for p in prefixes:
        # Shell-escaped variant: "agent box" → "agent\\ box"
        escaped = p.replace(" ", "\\ ")
        escaped_pattern = re.escape(escaped) + r"""(?=[/'" ]|$)"""
        try:
            new_cmd = re.sub(escaped_pattern, ".", cmd, count=1)
        except re.error:
            new_cmd = cmd
        if new_cmd != cmd:
            cmd = new_cmd
            continue

        # Use regex to handle quoted path components with spaces
        # e.g., /workspace/"agent box"/agent-box
        # Split path and build pattern that matches with optional quotes
        parts = p.split("/")
        pattern_parts = []
        for i, part in enumerate(parts):
            if not part:
                continue
            escaped_part = re.escape(part)
            if i == len(parts) - 1:
                # Last part (may contain spaces) - allow optional quotes around it
                pattern_parts.append(f'(?:{escaped_part}|"{escaped_part}"|\'{escaped_part}\')')
            else:
                pattern_parts.append(escaped_part)

        pattern = "/" + "/".join(pattern_parts) + r"""(?=[/'" ]|$)"""
        try:
            new_cmd = re.sub(pattern, ".", cmd, count=1)
            if new_cmd != cmd:
                cmd = new_cmd
                continue
        except re.error:
            pass

        # Fallback: check for exact quoted prefix
        if f'"{p}"' in cmd:
            cmd = cmd.replace(f'"{p}"', ".")
        elif f"'{p}'" in cmd:
            cmd = cmd.replace(f"'{p}'", ".")

        # Handle quoted path prefix: "/path/"dirname""
        if f'"{p}/' in cmd:
            cmd = cmd.replace(f'"{p}/', '"./')
        elif f"'{p}/" in cmd:
            cmd = cmd.replace(f"'{p}/", "'.")

        # Plain path — use regex to avoid matching inside longer path components
        plain_pattern = re.escape(p) + r"""(?=[/'" ]|$)"""
        try:
            new_cmd = re.sub(plain_pattern, ".", cmd, count=1)
            if new_cmd != cmd:
                cmd = new_cmd
        except re.error:
            pass

    return cmd


def _format_tool_summary(block: ToolUseBlock, *, prefixes: tuple[str, ...] = ()) -> str:
    """Return a one-line status for a tool call — just enough to signal
    *activity*, not enough to overwhelm the IM channel."""
    name = block.name
    inp = block.input or {}
    _s = lambda p: _shorten_path(p, prefixes)

    if name == "Bash":
        cmd = _shorten_paths_in_cmd(inp.get("command", ""), prefixes)
        if len(cmd) > 60:
            cmd = cmd[:57] + "..."
        return f"🔧 {cmd}"
    if name == "Read":
        return f"📖 {_s(inp.get('file_path', ''))}"
    if name in ("Edit", "Write", "MultiEdit"):
        return f"✏️ {_s(inp.get('file_path', ''))}"
    if name == "Grep":
        return f"🔍 {inp.get('pattern', '')}"
    if name == "Glob":
        return f"📂 {inp.get('pattern', '')}"
    if name == "Agent":
        desc = inp.get("description") or inp.get("prompt", "")
        if len(desc) > 40:
            desc = desc[:37] + "..."
        return f"🤖 {desc}" if desc else "🤖 子任务"
    if name == "WebSearch":
        return f"🌐 {inp.get('query', '')}"
    if name == "WebFetch":
        return f"🌐 {inp.get('url', '')}"
    return f"⚙️ {name}"


# ── Context window limit recovery ──


def _is_context_limit_error(msg: ResultMessage) -> bool:
    """Return True if the ResultMessage indicates a context window overflow."""
    if not msg.is_error:
        return False
    error_text = " ".join(msg.errors or []) + " " + (msg.result or "")
    return "context window" in error_text.lower()


def _extract_text_from_content(content: Any) -> str:
    """Extract readable text from a message content list."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
        elif isinstance(block, TextBlock):
            parts.append(block.text)
    return "\n".join(parts).strip()


def _format_recent_rounds(messages: list[SessionMessage], n: int = 2) -> str:
    """Format messages from the last *n* user messages onwards.

    Finds the Nth-to-last user message and formats everything from that point,
    preserving all content types (text, tool_use, tool_result).
    """
    # Find indices of user messages
    user_indices = [i for i, m in enumerate(messages) if m.type == "user"]
    if not user_indices:
        return ""

    # Start from the Nth-to-last user message
    start = user_indices[-n] if len(user_indices) >= n else user_indices[0]

    lines: list[str] = ["[自动恢复上下文 — 最近对话摘要]"]
    for m in messages[start:]:
        role = "用户" if m.type == "user" else "助手"
        text = _extract_text_from_content(m.message.get("content", []))
        if text:
            lines.append(f"{role}：{text[:800]}")
    return "\n".join(lines)


# ── AskUserQuestion answer parser ──

_answer_client: AsyncAnthropic | None = None


def _get_answer_client() -> AsyncAnthropic:
    global _answer_client
    if _answer_client is None:
        _answer_client = AsyncAnthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
            base_url=os.environ.get("ANTHROPIC_BASE_URL") or None,
        )
    return _answer_client


async def _parse_user_answer(questions: list[dict], user_reply: str) -> dict:
    """Map free-text user reply to structured AskUserQuestion answers via LLM.

    Returns ``{"answers": {"<question>": "<label or free text>"}}`` on success.
    Raises on JSON parse failure so callers can fall back to the ``response`` field.
    """
    model = (
        os.environ.get("ANTHROPIC_SMALL_FAST_MODEL")
        or os.environ.get("ANTHROPIC_DEFAULT_HAIKU_MODEL")
        or "claude-haiku-4-5-20251001"
    )
    q_json = json.dumps(questions, ensure_ascii=False, indent=2)
    prompt = (
        "将用户回复映射为每个问题的答案。\n\n"
        f"问题列表（JSON）：\n{q_json}\n\n"
        f"用户回复：{user_reply}\n\n"
        "rules:\n"
        "- key 为问题的 question 字段\n"
        "- value 为用户选择的 option label（字符串）\n"
        "- multiSelect 题的 value 为 label 数组\n"
        "- 若回复无法对应任何选项，直接用用户原文作为 value\n"
        "只返回 JSON，不加代码块：\n"
        '{"answers": {"<question>": "<label or user text>"}}'
    )
    resp = await _get_answer_client().messages.create(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    # Strip optional markdown fences
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


# ── ExitPlanMode approval parser ──

# Words that count as approval / rejection when the user replies to a plan.
# Matched as a prefix on the reply (case-insensitive) so Chinese phrases
# without word boundaries (不要用 / 不行,) work just as well as English
# ("Yes, please", "No, change step 2"). Anything that doesn't match either
# defaults to rejection so the agent never runs a plan the user didn't
# explicitly approve.
_PLAN_APPROVE_WORDS = (
    "yes", "y", "ok", "okay", "approve", "approved", "lgtm",
    "是", "好的", "好", "可以", "同意", "批准", "行",
)
_PLAN_REJECT_WORDS = (
    "no", "n", "reject", "rejected", "deny", "denied", "cancel",
    "否", "不要", "不行", "拒绝", "别", "不",
)


def _build_exit_plan_permission(
    user_reply: str,
) -> PermissionResultAllow | PermissionResultDeny:
    """Translate the user's Yes/No reply into a permission decision for
    ExitPlanMode.

    Returning ``allow`` lets the CLI run the tool, which exits plan mode and
    emits ``"User has approved your plan..."``. Returning ``deny`` with a
    message makes that message the ``is_error`` tool_result the LLM sees, so
    it reads as rejection feedback.
    """
    reply = (user_reply or "").strip()
    reply_lower = reply.lower()

    # Reject: longest prefix match wins (so "不要用" matches "不要" before "不")
    reject_match = max(
        (w for w in _PLAN_REJECT_WORDS if reply_lower.startswith(w.lower())),
        key=len, default=None,
    )
    if reject_match:
        feedback = reply[len(reject_match):].lstrip(" :：,，、").strip()
        message = (
            f"User rejected the plan. Feedback: {feedback}. "
            "Please revise the plan based on this feedback and try again."
            if feedback
            else "User rejected the plan. Please revise and try again."
        )
        return PermissionResultDeny(behavior="deny", message=message)

    if any(reply_lower.startswith(w.lower()) for w in _PLAN_APPROVE_WORDS):
        return PermissionResultAllow(behavior="allow")

    # Ambiguous — default to rejection with the reply as feedback.
    return PermissionResultDeny(
        behavior="deny",
        message=(
            f"User rejected the plan. Feedback: {reply}. "
            "Please revise the plan based on this feedback and try again."
        ),
    )


class ClaudeCodeAgent(BaseAgent):
    """Each project gets one ClaudeSDKClient. Session id is tracked externally."""

    def __init__(self, project: ProjectInfo) -> None:
        super().__init__(project)
        self._client: ClaudeSDKClient | None = None
        # When the agent calls AskUserQuestion / ExitPlanMode, the
        # ``can_use_tool`` callback parks here on an ``asyncio.Future``.
        # ``run()`` surfaces the question to the IM channel and returns;
        # the next ``run()`` call resolves the future with the user's reply,
        # unblocking the callback so it can return the PermissionResult.
        self._pending_permission: dict[str, Any] | None = None

    def _build_options(self) -> ClaudeAgentOptions:
        opts = ClaudeAgentOptions(
            cwd=self.project.path,
            permission_mode=settings.agent_permission_mode,
            max_turns=settings.agent_max_turns,
            continue_conversation=True,
            model=self.project.model,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": _SEND_FILE_INSTRUCTION,
            },
            can_use_tool=self._can_use_tool,
            stderr=lambda line: log.warning("claude stderr [%s]: %s", self.project.name, line.rstrip()),
        )
        if self.project.session_id:
            opts.resume = self.project.session_id
        # Conditionally add wecom_mcp tool when WeCom channel is active
        from ..tools.wecom_mcp import is_wecom_mcp_enabled
        if is_wecom_mcp_enabled():
            from ..tools.wecom_mcp import create_wecom_mcp_server
            opts.mcp_servers = {"wecom_mcp": create_wecom_mcp_server()}
            opts.allowed_tools = ["wecom_mcp"]
        return opts

    async def _ensure_client(self) -> ClaudeSDKClient:
        if self._client is None:
            self._client = ClaudeSDKClient(self._build_options())
            await self._client.connect()
            log.info("agent connected for project %s", self.project.name)
        return self._client

    # ------------------------------------------------------------------
    # AskUserQuestion helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_question(block: ToolUseBlock) -> str:
        """Render AskUserQuestion tool input as human-readable text.

        Claude Code's AskUserQuestion tool has this schema:
        {
            questions: [{
                question: string,  // The actual question text
                header: string,    // Short label (max chip width chars)
                options: [{label, description, preview}],
                multiSelect: boolean
            }]
        }
        """
        inp = block.input or {}

        # Handle the actual schema: questions is an array of question objects
        questions_data = inp.get("questions")

        if questions_data and isinstance(questions_data, list):
            lines = []
            for i, q in enumerate(questions_data):
                if not isinstance(q, dict):
                    continue

                # Get question text - try multiple field names
                question_text = q.get("question") or q.get("prompt") or q.get("message") or ""

                # Get header if present
                header = q.get("header", "")
                if header:
                    lines.append(f"[{header}]")

                if not question_text:
                    question_text = "(agent requires your input)"

                lines.append(question_text)

                # Format options
                options = q.get("options")
                if options and isinstance(options, list):
                    for j, opt in enumerate(options, 1):
                        if isinstance(opt, dict):
                            label = opt.get("label", f"Option {j}")
                            desc = opt.get("description", "")
                            if desc:
                                lines.append(f"  {j}. {label} — {desc}")
                            else:
                                lines.append(f"  {j}. {label}")
                        else:
                            lines.append(f"  {j}. {opt}")

                # Add separator between multiple questions
                if i < len(questions_data) - 1:
                    lines.append("")

            result = "\n".join(lines)
            if not result:
                log.warning("AskUserQuestion with empty questions, input=%s", inp)
                return "(agent requires your input)"
            return result

        # Fallback for other formats
        question = inp.get("question") or inp.get("prompt") or inp.get("message") or ""
        if not question:
            log.warning(
                "AskUserQuestion with empty question, input=%s",
                inp,
            )
            return "(agent requires your input)"

        # Build options display
        options = inp.get("options")
        if options and isinstance(options, list):
            lines = [question, ""]
            for i, opt in enumerate(options, 1):
                if isinstance(opt, dict):
                    label = opt.get("label", f"Option {i}")
                    desc = opt.get("description", "")
                    if desc:
                        lines.append(f"  {i}. {label} — {desc}")
                    else:
                        lines.append(f"  {i}. {label}")
                else:
                    lines.append(f"  {i}. {opt}")
            return "\n".join(lines)

        return question

    # ------------------------------------------------------------------
    # Permission callback (can_use_tool)
    # ------------------------------------------------------------------
    #
    # Why AskUserQuestion / ExitPlanMode need a callback (problem writeup)
    # ───────────────────────────────────────────────────────────────────
    #
    # SYMPTOM
    #   The user's reply to an AskUserQuestion (and the Yes/No on an
    #   ExitPlanMode plan) was silently ignored: the question reached the IM
    #   channel, the user answered, yet the LLM proceeded with the default
    #   option exactly as if the user had cancelled.
    #
    # ROOT CAUSE
    #   The agent runs in ``bypassPermissions``, but that mode does NOT
    #   bypass tools flagged ``requiresUserInteraction()``. In the CLI's
    #   permission check the requiresUserInteraction short-circuit (the "1e"
    #   step in utils/permissions/permissions.ts) runs *before* the bypass
    #   check, so AskUserQuestion and ExitPlanMode still emit a
    #   ``can_use_tool`` request. With no callback registered, the SDK
    #   raised "canUseTool callback is not provided"
    #   (claude_agent_sdk/_internal/query.py), the CLI read that error as a
    #   user cancellation, and handed the LLM a cancelled tool_result —
    #   hence "user cancelled, use defaults".
    #
    # FIX — a two-phase handshake bridging the CLI permission request to the
    # IM channel, via an asyncio.Future keyed by tool_use_id:
    #
    #   1. The CLI streams the AssistantMessage carrying the tool_use block,
    #      then sends ``can_use_tool``. Either ``run()`` (seeing the block)
    #      or the callback (receiving the request) fires first;
    #      ``_get_or_create_permission_future`` guarantees exactly one shared
    #      future regardless of who wins the race.
    #   2. ``run()`` surfaces the question/plan to the IM channel and
    #      returns, so the channel can deliver it and the user can reply.
    #      Meanwhile the callback awaits the future, holding the CLI's
    #      permission request open (the CLI blocks until we answer).
    #   3. The user's reply arrives as the next ``run()`` call. ``run()``
    #      resolves the future with the reply text *instead of* issuing a
    #      fresh query (the CLI is mid-turn). The callback wakes and returns
    #      the decision:
    #        - AskUserQuestion → allow, with the parsed answers injected into
    #          ``updated_input.answers``. The CLI's tool execution formats
    #          those into the tool_result the LLM sees — this is the
    #          supported injection channel (the interactive UI uses it too).
    #        - ExitPlanMode → allow on approval (the CLI runs call() and
    #          exits plan mode), or deny with feedback whose message becomes
    #          the is_error tool_result.
    #
    #   The previous approach wrote a raw tool_result to stdin directly,
    #   bypassing the control protocol — flaky and ignored once the CLI had
    #   already moved past the (errored) permission request. Going through
    #   the callback is what makes the reply actually take effect.

    def _get_or_create_permission_future(
        self,
        tool_name: str,
        tool_use_id: str,
        input: dict[str, Any],
    ) -> dict[str, Any]:
        """Return the pending-permission entry for *tool_use_id*, creating
        it if neither ``run()`` nor the callback has yet.

        Both the block detection in ``run()`` (which sees the AssistantMessage
        first) and the ``can_use_tool`` callback (which the CLI fires right
        after) can win the race; whichever fires first creates the entry and
        the other reuses it, so there is exactly one shared future.
        """
        existing = self._pending_permission
        if existing is not None and existing.get("tool_use_id") == tool_use_id:
            return existing
        questions = (
            input.get("questions", []) if tool_name == "AskUserQuestion" else []
        )
        self._pending_permission = {
            "tool_name": tool_name,
            "tool_use_id": tool_use_id,
            "input": input,
            "questions": questions,
            "future": asyncio.get_running_loop().create_future(),
        }
        return self._pending_permission

    async def _can_use_tool(
        self,
        tool_name: str,
        input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """Permission callback wired into ``ClaudeAgentOptions``.

        Normal tools are allowed unconditionally (the agent runs in
        ``bypassPermissions``). The user-interaction tools — AskUserQuestion
        and ExitPlanMode — still trigger a ``can_use_tool`` request from the
        CLI even in bypass mode, so we intercept them here: park on a future
        until the user's reply arrives via the next ``run()`` call, then
        return the matching permission decision.
        """
        if tool_name not in _TOOLS_REQUIRING_USER_INPUT:
            return PermissionResultAllow(behavior="allow")

        pending = self._get_or_create_permission_future(
            tool_name, context.tool_use_id, input,
        )
        future: asyncio.Future = pending["future"]
        log.info(
            "can_use_tool: awaiting user reply for %s tool_use_id=%s",
            tool_name, context.tool_use_id,
        )
        try:
            reply = await future
        except asyncio.CancelledError:
            # Agent shutting down — deny so the CLI unblocks cleanly.
            self._pending_permission = None
            return PermissionResultDeny(
                behavior="deny", message="Agent shutting down",
            )
        self._pending_permission = None
        log.info(
            "can_use_tool: got user reply (%d chars) for %s, building result",
            len(reply or ""), tool_name,
        )

        if tool_name == "ExitPlanMode":
            return _build_exit_plan_permission(reply)

        # AskUserQuestion — map free-text reply to structured answers via the
        # small model, then inject them as ``updatedInput.answers``. The CLI's
        # tool execution formats them into the tool_result the LLM sees.
        questions = pending.get("questions", [])
        try:
            parsed = await _parse_user_answer(questions, reply)
            answers = parsed.get("answers", {})
        except Exception:
            log.warning(
                "failed to parse AskUserQuestion answer with LLM, "
                "falling back to raw user reply",
                exc_info=True,
            )
            answers = (
                {q.get("question", f"question_{i}"): reply
                 for i, q in enumerate(questions) if isinstance(q, dict)}
                or {"question": reply}
            )
        log.info("can_use_tool: parsed AskUserQuestion answers: %s", answers)
        return PermissionResultAllow(
            behavior="allow", updated_input={**input, "answers": answers},
        )

    @property
    def has_pending_question(self) -> bool:
        """True when the agent asked a question and is waiting for user reply."""
        return self._pending_permission is not None

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    async def run(self, prompt: str, user_id: str = "", channel: str = "") -> AsyncIterator[OutgoingMessage]:
        client = await self._ensure_client()

        # Diagnostic: trace every run() entry — what prompt, what pending state.
        pending_tool = (
            self._pending_permission.get("tool_name")
            if self._pending_permission else None
        )
        log.info(
            "run() entry: project=%s user=%s channel=%s prompt_preview=%r pending_permission=%s",
            self.project.name, user_id, channel,
            (prompt or "")[:200], pending_tool,
        )

        if self._pending_permission is not None:
            # Resume: the prompt is the user's reply to a pending permission.
            # Resolve the future so the ``can_use_tool`` callback can return
            # its PermissionResult and unblock the CLI. Do NOT send a fresh
            # query — the CLI is mid-turn, still waiting on the permission
            # response.
            pending = self._pending_permission
            log.info(
                "run(): resolving pending permission future with user reply "
                "(tool=%s tool_use_id=%s reply_preview=%r)",
                pending["tool_name"], pending["tool_use_id"],
                (prompt or "")[:200],
            )
            future: asyncio.Future = pending["future"]
            if not future.done():
                future.set_result(prompt)
        else:
            log.info("run(): no pending permission — sending prompt as fresh query")
            await client.query(prompt)

        # Build prefixes to strip from file paths in tool summaries.
        # - Project path: /home/.../workspace/project  → src/main.py
        # - Channel downloads: ~/.agent-box/channels/*/downloads → image.jpg
        _path_prefixes = _build_path_prefixes(self.project.path)

        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    # --- Tools that require user interaction ---
                    # AskUserQuestion and ExitPlanMode trigger a ``can_use_tool``
                    # request from the CLI even in bypassPermissions mode (they
                    # are ``requiresUserInteraction`` tools). We surface the
                    # question/plan to the IM channel, ensure a pending future
                    # exists (shared with the callback), and return so the user
                    # can reply. The next run() resolves the future; the
                    # ``_can_use_tool`` callback then returns the PermissionResult.
                    if (
                        isinstance(block, ToolUseBlock)
                        and block.name in _TOOLS_REQUIRING_USER_INPUT
                    ):
                        if block.name == "ExitPlanMode":
                            plan = (block.input or {}).get("plan")
                            body = plan.strip() if isinstance(plan, str) else ""
                            if not body:
                                body = "(agent 请求退出 plan 模式，但没有提供计划内容)"
                            text = body + _PLAN_APPROVAL_HINT
                            log.info(
                                "ExitPlanMode detected — surfacing plan, "
                                "tool_use_id=%s",
                                block.id,
                            )
                            yield OutgoingMessage(
                                text=text,
                                user_id=user_id,
                                channel=channel,
                                type=MessageType.text,
                                data={"id": block.id, "name": block.name, "input": block.input},
                            )
                            self._get_or_create_permission_future(
                                "ExitPlanMode", block.id, block.input or {},
                            )
                            log.info(
                                "ExitPlanMode pending permission saved, returning from run() — "
                                "waiting for user reply to approve/reject"
                            )
                            return  # Stop yielding; next run() will resume

                        # AskUserQuestion
                        log.info(
                            "AskUserQuestion detected — surfacing question, "
                            "tool_use_id=%s input=%s",
                            block.id, block.input,
                        )
                        question_text = self._format_question(block)
                        yield OutgoingMessage(
                            text=question_text,
                            user_id=user_id,
                            channel=channel,
                            type=MessageType.text,
                        )
                        self._get_or_create_permission_future(
                            "AskUserQuestion", block.id, block.input or {},
                        )
                        log.info(
                            "AskUserQuestion pending permission saved, returning from run() — "
                            "waiting for user reply (question_preview=%r)",
                            question_text[:200],
                        )
                        return  # Stop yielding; next run() will resume

                    # --- normal block handling ---
                    if isinstance(block, TextBlock):
                        cleaned = block.text.strip()
                        if cleaned:
                            text, file_paths = _parse_send_file_markers(cleaned)
                            if text:
                                yield OutgoingMessage(text=text, user_id=user_id, channel=channel, type=MessageType.text)
                            for fp in file_paths:
                                log.info("Agent requested file send: %s", fp)
                                yield OutgoingMessage(
                                    text="", user_id=user_id, channel=channel, type=MessageType.text,
                                    data={"file_path": fp},
                                )
                    elif isinstance(block, ToolUseBlock):
                        # Brief one-liner so the user knows something is happening.
                        summary = _format_tool_summary(block, prefixes=_path_prefixes)
                        yield OutgoingMessage(
                            text=summary, user_id=user_id, channel=channel, type=MessageType.text,
                            data={"id": block.id, "name": block.name, "input": block.input},
                        )
                    # ThinkingBlock / ToolResultBlock deliberately skipped —
                    # too noisy for IM channels.
            elif isinstance(msg, SystemMessage):
                yield OutgoingMessage(
                    text=msg.subtype, user_id=user_id, channel=channel, type=MessageType.system,
                    data=msg.data,
                )
            elif isinstance(msg, ResultMessage):
                # --- Context window limit recovery ---
                if _is_context_limit_error(msg):
                    log.warning(
                        "context window limit hit for project %s, session %s",
                        self.project.name, msg.session_id,
                    )
                    async for out_msg in self._recover_from_context_limit(
                        client, prompt, user_id, channel, msg.session_id,
                    ):
                        yield out_msg
                    return

                if msg.session_id and msg.session_id != self.project.session_id:
                    self.project.session_id = msg.session_id

                if msg.is_error:
                    error_detail = " ".join(msg.errors or []) or msg.result or "未知错误"
                    log.error(
                        "agent error for project %s: %s", self.project.name, error_detail,
                    )
                    yield OutgoingMessage(
                        text=f"❌ Agent 错误：{error_detail}",
                        user_id=user_id, channel=channel, type=MessageType.text,
                    )

                yield OutgoingMessage(
                    text=msg.result or "", user_id=user_id, channel=channel, type=MessageType.result,
                    data={"session_id": msg.session_id, "cost": msg.total_cost_usd, "duration_ms": msg.duration_ms},
                )

    async def _recover_from_context_limit(
        self,
        client: ClaudeSDKClient,
        prompt: str,
        user_id: str,
        channel: str,
        session_id: str,
    ) -> AsyncIterator[OutgoingMessage]:
        """Compact the session and replay recent context + original prompt."""
        yield OutgoingMessage(
            text="⚠️ 会话上下文已满，正在自动压缩并恢复...",
            user_id=user_id, channel=channel, type=MessageType.text,
        )

        # 1. Get recent conversation from session history
        recent_context = ""
        try:
            messages = await anyio.to_thread.run_sync(
                lambda: get_session_messages(
                    session_id, directory=self.project.path, limit=10,
                )
            )
            recent_context = _format_recent_rounds(messages, n=2)
            log.info("recovered %d session messages for context replay", len(messages))
        except Exception:
            log.warning("failed to read session messages for context replay", exc_info=True)

        # 2. Send /compact
        log.info("sending /compact to project %s", self.project.name)
        await client.query("/compact")
        async for msg in client.receive_response():
            if isinstance(msg, ResultMessage):
                if msg.session_id:
                    self.project.session_id = msg.session_id
                if msg.is_error:
                    yield OutgoingMessage(
                        text="❌ 自动压缩失败，请手动发送 /compact",
                        user_id=user_id, channel=channel, type=MessageType.text,
                    )
                    return
                break

        # 3. Re-send context + original prompt
        replay = recent_context + "\n\n" + prompt if recent_context else prompt
        log.info("re-playing prompt after compact (context=%d chars)", len(recent_context))
        await client.query(replay)
        _path_prefixes = _build_path_prefixes(self.project.path)

        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    # ExitPlanMode triggers a ``can_use_tool`` request —
                    # surface plan + hint, ensure a pending future, and pause
                    # so the next run() can resolve it with the user's reply.
                    if (
                        isinstance(block, ToolUseBlock)
                        and block.name == "ExitPlanMode"
                    ):
                        plan = (block.input or {}).get("plan")
                        body = plan.strip() if isinstance(plan, str) else ""
                        if not body:
                            body = "(agent 请求退出 plan 模式，但没有提供计划内容)"
                        yield OutgoingMessage(
                            text=body + _PLAN_APPROVAL_HINT,
                            user_id=user_id, channel=channel, type=MessageType.text,
                            data={"id": block.id, "name": block.name, "input": block.input},
                        )
                        self._get_or_create_permission_future(
                            "ExitPlanMode", block.id, block.input or {},
                        )
                        return
                    if isinstance(block, TextBlock):
                        cleaned = block.text.strip()
                        if cleaned:
                            text, file_paths = _parse_send_file_markers(cleaned)
                            if text:
                                yield OutgoingMessage(text=text, user_id=user_id, channel=channel, type=MessageType.text)
                            for fp in file_paths:
                                yield OutgoingMessage(
                                    text="", user_id=user_id, channel=channel, type=MessageType.text,
                                    data={"file_path": fp},
                                )
                    elif isinstance(block, ToolUseBlock):
                        summary = _format_tool_summary(block, prefixes=_path_prefixes)
                        yield OutgoingMessage(
                            text=summary, user_id=user_id, channel=channel, type=MessageType.text,
                            data={"id": block.id, "name": block.name, "input": block.input},
                        )
            elif isinstance(msg, SystemMessage):
                yield OutgoingMessage(
                    text=msg.subtype, user_id=user_id, channel=channel, type=MessageType.system,
                    data=msg.data,
                )
            elif isinstance(msg, ResultMessage):
                if msg.session_id:
                    self.project.session_id = msg.session_id
                yield OutgoingMessage(
                    text=msg.result or "", user_id=user_id, channel=channel, type=MessageType.result,
                    data={"session_id": msg.session_id, "cost": msg.total_cost_usd, "duration_ms": msg.duration_ms},
                )

    async def close(self) -> None:
        # Cancel any pending permission future so the ``can_use_tool`` callback
        # (awaiting in a background task) unblocks and returns a deny instead
        # of hanging on a client that's about to disconnect.
        if self._pending_permission is not None:
            future: asyncio.Future = self._pending_permission["future"]
            if not future.done():
                future.cancel()
            self._pending_permission = None
        if self._client:
            await self._client.disconnect()
            self._client = None
