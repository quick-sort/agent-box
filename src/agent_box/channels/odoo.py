"""Odoo Discuss / Live Chat channel adapter.

Bridges a single ``discuss.channel`` (a plain Discuss chat/channel, or a
Live Chat session) to agent-box: messages posted by humans in that channel
become ``IncomingMessage``s, and the agent's replies are posted back as
ordinary Discuss messages authored by the configured Odoo account.

This talks to Odoo over plain JSON-RPC, using the exact same public routes
the Discuss web client itself uses — there is no dedicated "bot" API. See
``docs/odoo_channel_design.md`` for the full design write-up (which routes
are used and why, identity/security model, and known limitations).

Protocol overview:
  1. POST {odoo_url}/web/session/authenticate  -> session cookie + session_info
     (session_info includes ``uid`` and ``partner_id`` of the logged-in
     account, used to recognize and skip our own outgoing messages)
  2. Loop: POST /websocket/peek_notifications   -> long-poll for new bus
     notifications on channel ``discuss.channel_<id>``
  3. POST /mail/message/post                    -> send a reply

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

# Long-poll timeout: Odoo's own bus._poll() blocks for up to ~50s server side
# when there is nothing new; give it comfortable headroom.
POLL_TIMEOUT = 65.0
# Backoff between retries after a network/auth error.
RETRY_DELAY = 5.0

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
    """Long-poll based adapter bridging one Discuss/Live Chat channel."""

    def __init__(self, send_stream: anyio.abc.ObjectSendStream[IncomingMessage]) -> None:
        super().__init__(send_stream)
        self._base_url = settings.odoo_url.rstrip("/")
        self._db = settings.odoo_db
        self._login = settings.odoo_login
        self._password = settings.odoo_password
        self._channel_id = settings.odoo_channel_id
        self._client: httpx.AsyncClient | None = None
        self._last_bus_id = 0
        # Partner id of our own logged-in account, resolved at login time,
        # used to filter out echoes of our own outgoing messages.
        self._self_partner_id: int | None = None

    # -- JSON-RPC helper -----------------------------------------------------

    async def _call(self, route: str, params: dict) -> dict:
        assert self._client is not None
        resp = await self._client.post(route, json={"id": 0, "jsonrpc": "2.0", "method": "call", "params": params})
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"Odoo RPC error on {route}: {data['error']}")
        return data.get("result") or {}

    async def _login_session(self) -> None:
        result = await self._call(
            "/web/session/authenticate",
            {"db": self._db, "login": self._login, "password": self._password},
        )
        if not result or not result.get("uid"):
            raise RuntimeError(
                f"Odoo authentication failed for login={self._login!r} db={self._db!r} "
                "(check odoo_url/odoo_db/odoo_login/odoo_password)"
            )
        self._self_partner_id = result.get("partner_id")
        log.info(
            "odoo channel authenticated as uid=%s partner_id=%s on channel_id=%s",
            result.get("uid"), self._self_partner_id, self._channel_id,
        )

    # -- BaseChannel interface ------------------------------------------------

    async def start(self) -> None:
        if not (self._base_url and self._db and self._login and self._channel_id):
            log.warning(
                "Odoo channel not configured (need ODOO_URL, ODOO_DB, ODOO_LOGIN, "
                "ODOO_PASSWORD, ODOO_CHANNEL_ID); channel will not start."
            )
            return

        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=POLL_TIMEOUT)
        try:
            while True:
                try:
                    await self._login_session()
                    break
                except Exception:
                    log.exception("odoo channel: login failed, retrying in %.0fs", RETRY_DELAY)
                    await anyio.sleep(RETRY_DELAY)

            channel_key = f"discuss.channel_{self._channel_id}"
            while True:
                try:
                    result = await self._call(
                        "/websocket/peek_notifications",
                        {
                            "channels": [channel_key],
                            "last": self._last_bus_id,
                            "is_first_poll": self._last_bus_id == 0,
                        },
                    )
                except Exception:
                    log.exception("odoo channel: peek_notifications failed, retrying")
                    await anyio.sleep(RETRY_DELAY)
                    # Session may have expired; re-authenticate before the next poll.
                    try:
                        await self._login_session()
                    except Exception:
                        log.exception("odoo channel: re-authentication failed")
                    continue

                for notif in result.get("notifications", []):
                    notif_id = notif.get("id")
                    if isinstance(notif_id, int):
                        self._last_bus_id = max(self._last_bus_id, notif_id)
                    await self._handle_notification(notif)
        finally:
            await self._client.aclose()
            await self.send_stream.aclose()

    async def _handle_notification(self, notif: dict) -> None:
        if notif.get("type") != "discuss.channel/new_message":
            return
        payload = notif.get("payload") or {}
        messages = ((payload.get("data") or {}).get("mail.message")) or []
        for msg in messages:
            author_id = msg.get("author_id")
            # author_id comes back as either an int or [id, display_name]
            if isinstance(author_id, (list, tuple)):
                author_id = author_id[0] if author_id else None
            if author_id is not None and author_id == self._self_partner_id:
                continue  # skip our own outgoing messages (echo-loop guard)
            text = _html_to_text(msg.get("body"))
            if not text:
                continue
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
        if self._client is None:
            log.warning("Odoo channel not initialized, cannot send reply.")
            return
        if (msg.data or {}).get("file_path"):
            log.warning("Odoo channel does not yet support sending file attachments (TODO).")
        try:
            await self._call(
                "/mail/message/post",
                {
                    "thread_model": "discuss.channel",
                    "thread_id": self._channel_id,
                    "post_data": {"body": msg.text, "message_type": "comment"},
                },
            )
        except Exception:
            log.exception("odoo channel: failed to post reply")
