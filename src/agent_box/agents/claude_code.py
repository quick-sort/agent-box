"""Claude Code agent — uses ClaudeSDKClient for persistent, resumable sessions."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

import anyio

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SessionMessage,
    SystemMessage,
    TextBlock,
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
_TOOLS_REQUIRING_USER_INPUT = frozenset({"AskUserQuestion"})

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


import re


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


class ClaudeCodeAgent(BaseAgent):
    """Each project gets one ClaudeSDKClient. Session id is tracked externally."""

    def __init__(self, project: ProjectInfo) -> None:
        super().__init__(project)
        self._client: ClaudeSDKClient | None = None
        # When the agent calls AskUserQuestion (or similar), we store the
        # tool_use_id here. The *next* ``run()`` call sends the user's
        # message as a ``tool_result`` instead of a fresh query.
        self._pending_ask: dict[str, Any] | None = None

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
        )
        if self.project.session_id:
            opts.resume = self.project.session_id
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

    async def _send_tool_result(
        self,
        client: ClaudeSDKClient,
        tool_use_id: str,
        answer: str,
        session_id: str,
    ) -> None:
        """Write a ``tool_result`` message to CLI stdin so it can continue."""
        message = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": answer,
                    }
                ],
            },
            "parent_tool_use_id": tool_use_id,
            "session_id": session_id or "default",
        }
        # Use the transport directly — ``client.query()`` only sends plain
        # text user messages, but we need to attach a tool_result payload.
        await client._transport.write(json.dumps(message) + "\n")

    @property
    def has_pending_question(self) -> bool:
        """True when the agent asked a question and is waiting for user reply."""
        return self._pending_ask is not None

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    async def run(self, prompt: str, user_id: str = "", channel: str = "") -> AsyncIterator[OutgoingMessage]:
        client = await self._ensure_client()

        # If a previous run() paused with a pending AskUserQuestion, the
        # current prompt is the user's answer — send it as tool_result.
        if self._pending_ask is not None:
            ask = self._pending_ask
            self._pending_ask = None
            log.info(
                "resuming pending AskUserQuestion tool_use_id=%s",
                ask["tool_use_id"],
            )
            await self._send_tool_result(
                client,
                tool_use_id=ask["tool_use_id"],
                answer=prompt,
                session_id=ask.get("session_id", ""),
            )
        else:
            await client.query(prompt)

        # Build prefixes to strip from file paths in tool summaries.
        # - Project path: /home/.../workspace/project  → src/main.py
        # - Channel downloads: ~/.agent-box/channels/*/downloads → image.jpg
        _path_prefixes = _build_path_prefixes(self.project.path)

        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    # --- AskUserQuestion interception ---
                    if (
                        isinstance(block, ToolUseBlock)
                        and block.name in _TOOLS_REQUIRING_USER_INPUT
                    ):
                        # Log the full input for debugging
                        log.info(
                            "AskUserQuestion detected, tool_use_id=%s, input=%s",
                            block.id,
                            block.input,
                        )
                        question_text = self._format_question(block)
                        yield OutgoingMessage(
                            text=question_text,
                            user_id=user_id,
                            channel=channel,
                            type=MessageType.text,
                        )
                        self._pending_ask = {
                            "tool_use_id": block.id,
                            "session_id": msg.session_id,
                        }
                        log.info(
                            "AskUserQuestion intercepted, pausing run() "
                            "tool_use_id=%s, question_text=%s",
                            block.id,
                            question_text,
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
        self._pending_ask = None
        if self._client:
            await self._client.disconnect()
            self._client = None
