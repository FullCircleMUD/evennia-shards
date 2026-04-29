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

## Key properties

- **Token as primary key**: Single indexed DB lookup on the hot path — no JSON scanning.
- **Single-use**: Ticket is deleted after validation. A second connection with the same token is refused.
- **No session transfer**: The shard creates a new session. The token is the only bridge between router and shard.
- **Same codebase**: The router and shard run identical code. The IC command override on the router and the ticket auth on the shard are gated on `SHARDS_ROLE`.

## Not yet implemented

- Portal protocol override to extract token from WebSocket URL
- Server-side auto-login and puppet hook
- IC command override on router
- OOC command override on shard (redirect back to router)
- `get_shard_websocket()` lookup
- Client-side redirect handling
