"""Claude Code agent — uses ClaudeSDKClient for persistent, resumable sessions."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
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
        """Render AskUserQuestion tool input as human-readable text."""
        inp = block.input or {}
        question = inp.get("question", "")
        if not question:
            question = "(agent requires your input)"

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

    async def run(self, prompt: str, user_id: str = "") -> AsyncIterator[OutgoingMessage]:
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

        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    # --- AskUserQuestion interception ---
                    if (
                        isinstance(block, ToolUseBlock)
                        and block.name in _TOOLS_REQUIRING_USER_INPUT
                    ):
                        question_text = self._format_question(block)
                        yield OutgoingMessage(
                            text=question_text,
                            user_id=user_id,
                            type=MessageType.text,
                        )
                        self._pending_ask = {
                            "tool_use_id": block.id,
                            "session_id": msg.session_id,
                        }
                        log.info(
                            "AskUserQuestion intercepted, pausing run() "
                            "tool_use_id=%s",
                            block.id,
                        )
                        return  # Stop yielding; next run() will resume

                    # --- normal block handling ---
                    if isinstance(block, TextBlock):
                        cleaned = block.text.strip()
                        if cleaned:
                            yield OutgoingMessage(text=cleaned, user_id=user_id, type=MessageType.text)
                    elif isinstance(block, ThinkingBlock):
                        yield OutgoingMessage(text=block.thinking, user_id=user_id, type=MessageType.thinking)
                    elif isinstance(block, ToolUseBlock):
                        yield OutgoingMessage(
                            text=block.name, user_id=user_id, type=MessageType.tool_use,
                            data={"id": block.id, "name": block.name, "input": block.input},
                        )
            elif isinstance(msg, UserMessage):
                for block in msg.content:
                    if isinstance(block, ToolResultBlock):
                        content = block.content if isinstance(block.content, str) else json.dumps(block.content, ensure_ascii=False) if block.content else ""
                        yield OutgoingMessage(
                            text=content, user_id=user_id, type=MessageType.tool_result,
                            data={"tool_use_id": block.tool_use_id, "is_error": block.is_error},
                        )
            elif isinstance(msg, SystemMessage):
                yield OutgoingMessage(
                    text=msg.subtype, user_id=user_id, type=MessageType.system,
                    data=msg.data,
                )
            elif isinstance(msg, ResultMessage):
                if msg.session_id and msg.session_id != self.project.session_id:
                    self.project.session_id = msg.session_id
                yield OutgoingMessage(
                    text=msg.result or "", user_id=user_id, type=MessageType.result,
                    data={"session_id": msg.session_id, "cost": msg.total_cost_usd, "duration_ms": msg.duration_ms},
                )

    async def close(self) -> None:
        self._pending_ask = None
        if self._client:
            await self._client.disconnect()
            self._client = None
