"""Abstract base for message routers."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import IncomingMessage


class BaseRouter(ABC):
    @abstractmethod
    async def route(self, msg: IncomingMessage) -> str:
        """Return a project slug, 'DEFAULT', or 'NEW_PROJECT <name>'."""
