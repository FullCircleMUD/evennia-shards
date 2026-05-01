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
  |  shard_url = get_shard_url(   |                               |
  |    "shard0")                  |                               |
  |                               |                               |
  |  OOB: shard_redirect          |                               |
  |  [shard_url?ticket=T]         |                               |
  |------------------------------>|                               |
  |                               |  window.location.href =       |
  |                               |  http://shard/webclient?       |
  |                               |    ticket=T                   |
  |                               |------------------------------>|
  |                               |                               |
  |                               |  middleware injects ticket     |
  |                               |  into window.csessid           |
  |                               |                               |
  |                               |  ws connects with             |
  |                               |  &ticket=T in query string    |
  |                               |                               |
  |                               |  onOpen() auth cascade:       |
  |                               |  1. browser session? (no)     |
  |                               |  2. ticket? validate + login  |
  |                               |  3. puppet character          |
  |                               |  4. delete ticket             |
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

## Auth priority in onOpen()

The `onOpen()` override uses a three-way auth cascade:

1. **Browser session** (csessid): if the browser already has an authenticated Django session, use it. This handles page refreshes — a stale `?ticket=` in the URL is ignored, avoiding re-consumption of an already-deleted ticket.
2. **Ticket token**: if no browser session but `?ticket=` is present in the WebSocket URL, validate and consume the ticket. Sets `uid` + `logged_in` for `portal_connect()` auto-login.
3. **No session, no token**: role-dependent gating. Shards reject ("this shard requires a ticket"). Routers fall through to the normal login screen.

This ordering is load-bearing: tickets are single-use, so checking them first would break page refresh (the token is already consumed on first use, but `at_login()` saved the uid to the browser session).

## Client-side redirect mechanism

The redirect uses a full page navigation (`window.location.href`), not a WebSocket-level reconnect. This is simpler and gives better UX:

- **OOB message**: server sends `["shard_redirect", ["http://host:port/webclient?ticket=TOKEN"], {}]`
- **JS plugin** (`shard_redirect.js`): catches the OOB via a direct emitter listener (bypasses `default_out.js` catch-all) and navigates the browser to the target URL.
- **Middleware** (`ShardRedirectScriptMiddleware`): intercepts the target instance's webclient HTML response. If `?ticket=` is present in the HTTP URL, injects an inline `<script>` that appends `&ticket=TOKEN` to `window.csessid`. This places the ticket in the WebSocket URL's query string alongside Evennia's positional params (`csessid&ticket=TOKEN&cuid&browser`), where both Evennia's positional parser and the library's `parse_qs()` extraction handle it correctly.
- **Browser refresh**: works because `at_login()` saves the uid to the Django session via csessid. A refresh re-sends the stale `?ticket=` in the URL, but the browser session check (auth priority #1) catches it first.

The middleware and JS plugin are auto-injected by `AppConfig.ready()` — zero consumer configuration beyond `INSTALLED_APPS`.

## Auto-puppet and `_last_puppet`

Evennia's `AUTO_PUPPET_ON_LOGIN` (default `True`) calls `self.puppet_object(session, self.db._last_puppet)` in `at_post_login`. `_last_puppet` is a serialized ObjectDB reference (stored as model class + PK in the Attribute table). Deserializing it triggers `from_db`.

The library must not force `AUTO_PUPPET_ON_LOGIN = False` — both modes must work.

**Router**: exempt from all chokepoints (see [shard-isolation.md](shard-isolation.md)), so it can freely deserialize `_last_puppet`, load characters from any shard, and perform chargen/chardelete. On login with `AUTO_PUPPET_ON_LOGIN = True`, the router reads `_last_puppet`, determines the character's shard, creates a ticket, and redirects. When the player selects a different character (via IC command or character selection), the router overwrites `_last_puppet` with the chosen character before redirecting — so `_last_puppet` is not always strictly the "last puppeted" character; it's the character the router has chosen for the next shard session. The router never actually puppets — it delegates that to the shard.

**Shard**: receives the player via ticket auth. `at_post_login` fires and auto-puppet reads `_last_puppet` — the character is on this shard, so `from_db` passes. Standard Evennia puppeting, no interception needed. The ticket's `character_id` and `_last_puppet` agree because the router set `_last_puppet` before redirecting. Shards explicitly set `AUTO_PUPPET_ON_LOGIN = True` in their per-instance settings to ensure ticket auth always triggers puppeting.

**Accounts are AccountDB** (not ObjectDB), so no chokepoint applies — shards load accounts freely during ticket auth.

## Not yet implemented

- Router-side `at_post_login` override (read `_last_puppet` → ticket → redirect instead of local puppet)
- IC command override (on all instances, gated by role — redirects on router, normal on shard)
- OOC command override (on all instances, gated by role — normal on router, redirects on shard)
