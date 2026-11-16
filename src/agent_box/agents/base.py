"""Abstract base for project agents."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from ..models import OutgoingMessage, ProjectInfo


class ProgressThrottle:
    """Rate-limit tool-progress messages for one agent session.

    IM channels impose per-conversation send quotas (WeCom: 30/min, QQ: 5
    passive replies per message). Surfacing every tool call in a burst burns
    that quota, so intermediate progress lines are dropped and at most one is
    emitted per ``interval`` seconds. Real answers are never throttled.
    """

    def __init__(self) -> None:
        self._last = 0.0

    def allow(self, interval: float) -> bool:
        now = time.monotonic()
        if interval > 0 and now - self._last < interval:
            return False
        self._last = now
        return True


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
