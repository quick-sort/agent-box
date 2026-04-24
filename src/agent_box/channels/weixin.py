"""Weixin (personal WeChat) channel adapter using weixin_sdk."""

from __future__ import annotations

import logging

import anyio

from .weixin_sdk import AccountClient, StateStore
from .weixin_sdk.messages import extract_text_body

from ..config import settings
from ..models import IncomingMessage, MessageType, OutgoingMessage
from .base import BaseChannel

log = logging.getLogger(__name__)

POLL_INTERVAL = 1.0  # seconds between polls on empty response


def _resolve_account_id(store: StateStore) -> str:
    """Return account_id from env or from saved active account."""
    if settings.weixin_account_id:
        return settings.weixin_account_id
    saved = store.load_active_account_id()
    if saved:
        return saved
    raise RuntimeError("No WEIXIN_ACCOUNT_ID set and no saved account found. Run login first.")


class WeixinChannel(BaseChannel):
    """Long-poll based adapter for personal WeChat via weixin_sdk."""

    def __init__(self, send_stream: anyio.abc.ObjectSendStream[IncomingMessage]) -> None:
        super().__init__(send_stream)
        self._store = StateStore(settings.weixin_state_dir)
        account_id = _resolve_account_id(self._store)
        self.account = AccountClient.from_store(account_id, store=self._store)

    async def start(self) -> None:
        log.info("weixin channel started for account %s", settings.weixin_account_id)
        try:
            while True:
                try:
                    poll = await anyio.to_thread.run_sync(self.account.poll_once, abandon_on_cancel=True)
                    for raw_msg in poll.messages:
                        text = extract_text_body(raw_msg)
                        if not text:
                            continue
                        user_id = raw_msg.get("from_user_id", "")
                        await self.send_stream.send(
                            IncomingMessage(
                                text=text,
                                user_id=user_id,
                                channel="weixin",
                                raw=raw_msg,
                            )
                        )
                except TimeoutError:
                    pass
                except Exception:
                    log.exception("weixin poll error")
                    await anyio.sleep(POLL_INTERVAL)
        finally:
            await self.send_stream.aclose()

    async def send_reply(self, msg: OutgoingMessage) -> None:
        if msg.type != MessageType.text:
            return
        await anyio.to_thread.run_sync(
            lambda: self.account.send_text(to_user_id=msg.user_id, text=msg.text)
        )
