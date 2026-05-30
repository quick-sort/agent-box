"""QQ Bot Official API channel adapter (WebSocket gateway).

Protocol overview:
  1. POST bots.qq.com/app/getAppAccessToken → access_token
  2. GET  api.sgroup.qq.com/gateway         → WebSocket URL
  3. Connect WS → receive Hello (op=10) → send Identify (op=2)
  4. Receive Dispatch (op=0) events for incoming messages
  5. Heartbeat loop: send op=1 at server-specified interval

user_id format:
  "c2c:{user_openid}"    — private (C2C) chat
  "group:{group_openid}" — group @-message

Attachment handling:
  Incoming: image attachments are downloaded locally; local paths are injected
  into IncomingMessage.text as "[图片: /path]" and into raw["image_paths"].
  Outgoing: set msg.data = {"image_url": "...", "image_path": "..."} to send
  an image; the channel uploads to QQ CDN then sends msg_type=7.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import random
import re
import time
from pathlib import Path
from typing import Any

import anyio
import httpx

from ..config import settings
from ..models import IncomingMessage, MessageType, OutgoingMessage
from .base import BaseChannel

log = logging.getLogger(__name__)

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"

# Intents bitmask: GROUP_AND_C2C | PUBLIC_GUILD_MESSAGES | DIRECT_MESSAGE | INTERACTION
INTENTS = (1 << 25) | (1 << 30) | (1 << 12) | (1 << 26)

# WebSocket opcodes
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

_MENTION_RE = re.compile(r"<@!?\w+>\s*")

RECONNECT_DELAY = 5.0  # seconds between reconnect attempts

# QQ Bot media file_type values (API enum: 1=image 2=video 3=voice 4=file)
_FILE_TYPE_IMAGE = 1
_FILE_TYPE_VIDEO = 2
_FILE_TYPE_VOICE = 3
_FILE_TYPE_FILE = 4

# Upload size limits per type (matching official TS SDK)
_UPLOAD_SIZE_LIMITS: dict[int, int] = {
    _FILE_TYPE_IMAGE: 30 * 1024 * 1024,   # 30MB
    _FILE_TYPE_VIDEO: 100 * 1024 * 1024,  # 100MB
    _FILE_TYPE_VOICE: 20 * 1024 * 1024,   # 20MB
    _FILE_TYPE_FILE: 100 * 1024 * 1024,   # 100MB
}

# File extension → media type mapping
_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})
_VIDEO_EXTS = frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv"})
_AUDIO_EXTS = frozenset({".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a", ".silk"})

# MD5_10M: hash of the first 10_002_432 bytes (QQ protocol requirement)
_MD5_10M_SIZE = 10_002_432

_DOWNLOAD_TIMEOUT_S = 60.0
_PUT_CHUNK_TIMEOUT_S = 300.0  # 5 min per part upload


def _next_msg_seq() -> int:
    return (int(time.time() * 1000) ^ random.randint(0, 65535)) % 65536


def _normalize_url(url: str) -> str:
    if url.startswith("//"):
        return f"https:{url}"
    return url


def _detect_file_type(file_path: str) -> int:
    """Detect QQ API file_type from file extension."""
    ext = Path(file_path).suffix.lower()
    if ext in _IMAGE_EXTS:
        return _FILE_TYPE_IMAGE
    if ext in _VIDEO_EXTS:
        return _FILE_TYPE_VIDEO
    if ext in _AUDIO_EXTS:
        return _FILE_TYPE_VOICE
    return _FILE_TYPE_FILE


def _format_size(n: int) -> str:
    """Human-readable file size."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f}TB"


class QQChannel(BaseChannel):
    """QQ Bot channel via the Official Bot API WebSocket gateway."""

    def __init__(self, send_stream: anyio.abc.ObjectSendStream[IncomingMessage]) -> None:
        super().__init__(send_stream)
        self._app_id = settings.qqbot_app_id
        self._client_secret = settings.qqbot_client_secret
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        self._session_id: str | None = None
        self._last_seq: int | None = None
        # Latest msg_id per user_id; used as passive-reply anchor
        self._last_msg_id: dict[str, str] = {}
        self._download_dir: Path = settings.config_dir / "channels" / "qq" / "downloads"

    # ── Auth ──────────────────────────────────────────────────────────────────

    async def _get_token(self) -> str:
        if self._access_token and time.time() < self._token_expires_at - 300:
            return self._access_token
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                TOKEN_URL,
                json={"appId": self._app_id, "clientSecret": self._client_secret},
            )
            resp.raise_for_status()
            data = resp.json()
        token: str = data["access_token"]
        self._access_token = token
        self._token_expires_at = time.time() + int(data.get("expires_in", 7200))
        log.info("QQ Bot token refreshed (expires_in=%s)", data.get("expires_in"))
        return token

    # ── REST helpers ──────────────────────────────────────────────────────────

    async def _api_get(self, path: str) -> dict[str, Any]:
        token = await self._get_token()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{API_BASE}{path}",
                headers={"Authorization": f"QQBot {token}"},
            )
            resp.raise_for_status()
            return resp.json()

    async def _api_post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        token = await self._get_token()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{API_BASE}{path}",
                json=body,
                headers={"Authorization": f"QQBot {token}"},
            )
        if not resp.is_success:
            log.error("QQ API %s → %s: %s", path, resp.status_code, resp.text[:300])
        return resp.json() if resp.content else {}

    # ── Chunked upload helpers ──────────────────────────────────────────────

    async def _compute_file_hashes(self, file_path: str) -> dict[str, str]:
        """Compute md5, sha1, md5_10m hashes for a file (in thread)."""

        def _hash() -> dict[str, str]:
            md5 = hashlib.md5()
            sha1 = hashlib.sha1()
            md5_10m = hashlib.md5()
            size = Path(file_path).stat().st_size
            need_10m = size > _MD5_10M_SIZE
            read_10m = 0
            with open(file_path, "rb") as f:
                while chunk := f.read(65536):
                    md5.update(chunk)
                    sha1.update(chunk)
                    if need_10m and read_10m < _MD5_10M_SIZE:
                        take = min(len(chunk), _MD5_10M_SIZE - read_10m)
                        md5_10m.update(chunk[:take])
                        read_10m += take
            return {
                "md5": md5.hexdigest(),
                "sha1": sha1.hexdigest(),
                "md5_10m": md5_10m.hexdigest() if need_10m else md5.hexdigest(),
            }

        return await anyio.to_thread.run_sync(_hash)

    async def _read_chunk_and_hash(
        self, file_path: str, offset: int, length: int
    ) -> tuple[bytes, str]:
        """Read a chunk of a file and compute its MD5 (in thread)."""

        def _read() -> tuple[bytes, str]:
            with open(file_path, "rb") as f:
                f.seek(offset)
                data = f.read(length)
            return data, hashlib.md5(data).hexdigest()

        return await anyio.to_thread.run_sync(_read)

    async def _put_chunk(self, url: str, data: bytes) -> bool:
        """PUT bytes to a presigned COS URL. Returns True on success."""
        try:
            async with httpx.AsyncClient(timeout=_PUT_CHUNK_TIMEOUT_S) as client:
                resp = await client.put(url, content=data)
            if resp.is_success:
                return True
            log.error("QQ PUT chunk failed: %s %s", resp.status_code, resp.text[:200])
            return False
        except Exception:
            log.exception("QQ PUT chunk exception")
            return False

    async def _chunked_upload(
        self,
        target_id: str,
        target_type: str,
        file_path: str,
        file_type: int,
    ) -> str | None:
        """Upload a file using QQ's chunked upload protocol.

        Returns file_info string on success, None on failure.

        Protocol (matching official TS SDK):
        1. upload_prepare → upload_id, block_size, parts with presigned URLs
        2. For each part: read chunk → PUT to presigned URL → upload_part_finish
        3. complete_upload → file_info
        """
        p = Path(file_path)
        if not p.is_file():
            log.error("chunked_upload: file not found: %s", file_path)
            return None
        file_size = p.stat().st_size
        if file_size == 0:
            log.error("chunked_upload: file is empty: %s", file_path)
            return None
        max_size = _UPLOAD_SIZE_LIMITS.get(file_type, 100 * 1024 * 1024)
        if file_size > max_size:
            log.error(
                "chunked_upload: file too large (%s > %s)",
                _format_size(file_size), _format_size(max_size),
            )
            return None

        file_name = p.name
        prefix = "c2c" if target_type == "c2c" else "group"

        # 1. Compute hashes
        hashes = await self._compute_file_hashes(file_path)
        log.info(
            "QQ chunked upload [%s]: %s (%s, type=%d) hashes computed",
            prefix, file_name, _format_size(file_size), file_type,
        )

        # 2. upload_prepare
        prepare_path = (
            f"/v2/users/{target_id}/upload_prepare"
            if target_type == "c2c"
            else f"/v2/groups/{target_id}/upload_prepare"
        )
        prepare_resp = await self._api_post(prepare_path, {
            "file_type": file_type,
            "file_name": file_name,
            "file_size": file_size,
            **hashes,
        })
        upload_id: str | None = prepare_resp.get("upload_id")
        block_size_raw = prepare_resp.get("block_size", 0)
        parts: list[dict[str, Any]] = prepare_resp.get("parts") or []
        if not upload_id or not parts:
            log.error("chunked_upload: prepare failed: %s", prepare_resp)
            return None
        block_size = int(block_size_raw)
        log.info(
            "QQ chunked upload [%s]: prepared upload_id=%s block_size=%s parts=%d",
            prefix, upload_id, _format_size(block_size), len(parts),
        )

        # 3. Upload each part
        finish_path = (
            f"/v2/users/{target_id}/upload_part_finish"
            if target_type == "c2c"
            else f"/v2/groups/{target_id}/upload_part_finish"
        )
        for i, part in enumerate(parts, 1):
            idx: int = part["index"]
            url: str = part["presigned_url"]
            offset = (idx - 1) * block_size
            length = min(block_size, file_size - offset)

            chunk_data, part_md5 = await self._read_chunk_and_hash(file_path, offset, length)

            ok = await self._put_chunk(url, chunk_data)
            if not ok:
                log.error("chunked_upload: PUT part %d/%d failed", i, len(parts))
                return None

            await self._api_post(finish_path, {
                "upload_id": upload_id,
                "part_index": idx,
                "block_size": length,
                "md5": part_md5,
            })
            log.debug("chunked_upload: part %d/%d done", i, len(parts))

        # 4. Complete upload
        complete_path = (
            f"/v2/users/{target_id}/files"
            if target_type == "c2c"
            else f"/v2/groups/{target_id}/files"
        )
        result = await self._api_post(complete_path, {"upload_id": upload_id})
        file_info = result.get("file_info")
        if not file_info:
            log.error("chunked_upload: complete failed: %s", result)
            return None
        log.info("QQ chunked upload [%s]: complete, file_info received", prefix)
        return file_info

    async def _send_media(
        self,
        target_id: str,
        target_type: str,
        file_path: str,
        *,
        msg_id: str | None = None,
    ) -> None:
        """Upload a file (any type) and send as QQ media message."""
        file_type = _detect_file_type(file_path)
        file_info = await self._chunked_upload(target_id, target_type, file_path, file_type)
        if not file_info:
            log.error("QQ: media upload failed for %s, skipping send", file_path)
            return

        body: dict[str, Any] = {
            "msg_type": 7,
            "media": {"file_info": file_info},
            "msg_seq": _next_msg_seq(),
        }
        if msg_id:
            body["msg_id"] = msg_id

        endpoint = (
            f"/v2/users/{target_id}/messages"
            if target_type == "c2c"
            else f"/v2/groups/{target_id}/messages"
        )
        try:
            await self._api_post(endpoint, body)
            log.info("QQ: sent media message (type=%d) to %s:%s", file_type, target_type, target_id)
        except Exception:
            log.exception("QQ: failed to send media message")

    # ── Media helpers ─────────────────────────────────────────────────────────

    async def _download_image(self, url: str, filename: str | None = None) -> str | None:
        """Download image from URL to local storage, return path or None."""
        url = _normalize_url(url)
        self._download_dir.mkdir(parents=True, exist_ok=True)

        suffix = Path(filename).suffix if filename else ""
        stamp = f"{int(time.time() * 1000)}-{random.randint(0, 9999)}"
        dest = self._download_dir / f"img-{stamp}{suffix or '.bin'}"

        try:
            async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT_S) as client:
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    if not suffix:
                        ct = resp.headers.get("content-type", "").split(";")[0].strip()
                        ext = mimetypes.guess_extension(ct) if ct else None
                        if ext:
                            dest = dest.with_suffix(ext)
                    dest.write_bytes(await resp.aread())
            log.debug("QQ: image saved to %s", dest)
            return str(dest)
        except Exception:
            log.exception("QQ: failed to download image from %s", url)
            return None

    async def _upload_image(
        self,
        target_id: str,
        target_type: str,
        *,
        image_url: str | None = None,
        image_path: str | None = None,
    ) -> str | None:
        """Upload image to QQ CDN; returns file_info string or None."""
        body: dict[str, Any] = {"file_type": _FILE_TYPE_IMAGE, "srv_send_msg": False}

        if image_url:
            body["url"] = _normalize_url(image_url)
        elif image_path:
            try:
                raw = Path(image_path).read_bytes()
                body["file_data"] = base64.b64encode(raw).decode("ascii")
            except Exception:
                log.exception("QQ: failed to read image file %s", image_path)
                return None
        else:
            return None

        endpoint = f"/v2/users/{target_id}/files" if target_type == "c2c" else f"/v2/groups/{target_id}/files"
        try:
            result = await self._api_post(endpoint, body)
            return result.get("file_info") or None
        except Exception:
            log.exception("QQ: image upload failed")
            return None

    async def _send_image(
        self,
        target_id: str,
        target_type: str,
        *,
        image_url: str | None = None,
        image_path: str | None = None,
        msg_id: str | None = None,
    ) -> None:
        """Upload image and send as QQ media message (msg_type=7)."""
        file_info = await self._upload_image(
            target_id, target_type, image_url=image_url, image_path=image_path
        )
        if not file_info:
            log.error("QQ: image upload failed, skipping send")
            return

        body: dict[str, Any] = {
            "msg_type": 7,
            "media": {"file_info": file_info},
            "msg_seq": _next_msg_seq(),
        }
        if msg_id:
            body["msg_id"] = msg_id

        endpoint = f"/v2/users/{target_id}/messages" if target_type == "c2c" else f"/v2/groups/{target_id}/messages"
        try:
            await self._api_post(endpoint, body)
        except Exception:
            log.exception("QQ: failed to send image message")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if not self._app_id or not self._client_secret:
            log.error(
                "QQ Bot credentials not configured. "
                "Set QQBOT_APP_ID and QQBOT_CLIENT_SECRET."
            )
            await self.send_stream.aclose()
            return
        try:
            while True:
                try:
                    await self._run_gateway()
                except Exception:
                    log.exception("QQ gateway error, reconnecting in %.0fs", RECONNECT_DELAY)
                    await anyio.sleep(RECONNECT_DELAY)
        finally:
            await self.send_stream.aclose()

    async def _run_gateway(self) -> None:
        import websockets

        gw = await self._api_get("/gateway")
        ws_url = gw["url"]
        log.info("QQ Bot connecting to gateway: %s", ws_url)

        async with websockets.connect(ws_url) as ws:
            # First frame is always Hello
            hello_raw = await ws.recv()
            hello = json.loads(hello_raw)
            if hello.get("op") != OP_HELLO:
                raise RuntimeError(f"Expected Hello (op=10), got op={hello.get('op')}")
            interval = hello["d"]["heartbeat_interval"] / 1000.0
            log.debug("QQ Hello received, heartbeat_interval=%.1fs", interval)

            token = await self._get_token()
            if self._session_id is not None:
                await ws.send(json.dumps({
                    "op": OP_RESUME,
                    "d": {
                        "token": f"QQBot {token}",
                        "session_id": self._session_id,
                        "seq": self._last_seq,
                    },
                }))
                log.info("QQ sent RESUME (session=%s, seq=%s)", self._session_id, self._last_seq)
            else:
                await ws.send(json.dumps({
                    "op": OP_IDENTIFY,
                    "d": {
                        "token": f"QQBot {token}",
                        "intents": INTENTS,
                        "shard": [0, 1],
                    },
                }))
                log.info("QQ sent IDENTIFY (intents=%d)", INTENTS)

            async with anyio.create_task_group() as tg:
                async def _heartbeat() -> None:
                    while True:
                        await anyio.sleep(interval)
                        await ws.send(json.dumps({"op": OP_HEARTBEAT, "d": self._last_seq}))
                        log.debug("QQ heartbeat sent (seq=%s)", self._last_seq)

                async def _recv() -> None:
                    async for raw in ws:
                        await self._on_payload(json.loads(raw))

                tg.start_soon(_heartbeat)
                tg.start_soon(_recv)

    async def _on_payload(self, payload: dict[str, Any]) -> None:
        op = payload.get("op")
        seq = payload.get("s")
        if seq is not None:
            self._last_seq = seq

        if op == OP_DISPATCH:
            await self._on_dispatch(payload.get("t"), payload.get("d") or {})
        elif op == OP_HEARTBEAT_ACK:
            log.debug("QQ heartbeat ACK")
        elif op == OP_INVALID_SESSION:
            log.warning("QQ invalid session — clearing state and reconnecting")
            self._session_id = None
            self._last_seq = None
            raise Exception("QQ invalid session")
        elif op == OP_RECONNECT:
            log.info("QQ server requested reconnect")
            raise Exception("QQ server requested reconnect")

    async def _on_dispatch(self, event: str | None, data: dict[str, Any]) -> None:
        if event == "READY":
            self._session_id = data.get("session_id")
            log.info("QQ Bot ready (session=%s)", self._session_id)
        elif event == "RESUMED":
            log.info("QQ Bot session resumed")
        elif event == "C2C_MESSAGE_CREATE":
            await self._handle_c2c(data)
        elif event in ("GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"):
            await self._handle_group(data)

    # ── Attachment extraction ─────────────────────────────────────────────────

    def _extract_all_atts(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Return all attachment entries from the event's attachments list."""
        return [
            a for a in (data.get("attachments") or [])
            if isinstance(a, dict)
        ]

    def _att_label(self, att: dict[str, Any]) -> str:
        """Human-readable label for an attachment."""
        ct = (att.get("content_type") or "").split(";")[0].strip()
        if ct.startswith("image/"):
            return "图片"
        if ct.startswith("video/"):
            return "视频"
        if ct.startswith("audio/"):
            return "语音"
        return "文件"

    async def _download_atts(
        self, atts: list[dict[str, Any]]
    ) -> list[tuple[str, str]]:
        """Download each attachment. Returns list of (label, local_path)."""
        results: list[tuple[str, str]] = []
        for att in atts:
            url = _normalize_url(att.get("url") or "")
            label = self._att_label(att)
            if url:
                local = await self._download_image(url, att.get("filename"))
                results.append((label, local or ""))
            else:
                results.append((label, ""))
        return results

    def _atts_text(self, atts_info: list[tuple[str, str]]) -> str:
        """Build text description lines for all attachments."""
        parts: list[str] = []
        for label, path in atts_info:
            if path:
                parts.append(f"用户发送了一个{label}，文件路径: {path}")
            else:
                parts.append(f"用户发送了一个{label}，文件路径: (下载失败)")
        return "\n".join(parts)

    # Keep old methods as thin wrappers for backward compatibility
    def _extract_image_atts(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            a for a in (data.get("attachments") or [])
            if isinstance(a, dict) and (a.get("content_type") or "").startswith("image/")
        ]

    async def _download_image_atts(
        self, image_atts: list[dict[str, Any]]
    ) -> tuple[list[str], list[str]]:
        urls: list[str] = []
        local_paths: list[str] = []
        for att in image_atts:
            url = _normalize_url(att.get("url") or "")
            urls.append(url)
            if url:
                local = await self._download_image(url, att.get("filename"))
                local_paths.append(local or "")
            else:
                local_paths.append("")
        return local_paths, urls

    # ── Inbound message handlers ──────────────────────────────────────────────

    async def _handle_c2c(self, data: dict[str, Any]) -> None:
        author = data.get("author") or {}
        openid = author.get("user_openid") or author.get("id", "")
        text = (data.get("content") or "").strip()
        msg_id = data.get("id", "")
        if not openid:
            return

        all_atts = self._extract_all_atts(data)
        if all_atts:
            atts_info = await self._download_atts(all_atts)
            att_text = self._atts_text(atts_info)
            text = f"{text}\n{att_text}".strip() if text else att_text

        if not text:
            return

        user_id = f"c2c:{openid}"
        self._last_msg_id[user_id] = msg_id
        log.debug("QQ c2c from %s: %s", openid, text[:80])
        await self.send_stream.send(IncomingMessage(
            text=text,
            user_id=user_id,
            channel="qq",
            raw={
                "type": "c2c",
                "target_id": openid,
                "msg_id": msg_id,
            },
        ))

    async def _handle_group(self, data: dict[str, Any]) -> None:
        group_openid = data.get("group_openid", "")
        text = _MENTION_RE.sub("", data.get("content") or "").strip()
        msg_id = data.get("id", "")
        if not group_openid:
            return

        all_atts = self._extract_all_atts(data)
        if all_atts:
            atts_info = await self._download_atts(all_atts)
            att_text = self._atts_text(atts_info)
            text = f"{text}\n{att_text}".strip() if text else att_text

        if not text:
            return

        user_id = f"group:{group_openid}"
        self._last_msg_id[user_id] = msg_id
        log.debug("QQ group %s: %s", group_openid, text[:80])
        await self.send_stream.send(IncomingMessage(
            text=text,
            user_id=user_id,
            channel="qq",
            raw={
                "type": "group",
                "target_id": group_openid,
                "msg_id": msg_id,
            },
        ))

    # ── Outbound ──────────────────────────────────────────────────────────────

    async def send_reply(self, msg: OutgoingMessage) -> None:
        if msg.type != MessageType.text:
            return
        if ":" not in msg.user_id:
            log.warning("QQ: unexpected user_id format %r", msg.user_id)
            return

        msg_type, target_id = msg.user_id.split(":", 1)
        msg_id = self._last_msg_id.get(msg.user_id)

        data = msg.data or {}

        # Generic file/media sending via chunked upload
        file_path: str | None = data.get("file_path")
        if file_path and msg_type in ("c2c", "group"):
            await self._send_media(target_id, msg_type, file_path, msg_id=msg_id)
            return

        # Legacy image sending (backward compatible)
        image_url: str | None = data.get("image_url")
        image_path: str | None = data.get("image_path")
        if image_url or image_path:
            if msg_type in ("c2c", "group"):
                await self._send_image(
                    target_id, msg_type,
                    image_url=image_url,
                    image_path=image_path,
                    msg_id=msg_id,
                )
            else:
                log.warning("QQ: unknown msg_type %r for image send", msg_type)
            return

        # Text sending (Markdown, msg_type=2)
        body: dict[str, Any] = {
            "msg_type": 2,
            "msg_seq": _next_msg_seq(),
            "markdown": {"content": msg.text},
        }
        if msg_id:
            body["msg_id"] = msg_id
        if msg_type == "c2c":
            await self._api_post(f"/v2/users/{target_id}/messages", body)
        elif msg_type == "group":
            await self._api_post(f"/v2/groups/{target_id}/messages", body)
        else:
            log.warning("QQ: unknown msg_type %r in user_id %r", msg_type, msg.user_id)
