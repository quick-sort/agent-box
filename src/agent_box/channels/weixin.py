"""Weixin (personal WeChat) channel adapter using weixin_sdk."""

from __future__ import annotations

import logging
from pathlib import Path

import anyio

from .weixin_sdk import AccountClient, StateStore
from .weixin_sdk.constants import ITEM_TYPE_FILE, ITEM_TYPE_IMAGE, ITEM_TYPE_VIDEO, ITEM_TYPE_VOICE
from .weixin_sdk.messages import extract_text_body, iter_media_items

from ..config import settings
from ..models import IncomingMessage, MessageType, OutgoingMessage
from .base import BaseChannel

log = logging.getLogger(__name__)

POLL_INTERVAL = 1.0  # seconds between polls on empty response

# Human-readable label for each media type
_MEDIA_LABELS = {
    ITEM_TYPE_IMAGE: "图片",
    ITEM_TYPE_VOICE: "语音",
    ITEM_TYPE_VIDEO: "视频",
    ITEM_TYPE_FILE: "文件",
}


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
        self._download_dir: Path = settings.config_dir / "channels" / "weixin" / "downloads"

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
                        user_id = raw_msg.get("from_user_id", "")

                        # Download any media attachments and append to text
                        if not text:
                            media_text = await self._download_media_text(raw_msg)
                            if not media_text:
                                continue
                            text = media_text
                        else:
                            # Even if there's text, check for co-sent media
                            media_text = await self._download_media_text(raw_msg)
                            if media_text:
                                text = f"{text}\n{media_text}"

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

    async def _download_media_text(self, raw_msg: dict) -> str:
        """Download all media items in a message and return a description string.

        Returns something like: "用户发送了一个图片，文件路径: /path/to/img.jpg"
        """
        items = list(iter_media_items(raw_msg))
        if not items or self.account is None:
            return ""

        self._download_dir.mkdir(parents=True, exist_ok=True)
        parts: list[str] = []

        for item in items:
            item_type = item.get("type")
            label = _MEDIA_LABELS.get(item_type, "文件")
            try:
                paths = await anyio.to_thread.run_sync(
                    lambda it=item: self.account.media.download_media(
                        it, output_dir=str(self._download_dir)
                    ),
                    abandon_on_cancel=True,
                )
                # download_media returns a single Path
                if isinstance(paths, Path):
                    parts.append(f"用户发送了一个{label}，文件路径: {paths}")
                elif isinstance(paths, list):
                    for p in paths:
                        parts.append(f"用户发送了一个{label}，文件路径: {p}")
            except Exception:
                log.exception("weixin: failed to download media item")
                parts.append(f"用户发送了一个{label}，文件路径: (下载失败)")

        return "\n".join(parts)

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

        data = msg.data or {}

        # File/media sending via MediaClient
        file_path = data.get("file_path")
        if file_path:
            try:
                await anyio.to_thread.run_sync(
                    lambda: self.account.media.send_file(
                        file_path=file_path,
                        to_user_id=msg.user_id,
                    ),
                    abandon_on_cancel=True,
                )
            except Exception:
                log.exception("weixin: failed to send file %s", file_path)
            return

        # Legacy image sending
        image_path = data.get("image_path")
        if image_path:
            try:
                await anyio.to_thread.run_sync(
                    lambda: self.account.media.send_file(
                        file_path=image_path,
                        to_user_id=msg.user_id,
                        forced_kind="image",
                    ),
                    abandon_on_cancel=True,
                )
            except Exception:
                log.exception("weixin: failed to send image %s", image_path)
            return

        # Text sending
        await anyio.to_thread.run_sync(
            lambda: self.account.send_text(to_user_id=msg.user_id, text=msg.text)
        )
