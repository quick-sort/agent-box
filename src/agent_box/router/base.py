"""Abstract base for message routers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..models import IncomingMessage


@dataclass
class RouteResult:
    """Outcome of routing one incoming message.

    Either:
      - the router handled a command (``reply`` is set, ``project`` is None), or
      - the message should be forwarded to ``project``.
    """

    project: str | None = None
    reply: str | None = None
    reset_agent: bool = False


class BaseRouter(ABC):
    @abstractmethod
    async def route(self, msg: IncomingMessage) -> RouteResult:
        """Decide whether to handle the message as a project-management command
        or forward it to the currently pinned project."""
