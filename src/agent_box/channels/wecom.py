"""WeCom (企业微信) channel adapter — WebSocket long connection mode.

Uses the official ``wecom-aibot-sdk`` Python SDK which connects via WebSocket
to wss://openws.work.weixin.qq.com. Only requires two credentials:

Required config:
  WECOM_BOT_ID   — 机器人 ID (from 企业微信管理后台 → 智能机器人)
  WECOM_SECRET   — 机器人 Secret
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from pathlib import Path
from typing import Any

from anyio.abc import ObjectSendStream

from wecom_aibot_sdk import WSClient, generate_req_id
from wecom_aibot_sdk.types import WsFrame, MessageType as WeComMsgType

from ..config import settings
from ..models import IncomingMessage, MessageType, OutgoingMessage
from .base import BaseChannel

log = logging.getLogger(__name__)

_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})
_AUDIO_EXTS = frozenset({".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a", ".amr"})


def _detect_media_type(file_path: str) -> str:
    ext = Path(file_path).suffix.lower()
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _AUDIO_EXTS:
        return "voice"
    return "file"


# ── Message parsing helpers ──────────────────────────────────────────────────

def _parse_message_body(body: dict[str, Any]) -> tuple[str, list[str]]:
    """Parse WeCom message body into (text, image_urls).

    Returns extracted text and list of image URLs that need to be downloaded.
    """
    msgtype = body.get("msgtype", "")
    text_parts: list[str] = []
    image_urls: list[str] = []

    if msgtype == "mixed" and body.get("mixed"):
        # 图文混排消息
        for item in body["mixed"].get("msg_item", []):
            if item.get("msgtype") == "text" and item.get("text", {}).get("content"):
                text_parts.append(item["text"]["content"])
            elif item.get("msgtype") == "image" and item.get("image", {}).get("url"):
                image_urls.append(item["image"]["url"])
    else:
        # 单条消息
        if body.get("text", {}).get("content"):
            text_parts.append(body["text"]["content"])
        if msgtype == "voice" and body.get("voice", {}).get("content"):
            # 语音转文字
            text_parts.append(body["voice"]["content"])
        if body.get("image", {}).get("url"):
            image_urls.append(body["image"]["url"])

    # 处理引用消息
    quote = body.get("quote")
    if quote:
        if quote.get("msgtype") == "text" and quote.get("text", {}).get("content"):
            if not text_parts:
                text_parts.append(quote["text"]["content"])
        elif quote.get("msgtype") == "voice" and quote.get("voice", {}).get("content"):
            if not text_parts:
                text_parts.append(quote["voice"]["content"])
        elif quote.get("msgtype") == "image" and quote.get("image", {}).get("url"):
            image_urls.append(quote["image"]["url"])

    text = "\n".join(text_parts).strip()

    # 对于纯媒体消息（无文本），生成描述
    if not text and (image_urls or msgtype in ("file", "video")):
        label = {"image": "图片", "voice": "语音", "video": "视频", "file": "文件"}.get(msgtype, "文件")
        text = f"[用户发送了{label}]"

    return text, image_urls


# ── WecomChannel ─────────────────────────────────────────────────────────────

class WecomChannel(BaseChannel):
    """WeCom Bot WebSocket channel using official wecom-aibot-sdk."""

    def __init__(self, send_stream: ObjectSendStream[IncomingMessage]) -> None:
        super().__init__(send_stream)
        self._client: WSClient | None = None
        self._download_dir = settings.config_dir / "channels" / "wecom" / "downloads"
        self._download_dir.mkdir(parents=True, exist_ok=True)

    def _check_config(self) -> bool:
        missing = [
            name for name, val in [
                ("WECOM_BOT_ID", settings.wecom_bot_id),
                ("WECOM_SECRET", settings.wecom_secret),
            ] if not val
        ]
        if missing:
            log.error("WeCom channel: missing config: %s", ", ".join(missing))
            return False
        return True

    async def start(self) -> None:
        if not self._check_config():
            await self.send_stream.aclose()
            return

        self._client = WSClient(
            bot_id=settings.wecom_bot_id,
            secret=settings.wecom_secret,
            heartbeat_interval=30000,
            max_reconnect_attempts=10,
            max_auth_failure_attempts=5,
        )

        # Register event handlers
        self._client.on("connected", self._on_connected)
        self._client.on("authenticated", self._on_authenticated)
        self._client.on("disconnected", self._on_disconnected)
        self._client.on("error", self._on_error)
        self._client.on("message", self._on_message)
        self._client.on("event", self._on_event)

        try:
            await self._client.connect()
            # Keep running until cancelled
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("wecom: unexpected error in start loop")
        finally:
            if self._client:
                await self._client.disconnect()
            await self.send_stream.aclose()

    # ── Event handlers ────────────────────────────────────────────────

    def _on_connected(self) -> None:
        log.info("wecom: WebSocket connected")

    def _on_authenticated(self) -> None:
        log.info("wecom: authenticated successfully")
        # Expose WSClient to the wecom_mcp tool
        from ..tools.wecom_mcp import set_ws_client
        set_ws_client(self._client)

    def _on_disconnected(self, reason: str) -> None:
        log.warning("wecom: disconnected: %s", reason)
        from ..tools.wecom_mcp import set_ws_client
        set_ws_client(None)

    def _on_error(self, error: Exception) -> None:
        log.error("wecom: error: %s", error)

    async def _on_message(self, frame: WsFrame) -> None:
        """Handle incoming message callback."""
        body = frame.get("body") or {}
        msgtype = body.get("msgtype", "")
        from_user = body.get("from", {}).get("userid", "")
        chat_id = body.get("chatid") or from_user

        if not from_user:
            return

        text, image_urls = _parse_message_body(body)

        # Download images to local files
        if image_urls:
            for url in image_urls:
                aes_key = body.get("image", {}).get("aeskey")
                local_path = await self._download_media(url, aes_key)
                if local_path:
                    text += f"\n文件路径: {local_path}"

        if not text:
            return

        log.debug("wecom: inbound from %s: %s", from_user, text[:80])
        await self.send_stream.send(
            IncomingMessage(
                text=text,
                user_id=from_user,
                channel="wecom",
                raw={
                    "frame": frame,
                    "chat_id": chat_id,
                    "chat_type": body.get("chattype", "single"),
                },
            )
        )

    async def _on_event(self, frame: WsFrame) -> None:
        """Handle event callbacks (template card clicks, etc.)."""
        body = frame.get("body") or {}
        event = body.get("event", {})
        event_type = event.get("eventtype", "")

        # For now, log events but don't route them
        log.debug("wecom: event received: type=%s", event_type)

    # ── Media download ────────────────────────────────────────────────

    async def _download_media(self, url: str, aes_key: str | None = None) -> str | None:
        """Download and optionally decrypt a media file."""
        if not self._client:
            return None
        try:
            result = await self._client.download_file(url, aes_key)
            buffer: bytes = result["buffer"]
            filename: str | None = result.get("filename")

            if not filename:
                # Guess extension from content
                import magic  # noqa: F401 - optional
                ext = ".bin"
                filename = f"media_{generate_req_id('dl')}{ext}"

            dest = self._download_dir / filename
            dest.write_bytes(buffer)
            return str(dest)
        except ImportError:
            # No python-magic, use a generic extension
            pass
        except Exception:
            log.exception("wecom: failed to download media from %s", url)
        return None

    # ── Outbound reply ────────────────────────────────────────────────

    async def send_reply(self, msg: OutgoingMessage) -> None:
        if not self._client or not self._client.is_connected:
            log.warning("wecom: cannot send reply, not connected")
            return

        if msg.type != MessageType.text:
            return

        raw = msg.data or {}
        chat_id = raw.get("chat_id") or msg.user_id
        file_path: str | None = raw.get("file_path") or raw.get("image_path")

        if file_path:
            await self._send_file(chat_id, file_path)
            return

        # Send text as markdown via proactive send
        try:
            await self._client.send_message(chat_id, {
                "msgtype": "markdown",
                "markdown": {"content": msg.text},
            })
        except Exception:
            log.exception("wecom: failed to send text to %s", chat_id)

    async def _send_file(self, chat_id: str, file_path: str) -> None:
        """Upload and send a file/image."""
        if not self._client:
            return

        try:
            p = Path(file_path)
            if not p.exists():
                log.error("wecom: file not found: %s", file_path)
                return

            media_type = _detect_media_type(file_path)
            file_data = p.read_bytes()

            upload_result = await self._client.upload_media(
                file_data,
                type=media_type,  # type: ignore[arg-type]
                filename=p.name,
            )
            media_id = upload_result["media_id"]

            await self._client.send_media_message(
                chat_id,
                media_type=media_type,  # type: ignore[arg-type]
                media_id=media_id,
            )
        except Exception:
            log.exception("wecom: failed to send file %s to %s", file_path, chat_id)
