from __future__ import annotations

import base64
import hashlib
import mimetypes
from pathlib import Path
from secrets import token_hex
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from .constants import (
    CDN_UPLOAD_MAX_RETRIES,
    DEFAULT_API_TIMEOUT_S,
    ITEM_TYPE_FILE,
    ITEM_TYPE_IMAGE,
    ITEM_TYPE_VIDEO,
    ITEM_TYPE_VOICE,
    UPLOAD_MEDIA_TYPE_FILE,
    UPLOAD_MEDIA_TYPE_IMAGE,
    UPLOAD_MEDIA_TYPE_VIDEO,
)
from .crypto import aes_ecb_padded_size, decrypt_aes_ecb, encrypt_aes_ecb, parse_aes_key_base64
from .exceptions import WeixinApiError, WeixinError
from .messages import iter_media_items, resolve_output_filename
from .models import UploadedFileInfo

if TYPE_CHECKING:
    from .client import AccountClient


def _build_cdn_download_url(encrypted_query_param: str, cdn_base_url: str) -> str:
    return (
        f"{cdn_base_url.rstrip('/')}/download"
        f"?encrypted_query_param={quote(encrypted_query_param, safe='')}"
    )


def _build_cdn_upload_url(cdn_base_url: str, upload_param: str, filekey: str) -> str:
    return (
        f"{cdn_base_url.rstrip('/')}/upload"
        f"?encrypted_query_param={quote(upload_param, safe='')}"
        f"&filekey={quote(filekey, safe='')}"
    )


def _guess_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    return mime or "application/octet-stream"


def _guess_upload_media_type(path: Path, forced_kind: str | None = None) -> tuple[int, str]:
    if forced_kind == "image":
        return UPLOAD_MEDIA_TYPE_IMAGE, "image"
    if forced_kind == "video":
        return UPLOAD_MEDIA_TYPE_VIDEO, "video"
    if forced_kind == "file":
        return UPLOAD_MEDIA_TYPE_FILE, "file"

    mime = _guess_mime(path)
    if mime.startswith("image/"):
        return UPLOAD_MEDIA_TYPE_IMAGE, "image"
    if mime.startswith("video/"):
        return UPLOAD_MEDIA_TYPE_VIDEO, "video"
    return UPLOAD_MEDIA_TYPE_FILE, "file"


def _build_media_item(kind: str, uploaded: UploadedFileInfo, file_name: str | None = None) -> dict[str, Any]:
    media = {
        "encrypt_query_param": uploaded.download_encrypted_query_param,
        "aes_key": base64.b64encode(bytes.fromhex(uploaded.aeskey_hex)).decode("ascii"),
        "encrypt_type": 1,
    }
    if kind == "image":
        return {
            "type": ITEM_TYPE_IMAGE,
            "image_item": {
                "media": media,
                "mid_size": uploaded.file_size_ciphertext,
            },
        }
    if kind == "video":
        return {
            "type": ITEM_TYPE_VIDEO,
            "video_item": {
                "media": media,
                "video_size": uploaded.file_size_ciphertext,
            },
        }
    return {
        "type": ITEM_TYPE_FILE,
        "file_item": {
            "media": media,
            "file_name": file_name or "file.bin",
            "len": str(uploaded.file_size),
        },
    }


def _infer_extension_from_bytes(item: dict[str, Any], raw: bytes) -> str:
    item_type = item.get("type")

    if item_type == ITEM_TYPE_IMAGE:
        if raw.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if raw.startswith((b"GIF87a", b"GIF89a")):
            return ".gif"
        if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
            return ".webp"
        if raw.startswith(b"BM"):
            return ".bmp"
        return ".bin"

    if item_type == ITEM_TYPE_VIDEO:
        if len(raw) > 12 and raw[4:8] == b"ftyp":
            return ".mp4"
        return ".bin"

    if item_type == ITEM_TYPE_VOICE:
        if raw.startswith(b"#!SILK"):
            return ".silk"
        if raw.startswith(b"RIFF") and raw[8:12] == b"WAVE":
            return ".wav"
        if raw.startswith(b"ID3"):
            return ".mp3"
        return ".bin"

    if item_type == ITEM_TYPE_FILE:
        file_item = item.get("file_item") or {}
        original_name = file_item.get("file_name")
        if isinstance(original_name, str) and Path(original_name).suffix:
            return Path(original_name).suffix
        if raw.startswith(b"%PDF-"):
            return ".pdf"
        if raw.startswith(b"PK\x03\x04"):
            return ".zip"
        return ".bin"

    return ".bin"


class MediaClient:
    def __init__(self, account: "AccountClient") -> None:
        self.account = account

    def upload_file(
        self,
        *,
        file_path: str | Path,
        to_user_id: str,
        forced_kind: str | None = None,
    ) -> tuple[str, UploadedFileInfo]:
        path = Path(file_path).expanduser().resolve()
        if not path.exists():
            raise WeixinError(f"文件不存在: {path}")

        media_type, kind = _guess_upload_media_type(path, forced_kind)
        plaintext = path.read_bytes()
        rawsize = len(plaintext)
        rawfilemd5 = hashlib.md5(plaintext).hexdigest()
        filesize = aes_ecb_padded_size(rawsize)
        filekey = token_hex(16)
        aeskey = token_hex(16)

        response = self.account.client.post_json(
            "ilink/bot/getuploadurl",
            {
                "filekey": filekey,
                "media_type": media_type,
                "to_user_id": to_user_id,
                "rawsize": rawsize,
                "rawfilemd5": rawfilemd5,
                "filesize": filesize,
                "no_need_thumb": True,
                "aeskey": aeskey,
                "base_info": self.account.client.build_base_info(),
            },
        )
        upload_full_url = (response.get("upload_full_url") or "").strip()
        upload_param = response.get("upload_param")
        if not upload_full_url and not upload_param:
            raise WeixinError("getuploadurl 未返回 upload_full_url 或 upload_param")

        ciphertext = encrypt_aes_ecb(plaintext, bytes.fromhex(aeskey))
        upload_url = upload_full_url or _build_cdn_upload_url(
            self.account.client.cdn_base_url,
            str(upload_param),
            filekey,
        )
        last_error: Exception | None = None
        header_map: dict[str, str] = {}
        for attempt in range(1, CDN_UPLOAD_MAX_RETRIES + 1):
            try:
                _, header_map = self.account.client.post_bytes(
                    upload_url,
                    ciphertext,
                    headers={"Content-Type": "application/octet-stream"},
                    timeout_s=DEFAULT_API_TIMEOUT_S,
                )
                break
            except WeixinApiError as exc:
                last_error = exc
                if exc.status_code is not None and 400 <= exc.status_code < 500:
                    raise
                if attempt == CDN_UPLOAD_MAX_RETRIES:
                    raise
            except Exception as exc:
                last_error = exc
                if attempt == CDN_UPLOAD_MAX_RETRIES:
                    raise
        download_param = (
            header_map.get("x-encrypted-param")
            or header_map.get("X-Encrypted-Param")
        )
        if not download_param:
            raise WeixinError(
                "CDN 上传成功但响应头缺少 x-encrypted-param"
            ) from last_error
        return kind, UploadedFileInfo(
            filekey=filekey,
            download_encrypted_query_param=download_param,
            aeskey_hex=aeskey,
            file_size=rawsize,
            file_size_ciphertext=filesize,
        )

    def send_file(
        self,
        *,
        file_path: str | Path,
        to_user_id: str,
        caption: str = "",
        context_token: str | None = None,
        forced_kind: str | None = None,
    ) -> str:
        kind, uploaded = self.upload_file(
            file_path=file_path,
            to_user_id=to_user_id,
            forced_kind=forced_kind,
        )
        resolved_context = self.account.store.get_context_token(
            self.account.account_id,
            to_user_id,
        )
        if context_token:
            resolved_context = context_token
        if caption:
            self.account.send_text(
                to_user_id=to_user_id,
                text=caption,
                context_token=resolved_context,
            )
        item = _build_media_item(kind, uploaded, Path(file_path).name)
        return self.account.send_item(
            to_user_id=to_user_id,
            item=item,
            context_token=resolved_context,
        )

    def _download_url_for_item(self, item: dict[str, Any]) -> tuple[str, str | None]:
        item_type = item.get("type")
        media: dict[str, Any] | None = None
        aes_key_b64: str | None = None
        if item_type == ITEM_TYPE_IMAGE:
            image_item = item.get("image_item") or {}
            media = image_item.get("media") or {}
            image_aes = image_item.get("aeskey")
            if image_aes:
                aes_key_b64 = base64.b64encode(bytes.fromhex(str(image_aes))).decode("ascii")
            elif media.get("aes_key"):
                aes_key_b64 = str(media["aes_key"])
        elif item_type == ITEM_TYPE_VIDEO:
            video_item = item.get("video_item") or {}
            media = video_item.get("media") or {}
            if media.get("aes_key"):
                aes_key_b64 = str(media["aes_key"])
        elif item_type == ITEM_TYPE_FILE:
            file_item = item.get("file_item") or {}
            media = file_item.get("media") or {}
            if media.get("aes_key"):
                aes_key_b64 = str(media["aes_key"])
        elif item_type == ITEM_TYPE_VOICE:
            voice_item = item.get("voice_item") or {}
            media = voice_item.get("media") or {}
            if media.get("aes_key"):
                aes_key_b64 = str(media["aes_key"])

        if not media:
            raise WeixinError("消息项不包含可下载的 media 字段")
        full_url = media.get("full_url")
        if isinstance(full_url, str) and full_url:
            return full_url, aes_key_b64
        encrypted_query_param = media.get("encrypt_query_param")
        if not isinstance(encrypted_query_param, str) or not encrypted_query_param:
            raise WeixinError("消息项缺少 full_url 和 encrypt_query_param")
        return _build_cdn_download_url(encrypted_query_param, self.account.client.cdn_base_url), aes_key_b64

    def download_media(
        self,
        item: dict[str, Any],
        *,
        output_dir: str | Path,
    ) -> Path:
        url, aes_key_b64 = self._download_url_for_item(item)
        raw = self.account.client.fetch_bytes(url)
        if aes_key_b64:
            raw = decrypt_aes_ecb(raw, parse_aes_key_base64(aes_key_b64))
        output_path = Path(output_dir).expanduser().resolve() / resolve_output_filename(item)
        suffix = _infer_extension_from_bytes(item, raw)
        if output_path.suffix.lower() != suffix.lower():
            output_path = output_path.with_suffix(suffix)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(raw)
        return output_path

    def download_message_media(
        self,
        message: dict[str, Any],
        *,
        output_dir: str | Path,
    ) -> list[Path]:
        return [
            self.download_media(item, output_dir=output_dir)
            for item in iter_media_items(message)
        ]
