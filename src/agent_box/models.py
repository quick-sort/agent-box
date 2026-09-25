"""Shared data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class MessageType(Enum):
    text = "text"
    tool_use = "tool_use"
    tool_result = "tool_result"
    thinking = "thinking"
    system = "system"
    result = "result"


@dataclass
class IncomingMessage:
    """Unified message from any IM channel."""

    text: str
    user_id: str
    channel: str  # e.g. "weixin"
    raw: dict | None = None  # original payload
    # The chat/session the message came from. Defaults to user_id, but for
    # group channels it is the group id (e.g. WeCom group chatid), which is
    # *different* from the sender's user_id. Drives per-conversation isolation
    # and reply routing.
    conversation_id: str | None = None

    def __post_init__(self) -> None:
        if not self.conversation_id:
            self.conversation_id = self.user_id


@dataclass
class OutgoingMessage:
    """Message to send back through an IM channel."""

    text: str
    user_id: str
    channel: str = ""  # target channel name (e.g. "qq", "weixin")
    type: MessageType = MessageType.text
    data: dict[str, Any] | None = None  # extra payload per type
    # The chat/session the reply must go back to. Defaults to user_id; group
    # channels set it to the group id so the reply lands in the same group the
    # original message came from.
    conversation_id: str | None = None

    def __post_init__(self) -> None:
        if not self.conversation_id:
            self.conversation_id = self.user_id


@dataclass
class ProjectInfo:
    """Metadata for a managed project. Identified by ``name``."""

    name: str
    path: str  # absolute path to project folder
    agent_type: str = "claude_code"
    model: str | None = None  # override model for this project
    session_id: str | None = None  # one session per agent per project
    description: str = ""  # background info for routing
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass(frozen=True)
class ChannelSpec:
    """A concrete channel instance to run.

    ``type`` is the adapter kind ("wecom" / "qq" / "tui" / "weixin").
    ``instance_id`` is the routing key carried in ``IncomingMessage.channel``
    / ``OutgoingMessage.channel`` — unique per running channel (e.g.
    ``"wecom:prod"``). ``config`` carries per-instance configuration for
    adapters that need it (WeCom), or ``None`` for single-instance types.
    """

    type: str
    instance_id: str
    config: Any | None = None
