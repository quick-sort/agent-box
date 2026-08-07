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
import re
from pathlib import Path
from typing import Any, NamedTuple

from anyio.abc import ObjectSendStream

from wecom_aibot_sdk import WSClient, generate_req_id
from wecom_aibot_sdk.types import WsFrame, MessageType as WeComMsgType

from ..config import settings
from ..models import IncomingMessage, MessageType, OutgoingMessage
from .base import BaseChannel

log = logging.getLogger(__name__)

_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})
_VIDEO_EXTS = frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v", ".mpeg", ".mpg"})
# 企微语音消息仅支持 AMR；其他音频格式只能作为普通文件发送。
_VOICE_EXTS = frozenset({".amr"})

# 企微出站媒体大小限制
_IMAGE_MAX_BYTES = 10 * 1024 * 1024
_VIDEO_MAX_BYTES = 10 * 1024 * 1024
_VOICE_MAX_BYTES = 2 * 1024 * 1024
_ABSOLUTE_MAX_BYTES = 20 * 1024 * 1024

_TYPE_MAX_BYTES = {
    "image": _IMAGE_MAX_BYTES,
    "video": _VIDEO_MAX_BYTES,
    "voice": _VOICE_MAX_BYTES,
}

_MEDIA_LABELS = {"image": "图片", "voice": "语音", "video": "视频", "file": "文件"}

# Download timeouts (seconds) — files can be much larger than images.
_IMAGE_DOWNLOAD_TIMEOUT = 60.0
_FILE_DOWNLOAD_TIMEOUT = 180.0

# Magic-byte sniffing for the fallback filename when the server sends no
# Content-Disposition header.
_MAGIC_EXTS: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"%PDF-", ".pdf"),
    (b"PK\x03\x04", ".zip"),  # also docx/xlsx/pptx
    (b"\xd0\xcf\x11\xe0", ".doc"),  # legacy OLE2: doc/xls/ppt
    (b"\x1f\x8b", ".gz"),
    (b"Rar!\x1a\x07", ".rar"),
    (b"7z\xbc\xaf\x27\x1c", ".7z"),
    (b"ID3", ".mp3"),
    (b"#!AMR", ".amr"),
    (b"\x23\x21SILK", ".silk"),
)

_UNSAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

_DEFAULT_EXT_BY_KIND = {"image": ".jpg", "voice": ".amr", "video": ".mp4", "file": ".bin"}


class MediaRef(NamedTuple):
    """A downloadable media attachment referenced by an inbound message."""

    url: str
    aes_key: str | None
    kind: str  # "image" | "file" | "video" | "voice"


def _sniff_ext(buffer: bytes, kind: str) -> str:
    for magic, ext in _MAGIC_EXTS:
        if buffer.startswith(magic):
            return ext
    if buffer[4:12] in (b"ftypmp42", b"ftypisom") or buffer[4:8] == b"ftyp":
        return ".mp4"
    if buffer.startswith(b"RIFF") and buffer[8:12] == b"WEBP":
        return ".webp"
    if buffer.startswith(b"RIFF") and buffer[8:12] == b"WAVE":
        return ".wav"
    return _DEFAULT_EXT_BY_KIND.get(kind, ".bin")


def _safe_filename(raw_name: str | None, buffer: bytes, kind: str) -> str:
    """Build a filesystem-safe filename, sniffing the extension if needed."""
    name = _UNSAFE_NAME_RE.sub("_", Path(raw_name or "").name).strip(" .")
    if not name:
        name = f"media_{generate_req_id('dl')}"
    if not Path(name).suffix:
        name += _sniff_ext(buffer, kind)
    return name


def _detect_media_type(file_path: str) -> str:
    """Map a local file to the WeCom outbound media type.

    WeCom supports ``image`` / ``voice`` / ``video`` / ``file``. Voice is
    AMR-only, so every other audio format falls back to ``file``.
    """
    ext = Path(file_path).suffix.lower()
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _VOICE_EXTS:
        return "voice"
    return "file"


def _apply_size_limits(media_type: str, size: int) -> tuple[str | None, str | None]:
    """Apply WeCom's per-type size caps.

    Returns ``(final_type, note)``. ``final_type`` is None when the file is
    too large to send at all; ``note`` describes a downgrade or rejection.
    """
    size_mb = size / (1024 * 1024)
    if size > _ABSOLUTE_MAX_BYTES:
        return None, (
            f"文件大小 {size_mb:.2f}MB 超过企业微信允许的最大限制 20MB，无法发送。"
        )

    limit = _TYPE_MAX_BYTES.get(media_type)
    if limit is not None and size > limit:
        label = _MEDIA_LABELS.get(media_type, media_type)
        return "file", (
            f"{label}大小 {size_mb:.2f}MB 超过 {limit // (1024 * 1024)}MB 限制，已转为文件格式发送"
        )

    return media_type, None


# ── Message parsing helpers ──────────────────────────────────────────────────

def _parse_message_body(body: dict[str, Any]) -> tuple[str, list[MediaRef]]:
    """Parse a WeCom message body into (text, media refs to download).

    Covers text, voice (already transcribed by WeCom), image, file, video,
    图文混排 (``mixed``) and quoted (``quote``) messages.
    """
    msgtype = body.get("msgtype", "")
    text_parts: list[str] = []
    media: list[MediaRef] = []

    def add_media(payload: Any, kind: str) -> None:
        if not isinstance(payload, dict):
            return
        url = payload.get("url")
        if url:
            media.append(MediaRef(url, payload.get("aeskey"), kind))

    if msgtype == "mixed" and body.get("mixed"):
        # 图文混排消息
        for item in body["mixed"].get("msg_item", []):
            item_type = item.get("msgtype")
            if item_type == "text" and item.get("text", {}).get("content"):
                text_parts.append(item["text"]["content"])
            elif item_type == "image":
                add_media(item.get("image"), "image")
    else:
        # 单条消息
        if body.get("text", {}).get("content"):
            text_parts.append(body["text"]["content"])
        if msgtype == "voice" and body.get("voice", {}).get("content"):
            # 语音转文字
            text_parts.append(body["voice"]["content"])
        add_media(body.get("image"), "image")
        if msgtype == "file":
            add_media(body.get("file"), "file")
        if msgtype == "video":
            add_media(body.get("video"), "video")

    # 处理引用消息
    quote = body.get("quote")
    if quote:
        quote_type = quote.get("msgtype")
        if quote_type == "text" and quote.get("text", {}).get("content"):
            if not text_parts:
                text_parts.append(quote["text"]["content"])
        elif quote_type == "voice" and quote.get("voice", {}).get("content"):
            if not text_parts:
                text_parts.append(quote["voice"]["content"])
        elif quote_type == "image":
            add_media(quote.get("image"), "image")
        elif quote_type == "file":
            add_media(quote.get("file"), "file")
        elif quote_type == "video":
            add_media(quote.get("video"), "video")

    text = "\n".join(text_parts).strip()

    # 对于纯媒体消息（无文本），生成描述
    if not text and media:
        label = _MEDIA_LABELS.get(media[0].kind, "文件")
        text = f"[用户发送了{label}]"

    return text, media


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

        text, media = _parse_message_body(body)

        # Download attachments (image/file/video) to local files so the agent
        # can read them by path.
        local_paths: list[str] = []
        for ref in media:
            label = _MEDIA_LABELS.get(ref.kind, "文件")
            local_path = await self._download_media(ref)
            if local_path:
                local_paths.append(local_path)
                text += f"\n用户发送了一个{label}，文件路径: {local_path}"
            else:
                text += f"\n用户发送了一个{label}，但下载失败"

        if not text:
            return

        log.info(
            "wecom: inbound from %s: msgtype=%s media=%d text_preview=%r",
            from_user, msgtype, len(media), text[:80],
        )
        await self.send_stream.send(
            IncomingMessage(
                text=text,
                user_id=from_user,
                channel="wecom",
                raw={
                    "frame": frame,
                    "chat_id": chat_id,
                    "chat_type": body.get("chattype", "single"),
                    "file_paths": local_paths,
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

    async def _download_media(self, ref: MediaRef) -> str | None:
        """Download (and decrypt) one attachment; return the local path."""
        if not self._client:
            log.warning("wecom: cannot download media, client not ready")
            return None

        timeout = _IMAGE_DOWNLOAD_TIMEOUT if ref.kind == "image" else _FILE_DOWNLOAD_TIMEOUT
        if not ref.aes_key:
            log.warning("wecom: no aeskey for %s media, data may stay encrypted", ref.kind)

        try:
            result = await asyncio.wait_for(
                self._client.download_file(ref.url, ref.aes_key), timeout
            )
        except TimeoutError:
            log.error("wecom: media download timed out after %.0fs: %s", timeout, ref.url)
            return None
        except Exception:
            log.exception("wecom: failed to download media from %s", ref.url)
            return None

        buffer: bytes = result.get("buffer") or b""
        if not buffer:
            log.error("wecom: media download returned empty body: %s", ref.url)
            return None

        filename = _safe_filename(result.get("filename"), buffer, ref.kind)
        dest = self._download_dir / filename
        if dest.exists():
            dest = self._download_dir / f"{Path(filename).stem}_{generate_req_id('dl')}{Path(filename).suffix}"

        try:
            dest.write_bytes(buffer)
        except OSError:
            log.exception("wecom: failed to write media to %s", dest)
            return None

        log.info(
            "wecom: media saved: kind=%s size=%d path=%s", ref.kind, len(buffer), dest
        )
        return str(dest)

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
        await self._send_text(chat_id, msg.text)

    async def _send_text(self, chat_id: str, text: str) -> None:
        """Send a markdown text message."""
        if not self._client:
            return
        try:
            await self._client.send_message(chat_id, {
                "msgtype": "markdown",
                "markdown": {"content": text},
            })
        except Exception:
            log.exception("wecom: failed to send text to %s", chat_id)

    async def _send_file(self, chat_id: str, file_path: str) -> None:
        """Upload and send a local file as image / voice / video / file."""
        if not self._client:
            return

        try:
            p = Path(file_path)
            if not p.is_file():
                log.error("wecom: file not found: %s", file_path)
                return

            file_data = p.read_bytes()
            if not file_data:
                log.error("wecom: refusing to send empty file: %s", file_path)
                return

            detected = _detect_media_type(file_path)
            media_type, note = _apply_size_limits(detected, len(file_data))

            if media_type is None:
                log.error("wecom: %s (%s)", note, file_path)
                await self._send_text(chat_id, f"⚠️ {note}")
                return
            if note:
                log.warning("wecom: %s (%s)", note, file_path)

            upload_result = await self._client.upload_media(
                file_data,
                type=media_type,  # type: ignore[arg-type]
                filename=p.name,
            )
            media_id = upload_result["media_id"]

            send_kwargs: dict[str, Any] = {}
            if media_type == "video":
                # 视频消息支持标题，缺省时企微只显示一个无名视频卡片。
                send_kwargs["video_title"] = p.stem

            await self._client.send_media_message(
                chat_id,
                media_type=media_type,  # type: ignore[arg-type]
                media_id=media_id,
                **send_kwargs,
            )
            log.info(
                "wecom: media sent: type=%s size=%d name=%s chat=%s",
                media_type, len(file_data), p.name, chat_id,
            )
        except Exception:
            log.exception("wecom: failed to send file %s to %s", file_path, chat_id)
