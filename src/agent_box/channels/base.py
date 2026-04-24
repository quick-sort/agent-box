"""Abstract base for IM channel adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod

import anyio

from ..models import IncomingMessage, OutgoingMessage


class BaseChannel(ABC):
    """An IM channel that receives messages and sends replies."""

    def __init__(self, send_stream: anyio.abc.ObjectSendStream[IncomingMessage]) -> None:
        self.send_stream = send_stream

    @abstractmethod
    async def start(self) -> None:
        """Start polling / listening for incoming messages."""

    @abstractmethod
    async def send_reply(self, msg: OutgoingMessage) -> None:
        """Send a reply back through this channel."""

    async def send_loop(self, recv: anyio.abc.ObjectReceiveStream[OutgoingMessage]) -> None:
        """Consume outgoing messages and send them."""
        async for msg in recv:
            await self.send_reply(msg)
