"""Abstract base for IM channel adapters."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import anyio

from ..models import IncomingMessage, OutgoingMessage

log = logging.getLogger(__name__)


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

    async def turn_finished(self, msg: OutgoingMessage) -> None:
        """Called when the inbound turn for ``msg.conversation_id`` ends.

        Channels that need an explicit end-of-turn signal (the relay channel
        acks the delivery here) override this. The default is a no-op.
        """

    async def send_loop(self, recv: anyio.abc.ObjectReceiveStream[OutgoingMessage]) -> None:
        """Consume outgoing messages and send them.

        A failed send must not kill this loop: every channel shares one task
        group in ``App.run``, so an escaping exception would cancel the whole
        application over a single transient network error.
        """
        async for msg in recv:
            if (msg.data or {}).get("_turn_end"):
                try:
                    await self.turn_finished(msg)
                except Exception:
                    log.exception(
                        "%s: failed to finish turn for %s",
                        type(self).__name__, msg.conversation_id,
                    )
                continue
            try:
                await self.send_reply(msg)
            except Exception:
                log.exception(
                    "%s: failed to send reply to %s, message dropped",
                    type(self).__name__, msg.user_id,
                )
