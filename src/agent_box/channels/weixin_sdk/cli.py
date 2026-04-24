from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .client import AccountClient
from .constants import DEFAULT_BOT_TYPE, DEFAULT_STATE_DIR
from .exceptions import WeixinError
from .login import LoginClient
from .messages import iter_media_items, summarize_message
from .store import StateStore


def _default_state_dir() -> str:
    try:
        from ...config import settings
        return str(settings.weixin_state_dir)
    except Exception:
        return str(DEFAULT_STATE_DIR)


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _print_qr_or_url(qrcode_url: str) -> None:
    try:
        import qrcode  # type: ignore
    except ModuleNotFoundError:
        print("二维码链接：")
        print(qrcode_url)
        return

    qr = qrcode.QRCode(border=1)
    qr.add_data(qrcode_url)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    for row in matrix:
        print("".join("██" if cell else "  " for cell in row))
    print("二维码链接：")
    print(qrcode_url)


def _login_event_handler(event: str, payload: dict[str, Any]) -> None:
    if event == "qr_ready":
        print("请使用微信扫描以下二维码：")
        _print_qr_or_url(str(payload["qrcode_url"]))
    elif event == "qr_refreshed":
        print("二维码已刷新，请重新扫码：")
        _print_qr_or_url(str(payload["qrcode_url"]))
    elif event == "scanned":
        print("已扫码，请在微信里继续确认。")
    elif event == "redirected":
        print(f"轮询 host 已切换到 {payload['base_url']}")
    elif event == "confirmed":
        print(f"登录成功，账号：{payload['account_id']}")


def _build_store(state_dir: str | None) -> StateStore:
    return StateStore(state_dir or _default_state_dir())


def _load_account(args: argparse.Namespace) -> AccountClient:
    store = _build_store(args.state_dir)
    return AccountClient.from_store(args.account_id, store=store)


def cmd_accounts(args: argparse.Namespace) -> int:
    store = _build_store(args.state_dir)
    rows = [account.to_dict() for account in store.list_accounts()]
    _print_json({"accounts": rows})
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    store = _build_store(args.state_dir)
    client = LoginClient(store=store)
    result = client.login_with_qr(
        session_key=args.session_key,
        timeout_s=args.timeout_s,
        bot_type=args.bot_type,
        force=args.force,
        event_callback=_login_event_handler,
    )
    _print_json(result.to_dict())
    return 0 if result.connected else 1


def cmd_login_start(args: argparse.Namespace) -> int:
    store = _build_store(args.state_dir)
    client = LoginClient(store=store)
    result = client.start(
        session_key=args.session_key,
        bot_type=args.bot_type,
        force=args.force,
        event_callback=_login_event_handler,
    )
    _print_json(
        {
            "session_key": result.session_key,
            "qrcode_url": result.qrcode_url,
            "message": result.message,
        }
    )
    return 0


def cmd_login_wait(args: argparse.Namespace) -> int:
    store = _build_store(args.state_dir)
    client = LoginClient(store=store)
    result = client.wait(
        session_key=args.session_key,
        timeout_s=args.timeout_s,
        event_callback=_login_event_handler,
    )
    _print_json(result.to_dict())
    return 0 if result.connected else 1


def _render_poll_message(message: dict[str, Any], args: argparse.Namespace, account: AccountClient) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "summary": summarize_message(message),
    }
    if args.raw:
        payload["raw"] = message
    if args.download_media:
        files = account.media.download_message_media(message, output_dir=args.download_media)
        payload["downloaded_media"] = [str(path) for path in files]
    return payload


def cmd_poll(args: argparse.Namespace) -> int:
    account = _load_account(args)
    while True:
        response = account.poll_once(timeout_s=args.timeout_s)
        if response.messages:
            for message in response.messages:
                _print_json(_render_poll_message(message, args, account))
        elif not args.forever:
            _print_json(response.to_dict())
        if not args.forever:
            break
    return 0


def cmd_send_text(args: argparse.Namespace) -> int:
    account = _load_account(args)
    message_id = account.send_text(
        to_user_id=args.to,
        text=args.text,
        context_token=args.context_token,
    )
    _print_json({"message_id": message_id})
    return 0


def _send_media_with_kind(args: argparse.Namespace, forced_kind: str | None = None) -> int:
    account = _load_account(args)
    message_id = account.media.send_file(
        file_path=args.path,
        to_user_id=args.to,
        caption=args.caption,
        context_token=args.context_token,
        forced_kind=forced_kind,
    )
    _print_json({"message_id": message_id})
    return 0


def cmd_download_media(args: argparse.Namespace) -> int:
    account = _load_account(args)
    with Path(args.message_file).open("r", encoding="utf-8") as handle:
        loaded = json.load(handle)
    message = loaded.get("raw", loaded) if isinstance(loaded, dict) else loaded
    media_items = list(iter_media_items(message))
    if not media_items:
        raise WeixinError("消息里没有可下载的媒体项")
    try:
        item = media_items[args.item_index]
    except IndexError as exc:
        raise WeixinError(f"item_index 越界，当前只有 {len(media_items)} 个媒体项") from exc
    path = account.media.download_media(item, output_dir=args.output_dir)
    _print_json({"path": str(path)})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="weixin-sdk")
    parser.add_argument("--state-dir", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    accounts = sub.add_parser("accounts")
    accounts.set_defaults(func=cmd_accounts)

    login = sub.add_parser("login")
    login.add_argument("--session-key")
    login.add_argument("--timeout-s", type=float, default=480.0)
    login.add_argument("--bot-type", default=DEFAULT_BOT_TYPE)
    login.add_argument("--force", action="store_true")
    login.set_defaults(func=cmd_login)

    login_sub = login.add_subparsers(dest="login_command")

    login_start = login_sub.add_parser("start")
    login_start.add_argument("--session-key")
    login_start.add_argument("--bot-type", default=DEFAULT_BOT_TYPE)
    login_start.add_argument("--force", action="store_true")
    login_start.set_defaults(func=cmd_login_start)

    login_wait = login_sub.add_parser("wait")
    login_wait.add_argument("--session-key", required=True)
    login_wait.add_argument("--timeout-s", type=float, default=480.0)
    login_wait.set_defaults(func=cmd_login_wait)

    poll = sub.add_parser("poll")
    poll.add_argument("--account-id", required=True)
    poll.add_argument("--timeout-s", type=float, default=35.0)
    poll.add_argument("--forever", action="store_true")
    poll.add_argument("--raw", action="store_true")
    poll.add_argument("--download-media")
    poll.set_defaults(func=cmd_poll)

    send_text = sub.add_parser("send-text")
    send_text.add_argument("--account-id", required=True)
    send_text.add_argument("--to", required=True)
    send_text.add_argument("--text", required=True)
    send_text.add_argument("--context-token")
    send_text.set_defaults(func=cmd_send_text)

    for name, forced_kind in (
        ("send-file", None),
        ("send-image", "image"),
        ("send-video", "video"),
    ):
        command = sub.add_parser(name)
        command.add_argument("--account-id", required=True)
        command.add_argument("--to", required=True)
        command.add_argument("--path", required=True)
        command.add_argument("--caption", default="")
        command.add_argument("--context-token")
        command.set_defaults(func=lambda args, kind=forced_kind: _send_media_with_kind(args, kind))

    download_media = sub.add_parser("download-media")
    download_media.add_argument("--account-id", required=True)
    download_media.add_argument("--message-file", required=True)
    download_media.add_argument("--item-index", type=int, default=0)
    download_media.add_argument("--output-dir", required=True)
    download_media.set_defaults(func=cmd_download_media)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func")
    if args.command == "login" and getattr(args, "login_command", None):
        return_code = func(args)
    else:
        if args.command == "login" and not getattr(args, "login_command", None):
            return_code = cmd_login(args)
        else:
            return_code = func(args)
    raise SystemExit(return_code)


if __name__ == "__main__":
    main(sys.argv[1:])
