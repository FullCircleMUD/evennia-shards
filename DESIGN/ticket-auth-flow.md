# Ticket-Based Authentication Flow

## Overview

When a player goes IC on the router, the router creates a single-use ticket and redirects the client to the target shard. The shard validates the ticket on connection, authenticates the session, puppets the character, and deletes the ticket.

## Flow

```
Router                          Client                          Shard
  |                               |                               |
  |  player types: IC bob         |                               |
  |<------------------------------|                               |
  |                               |                               |
  |  token = create_ticket(       |                               |
  |    account_id, character_id,  |                               |
  |    "shard0")                  |                               |
  |                               |                               |
  |  shard_ws = get_shard_ws(     |                               |
  |    "shard0")                  |                               |
  |                               |                               |
  |  redirect_client(shard_ws,    |                               |
  |    token)                     |                               |
  |------------------------------>|                               |
  |                               |  ws://shard:port/ws?ticket=T  |
  |                               |------------------------------>|
  |                               |                               |
  |                               |  validate_ticket(T)           |
  |                               |  -> account_id, character_id  |
  |                               |                               |
  |                               |  auto-login session           |
  |                               |  puppet character             |
  |                               |  delete ticket                |
  |                               |                               |
  |                               |  player is IC, playing        |
  |                               |<----------------------------->|
```

## Bidirectional: not just router→shard

Token auth works in **both directions**:

- **Router → Shard** (going IC): router creates ticket, redirects client to shard with token. Shard auto-logins and puppets the character.
- **Shard → Router** (going OOC): shard creates ticket, redirects client back to router with token. Router auto-logins the account (back to OOC state).

The rule: **if there's a token in the URL, use ticket auth — regardless of role** (except monolith). The token path is universal for any non-monolith instance.

The difference between roles is what *else* is available alongside token auth:

- **Router**: token auth (for returning players) + normal login (for fresh connections)
- **Shard**: token auth only (no login screen, no normal login)
- **Monolith**: normal login only (no token auth, library dormant)

## Key properties

- **Token as primary key**: Single indexed DB lookup on the hot path — no JSON scanning.
- **Single-use**: Ticket is deleted after validation. A second connection with the same token is refused.
- **IP-pinned**: The ticket records the client's IP at creation time. The receiving instance compares it against the connecting client's IP and rejects mismatches. Prevents token theft — an intercepted token is useless from a different IP. The field is nullable, so IP pinning is opt-in (e.g. omitted in test harnesses or when the IP is unavailable).
- **No session transfer**: The receiving instance creates a new session. The token is the only bridge between connections.
- **Same codebase**: The router and shard run identical code. Behaviour differences are gated on `SHARDS_ROLE`.

## Protocol override mechanism

Token extraction happens in a custom `WebSocketClient` subclass wired in via Evennia's `WEBSOCKET_PROTOCOL_CLASS` setting. Two design constraints:

1. **Monolith gating**: The override is only installed when `get_role() != "monolith"`. Monolith mode uses normal login exclusively; the library is dormant.

2. **Dynamic base class**: The library does *not* hardcode Evennia's `WebSocketClient` as the base class. Instead, `AppConfig.ready()` stashes the consumer's *current* `WEBSOCKET_PROTOCOL_CLASS` value before overwriting it. When `protocols.py` is later imported by `service.py`, it resolves the stashed path via `class_from_module` and subclasses *that*. This preserves any consumer customisations to the WebSocket protocol — the library layers on top rather than replacing.

## Not yet implemented

- Auto-login: set `uid` + `logged_in` from validated ticket to trigger `portal_connect()` auto-login. Complication: `init_session()` resets both to `None`/`False`, so auth state must be injected between `init_session()` and `sessionhandler.connect()` — both called inside `super().onOpen()`.
- Server-side puppet hook (go IC after auto-login)
- IC command override on router
- OOC command override on shard (redirect back to router)
- `get_shard_websocket()` lookup
- Client-side redirect handling (OOB message + JS plugin for WebSocket reconnect)
