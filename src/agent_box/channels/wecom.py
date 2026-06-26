"""WeCom (企业微信) channel adapter.

Inbound: HTTP webhook receiver (WeCom Bot callback mode).
  - GET  /wecom  → URL verification (echostr decryption)
  - POST /wecom  → message callback (AES decrypt → parse → emit)

Outbound: WeCom Agent HTTP API (corpId + corpSecret + agentId).

Required config:
  WECOM_TOKEN            — Webhook 验证 token
  WECOM_ENCODING_AES_KEY — 43-char Base64 AES key
  WECOM_CORP_ID          — 企业 ID (corpId)
  WECOM_CORP_SECRET      — 应用 secret
  WECOM_AGENT_ID         — 应用 agentId
  WECOM_WEBHOOK_PORT     — HTTP 监听端口 (default 8088)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import mimetypes
import random
import struct
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import anyio
import anyio.from_thread
import httpx
from anyio.abc import ObjectSendStream

from ..config import settings
from ..models import IncomingMessage, MessageType, OutgoingMessage
from .base import BaseChannel

log = logging.getLogger(__name__)

WECOM_GET_TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
WECOM_SEND_MSG_URL = "https://qyapi.weixin.qq.com/cgi-bin/message/send"
WECOM_UPLOAD_MEDIA_URL = "https://qyapi.weixin.qq.com/cgi-bin/media/upload"
WECOM_DOWNLOAD_MEDIA_URL = "https://qyapi.weixin.qq.com/cgi-bin/media/get"

_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})
_AUDIO_EXTS = frozenset({".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a", ".amr"})


# ── WeCom AES crypto (PKCS#7 + AES-CBC) ─────────────────────────────────────

def _wecom_sha1(*parts: str) -> str:
    return hashlib.sha1("".join(sorted(parts)).encode()).hexdigest()


def _verify_signature(token: str, timestamp: str, nonce: str, *extras: str) -> str:
    return _wecom_sha1(token, timestamp, nonce, *extras)


class _WecomCrypto:
    """Minimal WeCom AES-256-CBC encrypt/decrypt (mirrors @wecom/aibot-node-sdk WecomCrypto)."""

    def __init__(self, token: str, encoding_aes_key: str, receive_id: str) -> None:
        self.token = token
        self.receive_id = receive_id
        raw = base64.b64decode(encoding_aes_key + "=")  # pad to multiple of 4
        self._key = raw[:32]
        self._iv = raw[:16]

    def verify_signature(self, signature: str, timestamp: str, nonce: str, encrypt: str) -> bool:
        expected = _wecom_sha1(self.token, timestamp, nonce, encrypt)
        return hmac.compare_digest(expected, signature)

    def decrypt(self, encrypt: str) -> str:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        raw = base64.b64decode(encrypt)
        cipher = Cipher(algorithms.AES(self._key), modes.CBC(self._iv))
        dec = cipher.decryptor()
        plain = dec.update(raw) + dec.finalize()
        # skip random 16-byte prefix, then 4-byte big-endian length
        content_len = struct.unpack(">I", plain[16:20])[0]
        content = plain[20 : 20 + content_len].decode("utf-8")
        return content

    def encrypt(self, plain: str) -> tuple[str, str, str]:
        """Returns (encrypt_b64, timestamp, nonce)."""
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        nonce = str(random.randint(100000, 999999))
        timestamp = str(int(time.time()))
        content = plain.encode("utf-8")
        rand16 = bytes(random.getrandbits(8) for _ in range(16))
        msg = rand16 + struct.pack(">I", len(content)) + content + self.receive_id.encode()
        # PKCS#7 pad to 32-byte blocks
        pad = 32 - len(msg) % 32
        msg += bytes([pad]) * pad
        cipher = Cipher(algorithms.AES(self._key), modes.CBC(self._iv))
        enc = cipher.encryptor()
        encrypted = enc.update(msg) + enc.finalize()
        encrypt_b64 = base64.b64encode(encrypted).decode()
        sig = _wecom_sha1(self.token, timestamp, nonce, encrypt_b64)
        return encrypt_b64, timestamp, nonce


# ── WeCom Agent API client ───────────────────────────────────────────────────

class _WecomAgentClient:
    def __init__(self, corp_id: str, corp_secret: str, agent_id: int) -> None:
        self._corp_id = corp_id
        self._corp_secret = corp_secret
        self._agent_id = agent_id
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    async def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 300:
            return self._token
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                WECOM_GET_TOKEN_URL,
                params={"corpid": self._corp_id, "corpsecret": self._corp_secret},
            )
            resp.raise_for_status()
            data = resp.json()
        if not data.get("access_token"):
            raise RuntimeError(f"WeCom gettoken failed: {data}")
        self._token = data["access_token"]
        self._token_expires_at = time.time() + data.get("expires_in", 7200)
        return self._token

    async def send_text(self, to_user: str, text: str) -> None:
        token = await self._get_token()
        body: dict[str, Any] = {
            "touser": to_user,
            "msgtype": "text",
            "agentid": self._agent_id,
            "text": {"content": text},
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{WECOM_SEND_MSG_URL}?access_token={token}",
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
        if data.get("errcode", 0) != 0:
            raise RuntimeError(f"WeCom send_text failed: {data}")

    async def upload_media(self, file_path: str, media_type: str) -> str:
        """Upload temporary media, return media_id."""
        token = await self._get_token()
        p = Path(file_path)
        suffix = p.suffix.lower()
        mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        content = p.read_bytes()
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{WECOM_UPLOAD_MEDIA_URL}?access_token={token}&type={media_type}",
                files={"media": (p.name, content, mime)},
            )
            resp.raise_for_status()
            data = resp.json()
        if not data.get("media_id"):
            raise RuntimeError(f"WeCom upload_media failed: {data}")
        return data["media_id"]

    async def send_media(self, to_user: str, media_id: str, media_type: str) -> None:
        token = await self._get_token()
        body: dict[str, Any] = {
            "touser": to_user,
            "msgtype": media_type,
            "agentid": self._agent_id,
            media_type: {"media_id": media_id},
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{WECOM_SEND_MSG_URL}?access_token={token}",
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
        if data.get("errcode", 0) != 0:
            raise RuntimeError(f"WeCom send_media failed: {data}")

    async def download_media(self, media_id: str, dest_dir: Path) -> str | None:
        token = await self._get_token()
        dest_dir.mkdir(parents=True, exist_ok=True)
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                async with client.stream(
                    "GET",
                    f"{WECOM_DOWNLOAD_MEDIA_URL}?access_token={token}&media_id={media_id}",
                ) as resp:
                    resp.raise_for_status()
                    ct = resp.headers.get("content-type", "").split(";")[0].strip()
                    ext = mimetypes.guess_extension(ct) if ct else None
                    dest = dest_dir / f"media-{media_id}{ext or '.bin'}"
                    dest.write_bytes(await resp.aread())
            return str(dest)
        except Exception:
            log.exception("wecom: failed to download media_id=%s", media_id)
            return None


# ── Inbound message parsing ──────────────────────────────────────────────────

def _parse_wecom_xml(xml_text: str) -> dict[str, str]:
    """Parse WeCom callback XML into a flat dict."""
    root = ET.fromstring(xml_text)
    return {child.tag: (child.text or "") for child in root}


def _detect_media_type(file_path: str) -> str:
    ext = Path(file_path).suffix.lower()
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _AUDIO_EXTS:
        return "voice"
    return "file"


# ── HTTP webhook server ──────────────────────────────────────────────────────

async def _handle_http(
    request_method: str,
    query: dict[str, str],
    body_bytes: bytes,
    crypto: _WecomCrypto,
    send_stream: ObjectSendStream[IncomingMessage],
    agent: _WecomAgentClient,
    download_dir: Path,
) -> tuple[int, str, str]:
    """Handle one HTTP request. Returns (status, content_type, body)."""
    msg_sig = query.get("msg_signature", query.get("signature", ""))
    timestamp = query.get("timestamp", "")
    nonce = query.get("nonce", "")

    if request_method == "GET":
        echostr = query.get("echostr", "")
        if not all([msg_sig, timestamp, nonce, echostr]):
            return 400, "text/plain", "missing params"
        if not crypto.verify_signature(msg_sig, timestamp, nonce, echostr):
            return 403, "text/plain", "signature mismatch"
        try:
            plain = crypto.decrypt(echostr)
            return 200, "text/plain", plain
        except Exception:
            log.exception("wecom: echostr decrypt failed")
            return 403, "text/plain", "decrypt failed"

    if request_method == "POST":
        if not body_bytes:
            return 400, "text/plain", "empty body"

        # WeCom POST body is XML with <Encrypt> field
        try:
            root = ET.fromstring(body_bytes.decode("utf-8"))
            encrypt = (root.findtext("Encrypt") or "").strip()
        except Exception:
            return 400, "text/plain", "invalid xml"

        if not encrypt:
            return 400, "text/plain", "missing Encrypt"

        if not crypto.verify_signature(msg_sig, timestamp, nonce, encrypt):
            return 403, "text/plain", "signature mismatch"

        try:
            plain = crypto.decrypt(encrypt)
        except Exception:
            log.exception("wecom: message decrypt failed")
            return 400, "text/plain", "decrypt failed"

        msg = _parse_wecom_xml(plain)
        msg_type = msg.get("MsgType", "")
        from_user = msg.get("FromUserName", "")

        if not from_user:
            return 200, "text/plain", ""

        text = ""
        if msg_type == "text":
            text = msg.get("Content", "").strip()
        elif msg_type in ("image", "voice", "file", "video"):
            media_id = msg.get("MediaId", "")
            label = {"image": "图片", "voice": "语音", "video": "视频"}.get(msg_type, "文件")
            if media_id:
                local = await agent.download_media(media_id, download_dir)
                if local:
                    text = f"用户发送了一个{label}，文件路径: {local}"
                else:
                    text = f"用户发送了一个{label}，文件路径: (下载失败)"
            else:
                text = f"用户发送了一个{label}"
        elif msg_type == "event":
            # Ignore events (enter_chat, etc.)
            return 200, "text/plain", ""
        else:
            log.debug("wecom: unhandled msgtype=%s", msg_type)
            return 200, "text/plain", ""

        if not text:
            return 200, "text/plain", ""

        log.debug("wecom: inbound from %s: %s", from_user, text[:80])
        await send_stream.send(
            IncomingMessage(
                text=text,
                user_id=from_user,
                channel="wecom",
                raw=msg,
            )
        )
        return 200, "text/plain", ""

    return 405, "text/plain", "method not allowed"


def _parse_query(url: str) -> dict[str, str]:
    idx = url.find("?")
    if idx < 0:
        return {}
    from urllib.parse import parse_qs
    qs = parse_qs(url[idx + 1 :], keep_blank_values=True)
    return {k: v[0] for k, v in qs.items()}


async def _serve_http(
    port: int,
    path: str,
    crypto: _WecomCrypto,
    send_stream: ObjectSendStream[IncomingMessage],
    agent: _WecomAgentClient,
    download_dir: Path,
) -> None:
    """Minimal async HTTP server using anyio TCP sockets."""
    listener = await anyio.create_tcp_listener(local_port=port, local_host="0.0.0.0")
    log.info("wecom webhook listening on port %d at %s", port, path)

    async def handle(stream: anyio.abc.ByteStream) -> None:
        try:
            # Read full HTTP request (until headers end)
            raw = b""
            async with stream:
                while b"\r\n\r\n" not in raw:
                    chunk = await stream.receive(4096)
                    if not chunk:
                        break
                    raw += chunk

                header_end = raw.index(b"\r\n\r\n")
                header_bytes = raw[:header_end]
                body_so_far = raw[header_end + 4 :]

                lines = header_bytes.decode("latin-1").split("\r\n")
                request_line = lines[0]
                parts = request_line.split(" ")
                method = parts[0] if parts else "GET"
                url = parts[1] if len(parts) > 1 else "/"

                headers: dict[str, str] = {}
                for line in lines[1:]:
                    if ":" in line:
                        k, _, v = line.partition(":")
                        headers[k.strip().lower()] = v.strip()

                content_length = int(headers.get("content-length", "0"))
                while len(body_so_far) < content_length:
                    chunk = await stream.receive(4096)
                    if not chunk:
                        break
                    body_so_far += chunk

                req_path = url.split("?")[0]
                if req_path != path:
                    resp = b"HTTP/1.1 404 Not Found\r\nContent-Length: 9\r\n\r\nNot Found"
                    await stream.send(resp)
                    return

                query = _parse_query(url)
                status, ct, body_str = await _handle_http(
                    method, query, body_so_far, crypto, send_stream, agent, download_dir
                )
                body_b = body_str.encode("utf-8")
                resp = (
                    f"HTTP/1.1 {status} OK\r\n"
                    f"Content-Type: {ct}; charset=utf-8\r\n"
                    f"Content-Length: {len(body_b)}\r\n"
                    f"Connection: close\r\n\r\n"
                ).encode() + body_b
                await stream.send(resp)
        except Exception:
            log.exception("wecom: HTTP handler error")

    async with listener:
        async with anyio.create_task_group() as tg:
            async for stream in listener:  # type: ignore[attr-defined]
                tg.start_soon(handle, stream)


# ── WecomChannel ─────────────────────────────────────────────────────────────

class WecomChannel(BaseChannel):
    """WeCom Bot webhook channel + Agent API for sending."""

    def __init__(self, send_stream: ObjectSendStream[IncomingMessage]) -> None:
        super().__init__(send_stream)
        self._crypto = _WecomCrypto(
            token=settings.wecom_token,
            encoding_aes_key=settings.wecom_encoding_aes_key,
            receive_id=settings.wecom_corp_id,
        )
        self._agent = _WecomAgentClient(
            corp_id=settings.wecom_corp_id,
            corp_secret=settings.wecom_corp_secret,
            agent_id=settings.wecom_agent_id,
        )
        self._port = settings.wecom_webhook_port
        self._path = settings.wecom_webhook_path
        self._download_dir = settings.config_dir / "channels" / "wecom" / "downloads"

    def _check_config(self) -> bool:
        missing = [
            name for name, val in [
                ("WECOM_TOKEN", settings.wecom_token),
                ("WECOM_ENCODING_AES_KEY", settings.wecom_encoding_aes_key),
                ("WECOM_CORP_ID", settings.wecom_corp_id),
                ("WECOM_CORP_SECRET", settings.wecom_corp_secret),
                ("WECOM_AGENT_ID", str(settings.wecom_agent_id)),
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
        try:
            await _serve_http(
                port=self._port,
                path=self._path,
                crypto=self._crypto,
                send_stream=self.send_stream,
                agent=self._agent,
                download_dir=self._download_dir,
            )
        finally:
            await self.send_stream.aclose()

    async def send_reply(self, msg: OutgoingMessage) -> None:
        if msg.type != MessageType.text:
            return

        data = msg.data or {}
        file_path: str | None = data.get("file_path") or data.get("image_path")
        if file_path:
            try:
                media_type = _detect_media_type(file_path)
                media_id = await self._agent.upload_media(file_path, media_type)
                await self._agent.send_media(msg.user_id, media_id, media_type)
            except Exception:
                log.exception("wecom: failed to send file %s", file_path)
            return

        try:
            await self._agent.send_text(msg.user_id, msg.text)
        except Exception:
            log.exception("wecom: failed to send text to %s", msg.user_id)
