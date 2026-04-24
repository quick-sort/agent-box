"""Agent factory — create agent by type string."""

from __future__ import annotations

from ..config import settings
from ..models import ProjectInfo
from .base import BaseAgent

_REGISTRY: dict[str, type] = {}


def _ensure_registry() -> None:
    if _REGISTRY:
        return
    # Register all known agent types (lazy imports)
    from .claude_code import ClaudeCodeAgent
    _REGISTRY["claude_code"] = ClaudeCodeAgent


def create_agent(agent_type: str, project: ProjectInfo) -> BaseAgent:
    """Create an agent instance by type name. Must be in settings.agents."""
    _ensure_registry()
    if agent_type not in settings.agents:
        raise ValueError(f"Agent {agent_type!r} not enabled. Enabled: {settings.agents}")
    cls = _REGISTRY.get(agent_type)
    if cls is None:
        raise ValueError(f"Unknown agent type: {agent_type!r}")
    return cls(project)
