"""Abstract base for project agents."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from ..models import OutgoingMessage, ProjectInfo


class BaseAgent(ABC):
    def __init__(self, project: ProjectInfo) -> None:
        self.project = project

    @abstractmethod
    async def run(
        self,
        prompt: str,
        user_id: str = "",
        channel: str = "",
    ) -> AsyncIterator[OutgoingMessage]:
        """Execute one conversational turn and stream channel-neutral events."""

    async def cancel(self) -> None:
        """Interrupt the current turn without releasing persistent resources."""

    async def close(self) -> None:
        """Release agent resources. Stateless implementations may do nothing."""
