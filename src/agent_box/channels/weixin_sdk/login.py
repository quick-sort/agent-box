from __future__ import annotations

import time
from typing import Any, Callable
from urllib.parse import quote
from uuid import uuid4

from .client import WeixinClient
from .constants import (
    ACTIVE_LOGIN_TTL_S,
    DEFAULT_BASE_URL,
    DEFAULT_BOT_TYPE,
    MAX_QR_REFRESH_COUNT,
    QR_LONG_POLL_TIMEOUT_S,
)
from .models import AccountCredentials, LoginSession, LoginStartResult, LoginWaitResult
from .store import StateStore

EventCallback = Callable[[str, dict[str, Any]], None]


def _emit(callback: EventCallback | None, event: str, payload: dict[str, Any]) -> None:
    if callback:
        callback(event, payload)


class LoginClient:
    def __init__(
        self,
        *,
        client: WeixinClient | None = None,
        store: StateStore | None = None,
    ) -> None:
        self.store = store or StateStore()
        self.client = client or WeixinClient(store=self.store)

    def _login_client(self, base_url: str = DEFAULT_BASE_URL) -> WeixinClient:
        return WeixinClient(
            base_url=base_url,
            cdn_base_url=self.client.cdn_base_url,
            app_id=self.client.app_id,
            channel_version=self.client.channel_version,
            token=None,
            store=self.store,
        )

    @staticmethod
    def _is_login_fresh(session: LoginSession) -> bool:
        return time.time() - session.started_at < ACTIVE_LOGIN_TTL_S

    def _fetch_qrcode(self, bot_type: str) -> dict[str, Any]:
        return self._login_client().get_json(
            f"ilink/bot/get_bot_qrcode?bot_type={quote(bot_type, safe='')}",
            timeout_s=10.0,
        )

    def start(
        self,
        *,
        session_key: str | None = None,
        bot_type: str = DEFAULT_BOT_TYPE,
        force: bool = False,
        event_callback: EventCallback | None = None,
    ) -> LoginStartResult:
        resolved_session_key = session_key or str(uuid4())
        existing = self.store.load_login_session(resolved_session_key)
        if existing and self._is_login_fresh(existing) and not force:
            return LoginStartResult(
                qrcode_url=existing.qrcode_url,
                message="二维码已就绪，请使用微信扫描。",
                session_key=resolved_session_key,
            )

        response = self._fetch_qrcode(bot_type)
        session = LoginSession(
            session_key=resolved_session_key,
            qrcode=str(response["qrcode"]),
            qrcode_url=str(response["qrcode_img_content"]),
            started_at=time.time(),
            current_api_base_url=DEFAULT_BASE_URL,
            bot_type=bot_type,
        )
        self.store.save_login_session(session)
        _emit(
            event_callback,
            "qr_ready",
            {"session_key": session.session_key, "qrcode_url": session.qrcode_url},
        )
        return LoginStartResult(
            qrcode_url=session.qrcode_url,
            message="使用微信扫描以下二维码，以完成连接。",
            session_key=session.session_key,
        )

    def _poll_status(self, session: LoginSession) -> dict[str, Any]:
        try:
            return self._login_client(session.current_api_base_url).get_json(
                f"ilink/bot/get_qrcode_status?qrcode={quote(session.qrcode, safe='')}",
                timeout_s=QR_LONG_POLL_TIMEOUT_S,
            )
        except Exception:
            return {"status": "wait"}

    def wait(
        self,
        *,
        session_key: str,
        timeout_s: float = 480.0,
        event_callback: EventCallback | None = None,
    ) -> LoginWaitResult:
        session = self.store.load_login_session(session_key)
        if not session:
            return LoginWaitResult(
                connected=False,
                message="当前没有进行中的登录，请先发起登录。",
            )
        if not self._is_login_fresh(session):
            self.store.delete_login_session(session_key)
            return LoginWaitResult(
                connected=False,
                message="二维码已过期，请重新生成。",
            )

        deadline = time.time() + max(timeout_s, 1.0)
        qr_refresh_count = 1
        scanned_notified = False

        while time.time() < deadline:
            status = self._poll_status(session)
            current_status = status.get("status")
            if current_status == "wait":
                time.sleep(1.0)
                continue
            if current_status == "scaned":
                if not scanned_notified:
                    scanned_notified = True
                    _emit(event_callback, "scanned", {"session_key": session_key})
                time.sleep(1.0)
                continue
            if current_status == "scaned_but_redirect":
                redirect_host = status.get("redirect_host")
                if redirect_host:
                    session.current_api_base_url = f"https://{redirect_host}"
                    self.store.save_login_session(session)
                    _emit(
                        event_callback,
                        "redirected",
                        {
                            "session_key": session_key,
                            "base_url": session.current_api_base_url,
                        },
                    )
                time.sleep(1.0)
                continue
            if current_status == "expired":
                qr_refresh_count += 1
                if qr_refresh_count > MAX_QR_REFRESH_COUNT:
                    self.store.delete_login_session(session_key)
                    return LoginWaitResult(
                        connected=False,
                        message="登录超时：二维码多次过期，请重新开始登录流程。",
                    )
                response = self._fetch_qrcode(session.bot_type)
                session.qrcode = str(response["qrcode"])
                session.qrcode_url = str(response["qrcode_img_content"])
                session.started_at = time.time()
                session.current_api_base_url = DEFAULT_BASE_URL
                self.store.save_login_session(session)
                scanned_notified = False
                _emit(
                    event_callback,
                    "qr_refreshed",
                    {
                        "session_key": session_key,
                        "qrcode_url": session.qrcode_url,
                        "refresh_count": qr_refresh_count,
                    },
                )
                time.sleep(1.0)
                continue
            if current_status == "confirmed":
                account_id = status.get("ilink_bot_id")
                if not account_id:
                    self.store.delete_login_session(session_key)
                    return LoginWaitResult(
                        connected=False,
                        message="登录失败：服务器未返回 ilink_bot_id。",
                    )
                credentials = AccountCredentials(
                    account_id=str(account_id),
                    token=str(status.get("bot_token") or ""),
                    base_url=str(status.get("baseurl") or DEFAULT_BASE_URL),
                    user_id=str(status.get("ilink_user_id")) if status.get("ilink_user_id") else None,
                )
                self.store.save_account(credentials)
                self.store.save_active_account_id(credentials.account_id)
                self.store.delete_login_session(session_key)
                _emit(
                    event_callback,
                    "confirmed",
                    {
                        "session_key": session_key,
                        "account_id": credentials.account_id,
                        "user_id": credentials.user_id,
                        "base_url": credentials.base_url,
                    },
                )
                return LoginWaitResult(
                    connected=True,
                    message="✅ 与微信连接成功！",
                    bot_token=credentials.token,
                    account_id=credentials.account_id,
                    base_url=credentials.base_url,
                    user_id=credentials.user_id,
                )
            time.sleep(1.0)

        self.store.delete_login_session(session_key)
        return LoginWaitResult(
            connected=False,
            message="登录超时，请重试。",
        )

    def login_with_qr(
        self,
        *,
        session_key: str | None = None,
        timeout_s: float = 480.0,
        bot_type: str = DEFAULT_BOT_TYPE,
        force: bool = False,
        event_callback: EventCallback | None = None,
    ) -> LoginWaitResult:
        started = self.start(
            session_key=session_key,
            bot_type=bot_type,
            force=force,
            event_callback=event_callback,
        )
        return self.wait(
            session_key=started.session_key,
            timeout_s=timeout_s,
            event_callback=event_callback,
        )
