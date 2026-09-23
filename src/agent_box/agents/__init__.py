"""Typed registry and factory for project agent backends."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..config import settings
from ..models import ProjectInfo
from .base import BaseAgent

ModelLister = Callable[[], Awaitable[list[str]]]


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """Capabilities needed by the factory and provider-aware router."""

    factory: type[BaseAgent]
    list_models: ModelLister | None = None
    validate_model_with_anthropic: bool = True


_REGISTRY: dict[str, AgentDefinition] = {}


def _ensure_registry() -> None:
    if _REGISTRY:
        return
    from .claude_code import ClaudeCodeAgent

    _REGISTRY.update(
        {
            "claude_code": AgentDefinition(factory=ClaudeCodeAgent),
        }
    )


def get_agent_definition(agent_type: str) -> AgentDefinition:
    """Return backend capabilities independently of whether it is enabled."""
    _ensure_registry()
    definition = _REGISTRY.get(agent_type)
    if definition is None:
        raise ValueError(f"Unknown agent type: {agent_type!r}")
    return definition


def create_agent(agent_type: str, project: ProjectInfo) -> BaseAgent:
    """Create an enabled agent backend for *project*."""
    if agent_type not in settings.agents:
        raise ValueError(f"Agent {agent_type!r} not enabled. Enabled: {settings.agents}")
    return get_agent_definition(agent_type).factory(project)
