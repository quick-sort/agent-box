"""Claude Code agent — uses ClaudeSDKClient for persistent, resumable sessions."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from claude_code_sdk import (
    AssistantMessage,
    ClaudeCodeOptions,
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


class ClaudeCodeAgent(BaseAgent):
    """Each project gets one ClaudeSDKClient. Session id is tracked externally."""

    def __init__(self, project: ProjectInfo) -> None:
        super().__init__(project)
        self._client: ClaudeSDKClient | None = None

    def _build_options(self) -> ClaudeCodeOptions:
        opts = ClaudeCodeOptions(
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
            log.info("agent connected for project %s", self.project.slug)
        return self._client

    async def run(self, prompt: str, user_id: str = "") -> AsyncIterator[OutgoingMessage]:
        client = await self._ensure_client()
        await client.query(prompt)

        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
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
        if self._client:
            await self._client.disconnect()
            self._client = None
