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


def _resolve_account_id(store: StateStore) -> str | None:
    """Return account_id from env or from saved active account, or None if not found."""
    if settings.weixin_account_id:
        return settings.weixin_account_id
    saved = store.load_active_account_id()
    if saved:
        return saved
    return None


class WeixinChannel(BaseChannel):
    """Long-poll based adapter for personal WeChat via weixin_sdk."""

    def __init__(self, send_stream: anyio.abc.ObjectSendStream[IncomingMessage]) -> None:
        super().__init__(send_stream)
        self._store = StateStore(settings.weixin_state_dir)
        self._account_id = _resolve_account_id(self._store)
        self.account: AccountClient | None = None

    async def start(self) -> None:
        while True:
            account_id = self._account_id or _resolve_account_id(self._store)
            if account_id is None:
                log.warning(
                    "No weixin account found. Set WEIXIN_ACCOUNT_ID or run weixin-sdk login first. "
                    "Retrying in 60 seconds."
                )
                await anyio.sleep(60)
                continue
            self._account_id = account_id
            break
        self.account = AccountClient.from_store(self._account_id, store=self._store)
        log.info("weixin channel started for account %s", self._account_id)
        try:
            while True:
                try:
                    poll = await anyio.to_thread.run_sync(self.account.poll_once, abandon_on_cancel=True)
                    for raw_msg in poll.messages:
                        text = extract_text_body(raw_msg)
                        if not text:
                            continue
                        user_id = raw_msg.get("from_user_id", "")
                        try:
                            await anyio.to_thread.run_sync(
                                lambda uid=user_id: self._send_typing(uid),
                                abandon_on_cancel=True,
                            )
                        except Exception:
                            log.debug("failed to send typing indicator to %s", user_id)
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

    def _send_typing(self, user_id: str) -> None:
        assert self.account is not None
        ticket = self.account.get_typing_ticket(user_id=user_id)
        if ticket:
            self.account.send_typing(user_id=user_id, typing_ticket=ticket)

    async def send_reply(self, msg: OutgoingMessage) -> None:
        if msg.type != MessageType.text:
            return
        if self.account is None:
            log.warning("Weixin account not initialized, cannot send reply.")
            return
        await anyio.to_thread.run_sync(
            lambda: self.account.send_text(to_user_id=msg.user_id, text=msg.text)
        )
