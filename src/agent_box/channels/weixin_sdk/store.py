from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from .constants import DEFAULT_STATE_DIR
from .models import AccountCredentials, JSONDict, LoginSession


class StateStore:
    def __init__(self, root_dir: str | Path | None = None) -> None:
        self.root_dir = Path(root_dir) if root_dir else DEFAULT_STATE_DIR
        self.accounts_dir = self.root_dir / "accounts"
        self.login_sessions_dir = self.root_dir / "login-sessions"

    def ensure(self) -> None:
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        self.login_sessions_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _encode_key(raw: str) -> str:
        return quote(raw, safe="")

    @staticmethod
    def _decode_key(raw: str) -> str:
        return unquote(raw)

    def _account_base_path(self, account_id: str) -> Path:
        return self.accounts_dir / self._encode_key(account_id)

    def _session_path(self, session_key: str) -> Path:
        return self.login_sessions_dir / f"{self._encode_key(session_key)}.json"

    @staticmethod
    def _read_json(path: Path) -> JSONDict | None:
        try:
            if not path.exists():
                return None
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _write_json(path: Path, data: JSONDict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)

    def save_account(self, credentials: AccountCredentials) -> None:
        self.ensure()
        path = self._account_base_path(credentials.account_id).with_suffix(".account.json")
        self._write_json(path, credentials.to_dict())

    def load_account(self, account_id: str) -> AccountCredentials | None:
        path = self._account_base_path(account_id).with_suffix(".account.json")
        data = self._read_json(path)
        return AccountCredentials.from_dict(data) if data else None

    def list_accounts(self) -> list[AccountCredentials]:
        self.ensure()
        result: list[AccountCredentials] = []
        for file_path in sorted(self.accounts_dir.glob("*.account.json")):
            data = self._read_json(file_path)
            if data:
                result.append(AccountCredentials.from_dict(data))
        return result

    def save_sync_buffer(self, account_id: str, get_updates_buf: str) -> None:
        self.ensure()
        path = self._account_base_path(account_id).with_suffix(".sync.json")
        self._write_json(path, {"get_updates_buf": get_updates_buf})

    def load_sync_buffer(self, account_id: str) -> str | None:
        path = self._account_base_path(account_id).with_suffix(".sync.json")
        data = self._read_json(path)
        value = data.get("get_updates_buf") if data else None
        return str(value) if isinstance(value, str) else None

    def save_context_tokens(self, account_id: str, tokens: dict[str, str]) -> None:
        self.ensure()
        path = self._account_base_path(account_id).with_suffix(".context.json")
        self._write_json(path, {"tokens": tokens})

    def load_context_tokens(self, account_id: str) -> dict[str, str]:
        path = self._account_base_path(account_id).with_suffix(".context.json")
        data = self._read_json(path)
        if not data:
            return {}
        tokens = data.get("tokens")
        if not isinstance(tokens, dict):
            return {}
        result: dict[str, str] = {}
        for user_id, token in tokens.items():
            if isinstance(user_id, str) and isinstance(token, str) and token:
                result[user_id] = token
        return result

    def set_context_token(self, account_id: str, user_id: str, token: str) -> None:
        tokens = self.load_context_tokens(account_id)
        tokens[user_id] = token
        self.save_context_tokens(account_id, tokens)

    def get_context_token(self, account_id: str, user_id: str) -> str | None:
        return self.load_context_tokens(account_id).get(user_id)

    def save_login_session(self, session: LoginSession) -> None:
        self.ensure()
        self._write_json(self._session_path(session.session_key), session.to_dict())

    def load_login_session(self, session_key: str) -> LoginSession | None:
        data = self._read_json(self._session_path(session_key))
        return LoginSession.from_dict(data) if data else None

    def delete_login_session(self, session_key: str) -> None:
        path = self._session_path(session_key)
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def save_json_snapshot(self, relative_path: str, payload: dict[str, Any]) -> Path:
        path = self.root_dir / relative_path
        self._write_json(path, payload)
        return path

    def save_active_account_id(self, account_id: str) -> None:
        self._write_json(self.root_dir / "active_account.json", {"account_id": account_id})

    def load_active_account_id(self) -> str | None:
        data = self._read_json(self.root_dir / "active_account.json")
        return str(data["account_id"]) if data and data.get("account_id") else None
