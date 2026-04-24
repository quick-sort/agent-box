"""Abstract base for project agents."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from ..models import OutgoingMessage, ProjectInfo


class BaseAgent(ABC):
    def __init__(self, project: ProjectInfo) -> None:
        self.project = project

    @abstractmethod
    async def run(self, prompt: str, user_id: str = "") -> AsyncIterator[OutgoingMessage]:
        """Execute a prompt and yield OutgoingMessage events."""
