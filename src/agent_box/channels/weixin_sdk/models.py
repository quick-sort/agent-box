from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


JSONDict = dict[str, Any]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class AccountCredentials:
    account_id: str
    token: str
    base_url: str
    user_id: str | None = None
    saved_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> JSONDict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: JSONDict) -> "AccountCredentials":
        return cls(
            account_id=str(data["account_id"]),
            token=str(data["token"]),
            base_url=str(data["base_url"]),
            user_id=str(data["user_id"]) if data.get("user_id") else None,
            saved_at=str(data.get("saved_at") or utc_now_iso()),
        )


@dataclass(slots=True)
class LoginSession:
    session_key: str
    qrcode: str
    qrcode_url: str
    started_at: float
    current_api_base_url: str
    bot_type: str

    def to_dict(self) -> JSONDict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: JSONDict) -> "LoginSession":
        return cls(
            session_key=str(data["session_key"]),
            qrcode=str(data["qrcode"]),
            qrcode_url=str(data["qrcode_url"]),
            started_at=float(data["started_at"]),
            current_api_base_url=str(data["current_api_base_url"]),
            bot_type=str(data["bot_type"]),
        )


@dataclass(slots=True)
class LoginStartResult:
    qrcode_url: str | None
    message: str
    session_key: str


@dataclass(slots=True)
class LoginWaitResult:
    connected: bool
    message: str
    bot_token: str | None = None
    account_id: str | None = None
    base_url: str | None = None
    user_id: str | None = None

    def to_dict(self) -> JSONDict:
        return {
            "connected": self.connected,
            "message": self.message,
            "bot_token": self.bot_token,
            "account_id": self.account_id,
            "base_url": self.base_url,
            "user_id": self.user_id,
        }


@dataclass(slots=True)
class PollResponse:
    ret: int | None
    errcode: int | None
    errmsg: str | None
    messages: list[JSONDict]
    get_updates_buf: str | None
    longpolling_timeout_ms: int | None = None

    @classmethod
    def from_dict(cls, data: JSONDict) -> "PollResponse":
        raw_messages = data.get("msgs") or []
        return cls(
            ret=int(data["ret"]) if data.get("ret") is not None else None,
            errcode=int(data["errcode"]) if data.get("errcode") is not None else None,
            errmsg=str(data["errmsg"]) if data.get("errmsg") else None,
            messages=[msg for msg in raw_messages if isinstance(msg, dict)],
            get_updates_buf=str(data["get_updates_buf"]) if data.get("get_updates_buf") is not None else None,
            longpolling_timeout_ms=(
                int(data["longpolling_timeout_ms"])
                if data.get("longpolling_timeout_ms") is not None
                else None
            ),
        )

    def to_dict(self) -> JSONDict:
        return {
            "ret": self.ret,
            "errcode": self.errcode,
            "errmsg": self.errmsg,
            "msgs": self.messages,
            "get_updates_buf": self.get_updates_buf,
            "longpolling_timeout_ms": self.longpolling_timeout_ms,
        }


@dataclass(slots=True)
class UploadedFileInfo:
    filekey: str
    download_encrypted_query_param: str
    aeskey_hex: str
    file_size: int
    file_size_ciphertext: int

    def to_dict(self) -> JSONDict:
        return asdict(self)
