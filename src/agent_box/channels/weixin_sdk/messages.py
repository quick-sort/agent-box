from __future__ import annotations

import time
from pathlib import Path
from secrets import token_hex
from typing import Any, Iterable

from .constants import (
    ITEM_TYPE_FILE,
    ITEM_TYPE_IMAGE,
    ITEM_TYPE_TEXT,
    ITEM_TYPE_VIDEO,
    ITEM_TYPE_VOICE,
    MESSAGE_STATE_FINISH,
    MESSAGE_TYPE_BOT,
)


def generate_prefixed_id(prefix: str = "openclaw-weixin") -> str:
    return f"{prefix}:{int(time.time() * 1000)}-{token_hex(4)}"


def build_text_message_request(
    to_user_id: str,
    text: str,
    *,
    context_token: str | None = None,
    client_id: str | None = None,
) -> dict[str, Any]:
    client_id = client_id or generate_prefixed_id()
    item_list: list[dict[str, Any]] = []
    if text:
        item_list.append({"type": ITEM_TYPE_TEXT, "text_item": {"text": text}})
    return {
        "msg": {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": client_id,
            "message_type": MESSAGE_TYPE_BOT,
            "message_state": MESSAGE_STATE_FINISH,
            "item_list": item_list or None,
            "context_token": context_token or None,
        }
    }


def build_single_item_request(
    to_user_id: str,
    item: dict[str, Any],
    *,
    context_token: str | None = None,
    client_id: str | None = None,
) -> dict[str, Any]:
    client_id = client_id or generate_prefixed_id()
    return {
        "msg": {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": client_id,
            "message_type": MESSAGE_TYPE_BOT,
            "message_state": MESSAGE_STATE_FINISH,
            "item_list": [item],
            "context_token": context_token or None,
        }
    }


def extract_text_body(message: dict[str, Any]) -> str:
    item_list = message.get("item_list") or []
    if not isinstance(item_list, list):
        return ""
    for item in item_list:
        if not isinstance(item, dict):
            continue
        if item.get("type") == ITEM_TYPE_TEXT:
            text_item = item.get("text_item") or {}
            text = text_item.get("text")
            if text is not None:
                return str(text)
        if item.get("type") == ITEM_TYPE_VOICE:
            voice_item = item.get("voice_item") or {}
            if voice_item.get("text"):
                return str(voice_item["text"])
    return ""


def iter_media_items(message: dict[str, Any]) -> Iterable[dict[str, Any]]:
    item_list = message.get("item_list") or []
    if not isinstance(item_list, list):
        return []
    media_types = {ITEM_TYPE_IMAGE, ITEM_TYPE_VIDEO, ITEM_TYPE_FILE, ITEM_TYPE_VOICE}
    return [
        item
        for item in item_list
        if isinstance(item, dict) and item.get("type") in media_types
    ]


def summarize_message(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "message_id": message.get("message_id"),
        "from_user_id": message.get("from_user_id"),
        "to_user_id": message.get("to_user_id"),
        "create_time_ms": message.get("create_time_ms"),
        "context_token": message.get("context_token"),
        "text": extract_text_body(message),
        "item_types": [
            item.get("type")
            for item in (message.get("item_list") or [])
            if isinstance(item, dict)
        ],
    }


def resolve_output_filename(item: dict[str, Any]) -> str:
    ts = int(time.time() * 1000)
    suffix = token_hex(4)

    def build(prefix: str, ext: str) -> str:
        return f"{prefix}-{ts}-{suffix}{ext}"

    item_type = item.get("type")
    if item_type == ITEM_TYPE_FILE:
        file_item = item.get("file_item") or {}
        if file_item.get("file_name"):
            return str(file_item["file_name"])
        return build("file", ".bin")
    if item_type == ITEM_TYPE_VIDEO:
        return build("video", ".mp4")
    if item_type == ITEM_TYPE_VOICE:
        return build("voice", ".silk")
    if item_type == ITEM_TYPE_IMAGE:
        return build("image", ".bin")
    return build("media", ".bin")


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
