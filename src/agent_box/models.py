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


@dataclass
class OutgoingMessage:
    """Message to send back through an IM channel."""

    text: str
    user_id: str
    type: MessageType = MessageType.text
    data: dict[str, Any] | None = None  # extra payload per type


@dataclass
class ProjectInfo:
    """Metadata for a managed project."""

    slug: str
    name: str
    path: str  # absolute path to project folder
    agent_type: str = "claude_code"
    model: str | None = None  # override model for this project
    session_id: str | None = None  # one session per agent per project
    description: str = ""  # background info for routing
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
