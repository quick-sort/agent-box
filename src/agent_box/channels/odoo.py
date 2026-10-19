"""Odoo Discuss / Live Chat channel adapter.

Bridges a single ``discuss.channel`` (a plain Discuss chat/channel, or a
Live Chat session) to agent-box: messages posted by humans in that channel
become ``IncomingMessage``s, and the agent's replies are posted back as
ordinary Discuss messages authored by the configured Odoo account.

This talks to Odoo over plain JSON-RPC (``/jsonrpc``) authenticated with an
API key — there is no dedicated "bot" API. See
``docs/odoo_channel_design.md`` for the full design write-up (which routes
are used and why, identity/security model, and known limitations).

Protocol overview:
  1. POST {odoo_url}/jsonrpc  service=common  method=authenticate
     -> ``uid`` of the account owning the API key. API keys cannot create a
     web session (``/web/session/authenticate`` rejects them), so every call
     below is a self-contained RPC that carries db/uid/key.
  2. POST /jsonrpc  service=object  method=execute_kw
     -> ``res.users.read`` to resolve our own ``res.partner`` id (used to
        skip echoes of our own outgoing messages).
  3. Loop: ``mail.message.search_read`` for messages on
     ``discuss.channel:<id>`` newer than the last seen id -> dispatch.
  4. ``discuss.channel.message_post`` -> send a reply.

user_id format:
  the Discuss author's ``res.partner`` id, as a string (e.g. "7")
"""

from __future__ import annotations

import logging
import re

import anyio
import httpx

from ..config import settings
from ..models import IncomingMessage, MessageType, OutgoingMessage
from .base import BaseChannel

log = logging.getLogger(__name__)

# Seconds to wait between polls for new messages. There is no push channel
# available to API-key auth (the bus WebSocket needs a web session), so the
# receive path is a poll loop; keep it short enough to feel responsive and
# long enough not to hammer the server.
POLL_INTERVAL = 2.0
# Backoff between retries after a network/auth error.
RETRY_DELAY = 5.0

# Message types forwarded to the agent. ``comment`` is a normal user message;
# ``email`` is a reply that arrived by email. Everything else (``notification``,
# ``user_notification``, ``auto_comment``, ...) is system chatter and skipped.
_FORWARDED_MESSAGE_TYPES = frozenset({"comment", "email"})

_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_text(html: str | bool | None) -> str:
    """Best-effort HTML -> plain text, without pulling in a full HTML parser.

    Odoo message bodies are sanitized HTML; this is intentionally simple
    (strip tags, unescape a handful of entities) rather than a full
    html2text port, since the agent only needs the gist of the message.
    """
    if not html:
        return ""
    text = _TAG_RE.sub(" ", html)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')):
        text = text.replace(entity, char)
    return " ".join(text.split()).strip()


class OdooChannel(BaseChannel):
    """JSON-RPC adapter bridging one Discuss/Live Chat channel."""

    def __init__(self, send_stream: anyio.abc.ObjectSendStream[IncomingMessage]) -> None:
        super().__init__(send_stream)
        self._base_url = settings.odoo_url.rstrip("/")
        self._db = settings.odoo_db
        self._login = settings.odoo_login
        self._api_key = settings.odoo_api_key
        self._channel_id = settings.odoo_channel_id
        self._client: httpx.AsyncClient | None = None
        # uid of the account owning the API key, resolved at authenticate time.
        self._uid: int | None = None
        # Partner id of our own account, used to filter out echoes of our own
        # outgoing messages.
        self._self_partner_id: int | None = None
        # Highest mail.message id already seen; only newer ids are forwarded.
        self._last_message_id = 0

    # -- JSON-RPC helpers ----------------------------------------------------

    async def _jsonrpc(self, service: str, method: str, args: list) -> object:
        assert self._client is not None
        resp = await self._client.post(
            "/jsonrpc",
            json={
                "jsonrpc": "2.0",
                "method": "call",
                "params": {"service": service, "method": method, "args": args},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            err = data["error"]
            detail = (err.get("data") or {}).get("message") or err.get("message") or err
            raise RuntimeError(f"Odoo RPC error {service}.{method}: {detail}")
        return data.get("result")

    async def _execute_kw(self, model: str, method: str, args: list, kwargs: dict | None = None) -> object:
        return await self._jsonrpc(
            "object",
            "execute_kw",
            [self._db, self._uid, self._api_key, model, method, args, kwargs or {}],
        )

    async def _authenticate(self) -> None:
        uid = await self._jsonrpc(
            "common",
            "authenticate",
            [self._db, self._login, self._api_key, {"_browser": "agent-box", "user_agent": "agent-box"}],
        )
        if not uid:
            raise RuntimeError(
                f"Odoo authentication failed for login={self._login!r} db={self._db!r} "
                "(check odoo_url/odoo_db/odoo_login/odoo_api_key)"
            )
        self._uid = uid
        rows = await self._execute_kw("res.users", "read", [[uid]], {"fields": ["partner_id"]})
        partner = rows[0].get("partner_id") if rows else None
        # partner_id comes back as [id, display_name] or a bare id.
        if isinstance(partner, (list, tuple)):
            partner = partner[0] if partner else None
        self._self_partner_id = partner
        log.info(
            "odoo channel authenticated as uid=%s partner_id=%s on channel_id=%s",
            uid, self._self_partner_id, self._channel_id,
        )

    # -- BaseChannel interface ------------------------------------------------

    async def start(self) -> None:
        if not (self._base_url and self._db and self._login and self._api_key and self._channel_id):
            log.warning(
                "Odoo channel not configured (need ODOO_URL, ODOO_DB, ODOO_LOGIN, "
                "ODOO_API_KEY, ODOO_CHANNEL_ID); channel will not start."
            )
            return

        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=30.0)
        try:
            while True:
                try:
                    await self._authenticate()
                    break
                except Exception:
                    log.exception("odoo channel: authentication failed, retrying in %.0fs", RETRY_DELAY)
                    await anyio.sleep(RETRY_DELAY)

            # Start from "now": seed the cursor so existing history is not
            # replayed into the agent on startup.
            self._last_message_id = await self._latest_message_id()

            while True:
                try:
                    await self._poll_once()
                except Exception:
                    log.exception("odoo channel: poll failed, retrying")
                    await anyio.sleep(RETRY_DELAY)
                    # Credentials may have expired; re-authenticate before the
                    # next poll.
                    try:
                        await self._authenticate()
                    except Exception:
                        log.exception("odoo channel: re-authentication failed")
                    continue
                await anyio.sleep(POLL_INTERVAL)
        finally:
            await self._client.aclose()
            await self.send_stream.aclose()

    def _message_domain(self) -> list:
        return [["model", "=", "discuss.channel"], ["res_id", "=", self._channel_id]]

    async def _latest_message_id(self) -> int:
        ids = await self._execute_kw(
            "mail.message", "search", [self._message_domain()], {"order": "id desc", "limit": 1}
        )
        return ids[0] if ids else 0

    async def _poll_once(self) -> None:
        domain = self._message_domain() + [["id", ">", self._last_message_id]]
        rows = await self._execute_kw(
            "mail.message",
            "search_read",
            [domain],
            {"fields": ["id", "body", "author_id", "message_type"], "order": "id asc"},
        )
        for row in rows or []:
            msg_id = row.get("id")
            if isinstance(msg_id, int):
                self._last_message_id = max(self._last_message_id, msg_id)
            await self._handle_message(row)

    async def _handle_message(self, msg: dict) -> None:
        if msg.get("message_type") not in _FORWARDED_MESSAGE_TYPES:
            return
        author_id = msg.get("author_id")
        # author_id comes back as either an int or [id, display_name]
        if isinstance(author_id, (list, tuple)):
            author_id = author_id[0] if author_id else None
        if author_id is not None and author_id == self._self_partner_id:
            return  # skip our own outgoing messages (echo-loop guard)
        text = _html_to_text(msg.get("body"))
        if not text:
            return
        await self.send_stream.send(
            IncomingMessage(
                text=text,
                user_id=str(author_id) if author_id is not None else "unknown",
                channel="odoo",
                raw=msg,
            )
        )

    async def send_reply(self, msg: OutgoingMessage) -> None:
        if msg.type != MessageType.text:
            return
        if self._client is None or self._uid is None:
            log.warning("Odoo channel not initialized, cannot send reply.")
            return
        if (msg.data or {}).get("file_path"):
            log.warning("Odoo channel does not yet support sending file attachments (TODO).")
        try:
            await self._execute_kw(
                "discuss.channel",
                "message_post",
                [[self._channel_id]],
                {"body": msg.text, "message_type": "comment"},
            )
        except Exception:
            log.exception("odoo channel: failed to post reply")
