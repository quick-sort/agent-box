"""Abstract base for IM channel adapters."""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod

import anyio

from ..models import IncomingMessage, OutgoingMessage

log = logging.getLogger(__name__)

# How long a single channel send may block before the loop gives up on it.
# Without a bound, one hung HTTP request / WebSocket ack stalls every message
# queued behind it on that channel.
DEFAULT_SEND_TIMEOUT = 60.0

# Uploads are legitimately slow (chunked transfer of up to ~100MB), so media
# sends get a much larger bound than plain text.
DEFAULT_MEDIA_SEND_TIMEOUT = 600.0

# Log a warning when a message sat in the queues longer than this (seconds).
_BACKLOG_WARN_AGE = 10.0


class BaseChannel(ABC):
    """An IM channel that receives messages and sends replies."""

    # Per-send hard timeout (seconds). Overridable per adapter.
    send_timeout: float = DEFAULT_SEND_TIMEOUT
    media_send_timeout: float = DEFAULT_MEDIA_SEND_TIMEOUT

    # Minimum spacing between two sends in the same conversation (seconds).
    # 0 disables pacing. Adapters that talk to rate-limited platforms set this
    # to match the platform quota (e.g. WeCom allows 30 messages/minute per
    # conversation, so 2.0s). Pacing is applied by ``send_loop``.
    min_send_interval: float = 0.0

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

    def send_interval_for(self, msg: OutgoingMessage) -> float:
        """Pacing interval to apply before sending ``msg`` (0 = send now)."""
        return self.min_send_interval

    def send_timeout_for(self, msg: OutgoingMessage) -> float:
        """Hard timeout for one send; media uploads get a larger budget."""
        data = msg.data or {}
        if data.get("file_path") or data.get("image_path") or data.get("image_url"):
            return self.media_send_timeout
        return self.send_timeout

    async def send_loop(self, recv: anyio.abc.ObjectReceiveStream[OutgoingMessage]) -> None:
        """Consume outgoing messages and send them.

        A failed send must not kill this loop: every channel shares one task
        group in ``App.run``, so an escaping exception would cancel the whole
        application over a single transient network error.

        Sends are bounded by ``send_timeout`` and spaced by ``min_send_interval``
        so a slow or rate-limited platform cannot stall the queue indefinitely.
        """
        last_sent: dict[str, float] = {}
        while True:
            try:
                msg = await recv.receive()
            except anyio.EndOfStream:
                return
            except anyio.ClosedResourceError:
                return

            if (msg.data or {}).get("_turn_end"):
                try:
                    await self.turn_finished(msg)
                except Exception:
                    log.exception(
                        "%s: failed to finish turn for %s",
                        type(self).__name__, msg.conversation_id,
                    )
                continue

            key = msg.conversation_id or msg.user_id
            interval = self.send_interval_for(msg)
            if interval > 0:
                wait = last_sent.get(key, 0.0) + interval - time.monotonic()
                if wait > 0:
                    await anyio.sleep(wait)

            started = time.monotonic()
            age = started - msg.created_at
            timeout = self.send_timeout_for(msg)
            try:
                with anyio.fail_after(timeout):
                    await self.send_reply(msg)
                elapsed = time.monotonic() - started
                if age > _BACKLOG_WARN_AGE:
                    log.warning(
                        "%s: sent queued message to %s after %.1fs in queue "
                        "(send took %.2fs, conversation=%s)",
                        type(self).__name__, msg.user_id, age, elapsed, key,
                    )
                else:
                    log.debug(
                        "%s: sent to %s in %.2fs (queued %.2fs)",
                        type(self).__name__, msg.user_id, elapsed, age,
                    )
            except TimeoutError:
                log.error(
                    "%s: send to %s timed out after %.0fs, message dropped",
                    type(self).__name__, msg.user_id, timeout,
                )
            except Exception:
                log.exception(
                    "%s: failed to send reply to %s, message dropped",
                    type(self).__name__, msg.user_id,
                )
            finally:
                # Pace from the last attempt even when it failed, otherwise a
                # failing platform is hammered in a tight retry loop.
                last_sent[key] = time.monotonic()
