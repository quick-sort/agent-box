# Odoo Channel — Design Document

Status: implemented
Code: `src/agent_box/channels/odoo.py`

## 1. Goal

Let agent-box treat an Odoo **Discuss channel** or **Live Chat session** as
just another IM channel: messages posted there by a human show up as
`IncomingMessage`s routed to the agent/router pipeline, and the agent's
replies are posted back with `OutgoingMessage` → `OdooChannel.send_reply()`.
This is the *external integration* counterpart to the `llm_discuss` /
`llm_discuss_livechat` Odoo addons (see
`odoo_projects/odoo_llm/llm_discuss/DESIGN.md`), which instead run an LLM
bot *inside* Odoo. `OdooChannel` runs entirely outside Odoo, as an ordinary
agent-box channel adapter, authenticating like any Discuss user would.

## 2. Odoo APIs used (all confirmed by reading Odoo core source)

Odoo does not expose a dedicated "bot" API; `OdooChannel` uses the exact
same JSON-RPC endpoints the Discuss web client itself calls.

| Endpoint | Module | auth | Purpose |
|---|---|---|---|
| `POST /web/session/authenticate` | `web` (`controllers/session.py`) | `none` | Log in with `db`/`login`/`password`, get a session cookie |
| `POST /mail/message/post` | `mail` (`controllers/thread.py`) | `public`\* | Post a message: `thread_model="discuss.channel"`, `thread_id`, `post_data={"body": ..., "message_type": "comment"}` |
| `POST /websocket/peek_notifications` | `bus` (`controllers/websocket.py`) | `public`\* | Long-poll fallback for the realtime bus: `channels=["discuss.channel_<id>"]`, `last=<bus_id>` |

\* `auth="public"` means the route itself doesn't *require* a login, but
without a valid session cookie you are treated as the public user, which
has no membership in any `discuss.channel` — so in practice you still need
to authenticate first via `/web/session/authenticate` to have any access to
a real (non-guest) channel.

`peek_notifications`'s `channels` entries use the string format
`"<model>_<id>"`. For `discuss.channel` specifically, Odoo's
`addons/mail/models/discuss/ir_websocket.py::_build_bus_channel_list`
parses `"discuss.channel_<id>"` with a regex, re-resolves it to the actual
record, and applies the model's normal `ir.rule` (`is_member=True`) before
allowing the subscription — i.e. the logged-in user must actually be a
member of that channel, exactly like the web client.

A real bidirectional `/websocket` connection (the same route Discuss's own
JS `bus_service` opens) is more efficient than polling but requires
implementing the websocket subscribe handshake and reconnect handling.
`peek_notifications` was chosen for v1 because it is a plain JSON-RPC call
that fits the existing polling style of `WeixinChannel` (see §4), and Odoo
itself treats it as an equivalent, supported fallback to the websocket
(used by Odoo's own frontend when a real websocket can't be established).
Switching to `/websocket` later is a contained change inside `start()`;
`send_reply()` and the message format are unaffected.

## 3. Identity: who is agent-box "logged in as"?

This mirrors the two Odoo-side scenarios from the research phase:

- **Internal/operator identity** (recommended default): `odoo_login` /
  `odoo_password` point to a plain internal `res.users` account
  (`share=False`) that has been added as a member of the target
  `discuss.channel`, or as an operator (`im_livechat.channel.user_ids`) of
  the target Live Chat channel. This is required for Live Chat: operator
  assignment (`im_livechat.channel._get_operator()`) only ever considers
  non-share `res.users`, so a portal account cannot receive/answer visitor
  sessions.
- **Portal identity**: works for a plain Discuss channel the portal user has
  been added to (e.g. a support channel shared with a customer), subject to
  the same "must be a member" rule — nothing in `OdooChannel` special-cases
  portal vs internal, the distinction lives entirely in how the Odoo side is
  configured (who the `odoo_login` account is and what it's a member of).

`OdooChannel` does not implement the anonymous-guest (`mail.guest` /
`/im_livechat/cors/get_session`) flow — that path is for simulating a
*visitor*, which is a different use case (testing Odoo's Live Chat from the
outside) than "agent-box answers messages", which needs a real member
identity to read/post into an existing channel. It's flagged as a possible
future channel variant (`OdooVisitorChannel`) in §7.

## 4. `start()` — inbound loop

Follows `WeixinChannel`'s long-poll shape (`CLAUDE.md`'s own description:
"Weixin adapter wraps a sync SDK via anyio.to_thread"; here there's no SDK,
just `httpx`, so the loop is plain `async`/`await`):

```
1. POST /web/session/authenticate            → session cookie (httpx.AsyncClient cookie jar)
2. loop:
     POST /websocket/peek_notifications
       channels=["discuss.channel_<id>"], last=<self._last_bus_id>
     for each notification of type "discuss.channel/new_message":
       skip if author == self (own bot's messages, avoid echo loop)
       IncomingMessage(text=body_as_plaintext, user_id=str(author_id), channel="odoo", raw=...)
     update self._last_bus_id
```

Failure handling mirrors `WeixinChannel._download_media_text`/poll pattern:
network errors are logged and the loop backs off and retries rather than
crashing the whole channel (agent-box's other channels keep running).

## 5. `send_reply()` — outbound

```
POST /mail/message/post
  thread_model="discuss.channel", thread_id=<channel_id>
  post_data={"body": msg.text, "message_type": "comment"}
```

Only `MessageType.text` is handled in v1 (matching `WeixinChannel`'s own
`if msg.type != MessageType.text: return` guard for its base text path).
File/image attachments (`msg.data["file_path"]`) are **not yet implemented**
— posting an `ir.attachment` via JSON-RPC needs a separate multipart upload
route; this is called out as a TODO rather than silently ignored (logged at
`warning` level), matching agent-box's existing pattern of degrading
per-feature rather than failing the whole channel.

## 6. Configuration

New `Settings` fields (`config.py`), same style as `wecom_bot_id` /
`weixin_account_id`:

```python
odoo_url: str = ""          # e.g. https://odoo.example.com (no trailing slash)
odoo_db: str = ""
odoo_login: str = ""
odoo_password: str = ""
odoo_channel_id: int = 0    # discuss.channel id to bridge
```

Wired into `main.py._create_channel("odoo", ...)` and a `--odoo` CLI flag,
exactly like the three existing channels.

## 7. Known limitations / explicitly out of scope for v1

- **Single channel per process.** `odoo_channel_id` is one id; running
  against multiple Odoo channels means running multiple agent-box instances
  (or a future enhancement to accept a list and multiplex `user_id` by
  channel id). This matches the "single user, single router" design
  decision already documented in `CLAUDE.md`.
- **No attachment support yet** (see §5).
- **Long-poll, not websocket** (see §2) — adequate latency for a chat
  assistant (typically sub-second to a few seconds), but a real `/websocket`
  connection would be more efficient at scale / many channels.
- **No guest/visitor simulation** (see §3) — `OdooChannel` answers *as a
  member*, it does not create Live Chat sessions as a visitor.
- **Echo-loop guard is author-id based**: if the Odoo-side bot user id isn't
  known in advance, `OdooChannel` infers "is this my own message" by
  comparing the notification's author id against the id returned by
  `/web/session/authenticate` (the logged-in `uid`) — this only works
  because `author_id` in the Discuss message payload is a **partner** id
  while the session `uid` is a **user** id; the implementation resolves the
  logged-in user's `partner_id` once at startup (via
  `/mail/thread/messages` on first poll, or a dedicated small RPC) rather
  than assuming they're interchangeable. See the `_self_partner_id` handling
  in `odoo.py`.
- **Credentials**: `odoo_password` is a plaintext credential like the other
  channels' secrets (`wecom_secret`, etc.) — same `.env`-only storage
  convention, never logged.

## 8. Testing

`tests/test_channels.py` adds `OdooChannel` tests following the existing
`WeixinChannel` pattern: mock `httpx.AsyncClient` (via `respx` if available,
otherwise a hand-rolled fake transport) to verify `start()` emits the right
`IncomingMessage` from a `peek_notifications` payload, and `send_reply()`
POSTs the expected `/mail/message/post` body.
