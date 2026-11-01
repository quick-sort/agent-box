# AgentHub Relay Channel — Design Document

Status: implemented
Code: `src/agent_box/channels/relay.py`

## 1. Goal

AgentHub's target architecture moves every long-lived transport (WeCom aibot
sockets, Discuss bridges, routing, hot queues) into a standalone **relay**.
Odoo only stores config, the publish outbox, the record feed and
`mail.message` archives. agent-box therefore stops owning IM connections and
becomes an **agent client** of the relay: one outbound WebSocket, answer
deliveries, stream messages back.

`--relay` replaces the legacy `--wecom` channel. The two flags are mutually
exclusive because the relay and the legacy channel would otherwise fight over
the same `bot_id` connection.

## 2. Connection and frames

Endpoint: `RELAY_URL`, typically `ws://relay:8090/v1/agent/ws`.

| Direction | Frame | Notes |
|---|---|---|
| C→S | `{"type":"auth","agent_id":12,"token":"…"}` | first frame after connect |
| S→C | `{"type":"hello","heartbeat_sec":20,"limits":{…}}` | auth accepted; `error` otherwise |
| S→C | `{"type":"deliver","delivery_id","kind","turn","message"/"command"}` | one inbound turn |
| C→S | `{"type":"stream","delivery_id","message_id","stream_id","seq","final":true,"role","content"}` | zero or more replies |
| C→S | `{"type":"turn","delivery_id","state":"completed"|"failed","error"}` | turn finished |
| C→S | `{"type":"ack","delivery_id","state":"done"|"failed","error"}` | delivery finished |
| C→S | `{"type":"ping"}` / S→C `{"type":"pong"}` | heartbeat |

Every reply gets its own `message_id` / `stream_id`
(`"{turn_id}:{seq}"`) so multiple messages in one turn are distinct final
messages in the relay rather than collisions.

## 3. thread_key ↔ conversation ↔ session

- A delivery's `turn.thread_key` becomes `IncomingMessage.conversation_id`.
- `App._get_or_create_agent()` keys agents by `(project, conversation_id)`,
  so each relay thread gets its own agent instance/context; the router still
  chooses the project (multi-project mode) from the message text.
- Replies carry the same `conversation_id` back, so `send_reply()` can find
  the matching delivery (`thread_key → {delivery_id, turn_id, seq}`).

## 4. End-of-turn signal

The existing pipeline has no explicit "no more output" event. `App` emits an
`OutgoingMessage` with `data={"_turn_end": True, "error": …}` after every
inbound turn; `BaseChannel.send_loop` intercepts it and calls the optional
`turn_finished()` hook. All channels except relay ignore it. The relay
channel uses it to send `turn` + `ack` and drop the active delivery.

Without this signal the relay would keep the thread's active turn open and
never dispatch the next queued message.

## 5. Reliability

- Delivery is **at-least-once**: the relay requeues an unacked delivery when
  its lease expires. A crash or reconnect can therefore re-run a turn.
- The client acks only after the agent loop finished (or failed).
- On disconnect the channel clears its active deliveries and reconnects with
  exponential backoff (1s → 30s). The relay redelivers outstanding turns.
- A failed turn acks `state="failed"`, which counts against the relay's
  `max_attempts`; persistent failures end in the relay DLQ.

## 6. Configuration

| Variable | Meaning |
|---|---|
| `RELAY_URL` | full agent WebSocket URL |
| `RELAY_AGENT_ID` | `agenthub.agent` id in Odoo |
| `RELAY_AGENT_TOKEN` | plaintext token; the relay compares its SHA-256 against `token_hash` |
| `RELAY_HEARTBEAT_SECONDS` | ping interval (default 20) |

Run: `uv run agent-box --relay`.

## 7. Limitations

- Text only: no attachments, files or media in either direction.
- Commands are rendered as `payload.prompt` when present, otherwise as
  `[relay <name>] <json payload>`; there is no per-command protocol.
- `thread_key → conversation_id` mapping is in-memory. After a restart the
  same thread still maps to the same conversation id (it comes from the
  relay), but in-flight turns are replayed only through relay redelivery.
- Legacy `--wecom` remains available for deployments without a relay.
