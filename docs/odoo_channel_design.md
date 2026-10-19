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

## 2. Odoo APIs used

Odoo does not expose a dedicated "bot" API. `OdooChannel` talks to the
generic JSON-RPC entry point `/jsonrpc` and calls model methods with
`execute_kw`, i.e. the same external-API surface Odoo documents for
integrations. All calls are **stateless**: each one carries
`db` / `uid` / `api_key`, so no session cookie is involved.

| Call | Purpose |
|---|---|
| `service=common, method=authenticate(db, login, api_key, env)` | Resolve the `uid` owning the API key |
| `execute_kw(res.users, read, [uid], {partner_id})` | Resolve our own `res.partner` id (echo-loop guard) |
| `execute_kw(mail.message, search, [...], {order, limit})` | Seed the cursor to the latest message id at startup |
| `execute_kw(mail.message, search_read, [...], {fields, order})` | Poll for new messages on the channel |
| `execute_kw(discuss.channel, message_post, [id], {body, message_type})` | Send a reply |

### Why API key + `/jsonrpc`, and not the web-session routes

The obvious approach — the one the Discuss web client itself uses — is to
log in via `POST /web/session/authenticate` to obtain a session cookie, then
use cookie-authenticated routes (`/mail/message/post`,
`/websocket/peek_notifications`, `/websocket`). That approach has two
problems:

1. **API keys cannot create a web session.** Odoo rejects an API key at
   `/web/session/authenticate` with `AccessDenied` (verified against Odoo
   19: the same request with the account password succeeds, with the API key
   fails, even though `common.authenticate` accepts the very same key). API
   keys are for RPC only. Since we want API-key auth (so no plaintext account
   password is stored), the session-cookie routes are off the table.
2. **The realtime bus is session-scoped.** `/websocket` and
   `/websocket/peek_notifications` both require a web session; with API-key
   auth there is no cookie to present.

So `OdooChannel` uses a **pure RPC** design: `execute_kw` for everything,
including a poll loop over `mail.message` for the receive path. This also
sidesteps deployments where the bus long-poll endpoint is unavailable.

## 3. Identity: who is agent-box "logged in as"?

The API key belongs to a real `res.users` account. That account must be a
**member** of the target `discuss.channel` (or an operator of the target Live
Chat channel, `im_livechat.channel.user_ids`) — `execute_kw` applies the same
`ir.rule`s as the web client, so a non-member simply cannot read or post.

- **Internal/operator identity** (recommended): a plain internal `res.users`
  account (`share=False`) added as a member of the channel. Required for Live
  Chat, where operator assignment (`im_livechat.channel._get_operator()`) only
  considers non-share users.
- **Portal identity**: works for a plain Discuss channel the portal user has
  been added to, subject to the same "must be a member" rule. Nothing in
  `OdooChannel` special-cases portal vs internal.

`OdooChannel` does not implement the anonymous-guest (`mail.guest` /
`/im_livechat/cors/get_session`) flow — that simulates a *visitor*, which is
a different use case. It's flagged as a possible future variant
(`OdooVisitorChannel`) in §7.

## 4. `start()` — inbound loop

```
1. common.authenticate(db, login, api_key) → uid
   execute_kw(res.users, read, [uid], fields=[partner_id]) → self._self_partner_id
2. seed cursor: search(mail.message, model="discuss.channel", res_id=<id>,
                       order="id desc", limit=1) → self._last_message_id
3. loop (every POLL_INTERVAL seconds):
     search_read(mail.message,
                 [model="discuss.channel", res_id=<id>, id > self._last_message_id],
                 fields=[id, body, author_id, message_type], order="id asc")
     for each row:
       advance self._last_message_id
       skip if author_id == self (own messages, echo-loop guard)
       skip if message_type not in {comment, email} (system chatter)
       IncomingMessage(text=body_as_plaintext, user_id=str(author_id),
                       channel="odoo", raw=row)
```

Seeding the cursor to the latest id at startup means **existing history is
not replayed** into the agent — only messages that arrive after the channel
comes up are forwarded, matching the previous long-poll behaviour.

Failure handling mirrors the other channels: network/auth errors are logged,
the loop backs off (`RETRY_DELAY`) and re-authenticates before retrying,
rather than crashing the channel.

## 5. `send_reply()` — outbound

```
execute_kw(discuss.channel, message_post, [<channel_id>],
           {body: msg.text, message_type: "comment"})
```

Only `MessageType.text` is handled in v1 (matching `WeixinChannel`'s own
`if msg.type != MessageType.text: return` guard). File/image attachments
(`msg.data["file_path"]`) are **not yet implemented** — posting an
`ir.attachment` over RPC needs a separate binary upload; this is logged at
`warning` level rather than silently ignored.

## 6. Configuration

`Settings` fields (`config.py`), same style as `wecom_bot_id` /
`weixin_account_id`:

```python
odoo_url: str = ""          # e.g. https://odoo.example.com (no trailing slash)
odoo_db: str = ""
odoo_login: str = ""        # login owning the API key (used to resolve uid)
odoo_api_key: str = ""      # API key; RPC only, cannot create a web session
odoo_channel_id: int = 0    # discuss.channel id to bridge
```

Wired into `main.py._create_channel` (`spec.type == "odoo"`) and a `--odoo`
CLI flag, exactly like the existing channels.

## 7. Known limitations / explicitly out of scope for v1

- **Poll, not push.** API-key auth cannot open the bus WebSocket, so the
  receive path polls `mail.message` every `POLL_INTERVAL` (2s) seconds. That
  is adequate for a chat assistant but not instant, and it costs one RPC per
  interval even when idle. If session-cookie auth were acceptable, switching
  to `/websocket` would be a contained change inside `start()`.
- **Single channel per process.** `odoo_channel_id` is one id; bridging
  multiple channels means multiple agent-box instances (or a future
  enhancement to accept a list). Matches the "single user, single router"
  design decision in `CLAUDE.md`.
- **No attachment support yet** (see §5).
- **No guest/visitor simulation** (see §3) — answers *as a member*.
- **Echo-loop guard is author-id based**: "is this my own message" compares
  the message's `author_id` (a **partner** id) against the logged-in user's
  `partner_id`, resolved once at startup via `res.users.read`. The user id
  (`uid`) and partner id are not assumed interchangeable.
- **Credentials**: `odoo_api_key` is stored in `.env` like the other
  channels' secrets and never logged.

## 8. Testing

`tests/test_channels.py` covers `OdooChannel` with a fake `_jsonrpc` /
`_execute_kw`: `_authenticate()` resolves uid + partner id,
`_handle_message()` emits the right `IncomingMessage` (and skips own /
system / empty messages), `send_reply()` issues the expected
`discuss.channel.message_post`, and `start()` is a no-op when unconfigured.
