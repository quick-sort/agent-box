from __future__ import annotations

import base64
import json
import socket
import ssl
from dataclasses import asdict
from http.client import HTTPResponse
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from .constants import (
    DEFAULT_API_TIMEOUT_S,
    DEFAULT_APP_ID,
    DEFAULT_BASE_URL,
    DEFAULT_CDN_BASE_URL,
    DEFAULT_CHANNEL_VERSION,
    DEFAULT_CONFIG_TIMEOUT_S,
    DEFAULT_LONG_POLL_TIMEOUT_S,
    SESSION_EXPIRED_ERRCODE,
    TYPING_STATUS_TYPING,
)
from .exceptions import WeixinApiError, WeixinError
from .messages import build_single_item_request, build_text_message_request, generate_prefixed_id
from .media import MediaClient
from .models import AccountCredentials, JSONDict, PollResponse
from .store import StateStore


def _build_client_version(version: str) -> int:
    parts = version.split(".")
    major = int(parts[0]) if len(parts) > 0 and parts[0].isdigit() else 0
    minor = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    patch = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    return ((major & 0xFF) << 16) | ((minor & 0xFF) << 8) | (patch & 0xFF)


def _random_wechat_uin() -> str:
    import os

    value = int.from_bytes(os.urandom(4), "big")
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


class WeixinClient:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        cdn_base_url: str = DEFAULT_CDN_BASE_URL,
        app_id: str = DEFAULT_APP_ID,
        channel_version: str = DEFAULT_CHANNEL_VERSION,
        token: str | None = None,
        store: StateStore | None = None,
    ) -> None:
        self.base_url = base_url
        self.cdn_base_url = cdn_base_url
        self.app_id = app_id
        self.channel_version = channel_version
        self.client_version = _build_client_version(channel_version)
        self.token = token
        self.store = store or StateStore()

    def clone(
        self,
        *,
        base_url: str | None = None,
        token: str | None | object = None,
    ) -> "WeixinClient":
        resolved_token = self.token if token is None else token
        if resolved_token is NotImplemented:
            resolved_token = None
        return WeixinClient(
            base_url=base_url or self.base_url,
            cdn_base_url=self.cdn_base_url,
            app_id=self.app_id,
            channel_version=self.channel_version,
            token=resolved_token if isinstance(resolved_token, str) else None,
            store=self.store,
        )

    def build_base_info(self) -> JSONDict:
        return {"channel_version": self.channel_version}

    def _common_headers(self) -> dict[str, str]:
        return {
            "iLink-App-Id": self.app_id,
            "iLink-App-ClientVersion": str(self.client_version),
        }

    def _json_headers(self, body: bytes, token: str | None) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Content-Length": str(len(body)),
            "X-WECHAT-UIN": _random_wechat_uin(),
            **self._common_headers(),
        }
        if token:
            headers["Authorization"] = f"Bearer {token.strip()}"
        return headers

    def _request(
        self,
        *,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout_s: float,
    ) -> tuple[bytes, HTTPResponse]:
        request = Request(url=url, data=body, method=method)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            response = urlopen(request, timeout=timeout_s)
            raw = response.read()
            return raw, response
        except HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise WeixinApiError(
                f"{method} {url} failed with HTTP {exc.code}: {body_text}",
                status_code=exc.code,
                response_body=body_text,
            ) from exc
        except URLError as exc:
            if isinstance(exc.reason, socket.timeout):
                raise TimeoutError(f"{method} {url} timed out after {timeout_s}s") from exc
            if isinstance(exc.reason, ssl.SSLError):
                raise WeixinError(f"TLS error while requesting {url}: {exc.reason}") from exc
            raise WeixinError(f"Network error while requesting {url}: {exc.reason}") from exc
        except socket.timeout as exc:
            raise TimeoutError(f"{method} {url} timed out after {timeout_s}s") from exc

    def get_json(
        self,
        endpoint: str,
        *,
        timeout_s: float = DEFAULT_CONFIG_TIMEOUT_S,
        base_url: str | None = None,
    ) -> JSONDict:
        url = urljoin(f"{(base_url or self.base_url).rstrip('/')}/", endpoint)
        raw, _ = self._request(
            method="GET",
            url=url,
            headers=self._common_headers(),
            timeout_s=timeout_s,
        )
        if not raw.strip():
            return {}
        return json.loads(raw.decode("utf-8"))

    def post_json(
        self,
        endpoint: str,
        payload: JSONDict,
        *,
        timeout_s: float = DEFAULT_API_TIMEOUT_S,
        base_url: str | None = None,
        token: str | None = None,
    ) -> JSONDict:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        resolved_token = self.token if token is None else token
        url = urljoin(f"{(base_url or self.base_url).rstrip('/')}/", endpoint)
        raw, _ = self._request(
            method="POST",
            url=url,
            body=body,
            headers=self._json_headers(body, resolved_token),
            timeout_s=timeout_s,
        )
        if not raw.strip():
            return {}
        return json.loads(raw.decode("utf-8"))

    def fetch_bytes(self, url: str, *, timeout_s: float = DEFAULT_API_TIMEOUT_S) -> bytes:
        raw, _ = self._request(method="GET", url=url, headers={}, timeout_s=timeout_s)
        return raw

    def post_bytes(
        self,
        url: str,
        body: bytes,
        *,
        headers: dict[str, str] | None = None,
        timeout_s: float = DEFAULT_API_TIMEOUT_S,
    ) -> tuple[bytes, dict[str, str]]:
        raw, response = self._request(
            method="POST",
            url=url,
            body=body,
            headers=headers,
            timeout_s=timeout_s,
        )
        return raw, dict(response.headers.items())


class AccountClient:
    def __init__(
        self,
        credentials: AccountCredentials,
        *,
        store: StateStore | None = None,
        client: WeixinClient | None = None,
    ) -> None:
        self.credentials = credentials
        self.store = store or StateStore()
        self.client = client or WeixinClient(
            base_url=credentials.base_url,
            token=credentials.token,
            store=self.store,
        )
        self.media = MediaClient(self)

    @classmethod
    def from_store(
        cls,
        account_id: str,
        *,
        store: StateStore | None = None,
    ) -> "AccountClient":
        store = store or StateStore()
        credentials = store.load_account(account_id)
        if not credentials:
            raise WeixinError(f"未找到账号 `{account_id}` 的本地凭据")
        return cls(credentials, store=store)

    @property
    def account_id(self) -> str:
        return self.credentials.account_id

    def _resolve_context_token(self, to_user_id: str, context_token: str | None) -> str | None:
        return context_token or self.store.get_context_token(self.account_id, to_user_id)

    def poll_once(self, *, timeout_s: float = DEFAULT_LONG_POLL_TIMEOUT_S) -> PollResponse:
        sync_buf = self.store.load_sync_buffer(self.account_id) or ""
        payload = {
            "get_updates_buf": sync_buf,
            "base_info": self.client.build_base_info(),
        }
        try:
            response = self.client.post_json(
                "ilink/bot/getupdates",
                payload,
                timeout_s=timeout_s,
            )
        except TimeoutError:
            return PollResponse(
                ret=0,
                errcode=None,
                errmsg=None,
                messages=[],
                get_updates_buf=sync_buf,
            )
        poll = PollResponse.from_dict(response)
        if poll.get_updates_buf:
            self.store.save_sync_buffer(self.account_id, poll.get_updates_buf)
        if poll.errcode == SESSION_EXPIRED_ERRCODE or poll.ret == SESSION_EXPIRED_ERRCODE:
            raise WeixinApiError(
                f"session expired for account `{self.account_id}`",
                response_body=json.dumps(response, ensure_ascii=False),
            )
        for message in poll.messages:
            from_user_id = message.get("from_user_id")
            context_token = message.get("context_token")
            if isinstance(from_user_id, str) and isinstance(context_token, str) and context_token:
                self.store.set_context_token(self.account_id, from_user_id, context_token)
        return poll

    def send_text(
        self,
        *,
        to_user_id: str,
        text: str,
        context_token: str | None = None,
    ) -> str:
        resolved_context = self._resolve_context_token(to_user_id, context_token)
        client_id = generate_prefixed_id()
        body = build_text_message_request(
            to_user_id,
            text,
            context_token=resolved_context,
            client_id=client_id,
        )
        body["base_info"] = self.client.build_base_info()
        self.client.post_json("ilink/bot/sendmessage", body)
        return client_id

    def send_item(
        self,
        *,
        to_user_id: str,
        item: dict[str, Any],
        context_token: str | None = None,
    ) -> str:
        resolved_context = self._resolve_context_token(to_user_id, context_token)
        client_id = generate_prefixed_id()
        body = build_single_item_request(
            to_user_id,
            item,
            context_token=resolved_context,
            client_id=client_id,
        )
        body["base_info"] = self.client.build_base_info()
        self.client.post_json("ilink/bot/sendmessage", body)
        return client_id

    def get_typing_ticket(
        self,
        *,
        user_id: str,
        context_token: str | None = None,
    ) -> str | None:
        resolved_context = self._resolve_context_token(user_id, context_token)
        response = self.client.post_json(
            "ilink/bot/getconfig",
            {
                "ilink_user_id": user_id,
                "context_token": resolved_context,
                "base_info": self.client.build_base_info(),
            },
            timeout_s=DEFAULT_CONFIG_TIMEOUT_S,
        )
        ticket = response.get("typing_ticket")
        return str(ticket) if ticket else None

    def send_typing(
        self,
        *,
        user_id: str,
        typing_ticket: str,
        status: int = TYPING_STATUS_TYPING,
    ) -> None:
        self.client.post_json(
            "ilink/bot/sendtyping",
            {
                "ilink_user_id": user_id,
                "typing_ticket": typing_ticket,
                "status": status,
                "base_info": self.client.build_base_info(),
            },
            timeout_s=DEFAULT_CONFIG_TIMEOUT_S,
        )

    def export_credentials(self) -> dict[str, Any]:
        return asdict(self.credentials)
