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
  |  ws_url = get_shard_url(      |                               |
  |    "shard0")                  |                               |
  |  (a ws:// or wss:// URL)      |                               |
  |                               |                               |
  |  OOB: shard_redirect          |                               |
  |  [ws_url?ticket=T]            |                               |
  |------------------------------>|                               |
  |                               |  shard_redirect.js:           |
  |                               |  1. close existing WebSocket   |
  |                               |     (page stays loaded;       |
  |                               |     scrollback / UI persist)  |
  |                               |  2. open new WebSocket to      |
  |                               |     ws_url?ticket=T           |
  |                               |------------------------------>|
  |                               |                               |
  |                               |  onOpen() auth cascade:       |
  |                               |  1. browser session? (no)     |
  |                               |  2. ticket? validate + login  |
  |                               |  3. puppet character          |
  |                               |  4. delete ticket             |
  |                               |                               |
  |                               |  player is IC, playing        |
  |                               |  (same browser tab, same      |
  |                               |   page — only the WebSocket   |
  |                               |   has changed)                |
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

The redirect operates at the **WebSocket connection layer**, not the page-navigation layer. The page stays loaded; only the underlying WebSocket changes target. UI state, scrollback, command history, plugin state — all persist across cross-shard transitions.

- **OOB message**: server sends `["shard_redirect", ["ws://host:port/?ticket=TOKEN"], {}]`. The URL is a WebSocket URL (`ws://` or `wss://`) with the single-use ticket in the query string.
- **JS plugin** (`shard_redirect.js`): catches the OOB via a direct emitter listener and:
  1. Sets a `deliberate_transfer` flag.
  2. Calls `Evennia.connection.close()` on the current WebSocket-backed connection.
  3. Constructs a new `WebSocket(target_url)` and replaces `Evennia.connection` with a wrapper exposing the standard `{connect, msg, close, isOpen}` contract — mirroring Evennia's own `WebsocketConnection` event-emit shape so other plugins continue working unchanged.
  4. Wraps `Evennia.emitter.emit` once at module load to swallow the `connection_close` event triggered by step 2 (otherwise webclient_gui's `onConnectionClose` would print a misleading "connection was closed or lost" message). Real disconnects continue to render normally — the flag is consumed on the first `connection_close` after each deliberate transfer, with a 5s safety timeout.
- **Middleware** (`ShardRedirectScriptMiddleware`): injects the `shard_redirect.js` script tag into webclient HTML responses. This is what gets the JS plugin loaded into the page in the first place. The middleware also has a legacy ticket-injection path (`?ticket=` in the page URL → inline `<script>` appending `&ticket=TOKEN` to `window.csessid`); that path is now only relevant if someone loads a webclient page directly with `?ticket=` in the URL — an edge case (manual paste, bookmark) that the WS-level redirect doesn't otherwise traverse.
- **Browser refresh**: refreshes the loaded webclient page (same as a vanilla Evennia refresh). The Django/csessid session continues; if the player was IC on a shard at refresh time, they reconnect to that shard via the saved csessid (no ticket needed for a re-attach).

The middleware and JS plugin are auto-injected by `AppConfig.ready()` — zero consumer configuration beyond `INSTALLED_APPS`.

### Why connection-level instead of page-navigation

This design was chosen over an earlier full-page-navigation approach (`window.location.href`) for several reasons that compound:

- **UX continuity.** The page never reloads; scrollback, plugin state, custom UI all persist across cross-shard transitions. Players experience the transition as a brief connection swap, not as "the world disappeared and reappeared."
- **Protocol-agnostic shape.** "Close the existing connection, open a new one to a target with a single-use auth token" is exactly the same pattern that telnet/SSH/MUD-client redirects would use (via GMCP, server-side reconnect notice, etc.). The library's WebSocket implementation is one expression of a universal shape, not a web-specific mechanism.
- **No public-shard-URL UX problem.** A page-navigation redirect has to navigate to a URL on the destination shard, which means the destination shard must serve an HTTP webclient — and any direct hit to that URL becomes an orphan-landing problem. With connection-level redirect, the player's browser only ever loads the router's webclient; shards are reached only via WebSocket. This positions a future architecture where shards run WebSocket-only (no Django HTTP at all) — reducing attack surface, eliminating orphan URLs, simplifying deployment.

## Auto-puppet and `_last_puppet`

Evennia's `AUTO_PUPPET_ON_LOGIN` (default `True`) calls `self.puppet_object(session, self.db._last_puppet)` in `at_post_login`. `_last_puppet` is a serialized ObjectDB reference (stored as model class + PK in the Attribute table). Deserializing it triggers `from_db`.

The library must not force `AUTO_PUPPET_ON_LOGIN = False` — both modes must work.

**Router**: exempt from all chokepoints (see [shard-isolation.md](shard-isolation.md)), so it can freely deserialize `_last_puppet`, load characters from any shard, and perform chargen/chardelete. On login with `AUTO_PUPPET_ON_LOGIN = True`, the router reads `_last_puppet`, determines the character's shard, creates a ticket, and redirects. When the player selects a different character (via IC command or character selection), the router overwrites `_last_puppet` with the chosen character before redirecting — so `_last_puppet` is not always strictly the "last puppeted" character; it's the character the router has chosen for the next shard session. The router never actually puppets — it delegates that to the shard.

**Shard**: receives the player via ticket auth. `at_post_login` fires and auto-puppet reads `_last_puppet` — the character is on this shard, so `from_db` passes. The ticket's `character_id` and `_last_puppet` agree because the router set `_last_puppet` before redirecting. Shards explicitly set `AUTO_PUPPET_ON_LOGIN = True` in their per-instance settings to ensure ticket auth always triggers puppeting.

A thin wrapper (`make_shard_at_post_login` in `hooks.py`) is installed around Evennia's original `at_post_login` on shards. The wrapper flushes the `_last_puppet` character from the idmapper cache and refreshes its fields from the DB before delegating to the original. This is needed because `cross_shard_character_move` on the *source* shard updates the character's `shard_id` in the DB and writes to the Account's Attribute handler cache; the *destination* shard's Attribute handler cache may still hold the stale Python object with the old `shard_id`. Without the flush+refresh, `puppet_object` would save the stale object and the `pre_save` chokepoint would refuse it. See [shard-isolation.md](shard-isolation.md) for the broader idmapper/Attribute-cache staleness pattern.

**Accounts are AccountDB** (not ObjectDB), so no chokepoint applies — shards load accounts freely during ticket auth.

## IC command override

`ShardAwareCmdIC` (in `evennia_shards/commands.py`) replaces Evennia's `CmdIC` via monkey-patch in `AppConfig.ready()`. Injected when `get_role() != "monolith"`.

- **Router** (`AUTO_PUPPET_ON_LOGIN = False` path): resolves the character using the same logic as Evennia's `CmdIC` (playable characters search, Builder+ global search, `_last_puppet` fallback). Instead of calling `puppet_object()`, it sets `account.db._last_puppet` to the chosen character, creates a ticket via `create_ticket()`, and sends a `shard_redirect` OOB to the client. The shard then ticket-auths, auto-puppets via `_last_puppet`, and the player is IC.
- **Shard**: tells the player "Leave this character before trying to enter another one." IC always goes through the router — no same-shard shortcut.
- **Monolith**: original `CmdIC` stays; the override is never injected.

The injection uses the same pattern as the WebSocket protocol and middleware overrides: patch the module attribute (`evennia.commands.default.account.CmdIC`) so the `AccountCmdSet` picks up the replacement on cmdset rebuild.

## OOC command override

`ShardAwareCmdOOC` (in `evennia_shards/commands.py`) replaces Evennia's `CmdOOC` via monkey-patch in `AppConfig.ready()`. Injected **only** when `get_role() == "shard"` — the router and monolith keep vanilla `CmdOOC`.

- **Shard**: creates a ticket targeting the router (`to_shard = get_router_shard_id()`, always `"router"`) and sends a `shard_redirect` OOB to redirect the client back to the router's webclient. Always redirects — even if no puppet (error state), because a player should never be OOC on a shard.
- **Character ID in ticket**: `old_char.id` (current puppet) → `account.db._last_puppet.id` (fallback) → `0` (sentinel for truly broken state, with `logger.log_warn`).
- **`_last_puppet` is left alone.** The library does not mutate Evennia's `_last_puppet` attribute — vanilla semantics preserved. The OOC redirect loop on routers running `AUTO_PUPPET_ON_LOGIN = True` is broken instead by the per-session `protocol_flags["SHARDS_TICKET_AUTHED"]` value set in `ShardWebSocketClient.onOpen()` (see "Auto-puppet on login" below).
- **No explicit unpuppet**: the redirect closes the existing WebSocket connection (the JS plugin's first step before opening the new one). Evennia's disconnect handler (`sessionhandler.disconnect()` → `account.unpuppet_object()`) automatically releases the character on the shard when the connection drops.
- **Router**: vanilla `CmdOOC` stays — normal unpuppet, player stays on the router OOC.
- **Monolith**: vanilla `CmdOOC` stays; the override is never injected.

The asymmetry with IC (which patches both router and shard) is intentional: IC on a shard without the override would attempt a local puppet, which would either hit chokepoints or cause confusion. OOC on a router is harmless — the vanilla command does exactly what's needed (unpuppet, show OOC menu).

## Auto-puppet on login (`AUTO_PUPPET_ON_LOGIN = True`)

Evennia's default `AUTO_PUPPET_ON_LOGIN = True` calls `account.puppet_object(session, self.db._last_puppet)` from inside `at_post_login`. On a router that's broken — see [library-integration-risks.md](library-integration-risks.md#defaultaccountat_post_login-override) for why.

The library replaces `DefaultAccount.at_post_login` on routers with `shard_aware_at_post_login` (in `evennia_shards/hooks.py`). The replacement reproduces Evennia's prelude verbatim (protocol flags, `logged_in` OOB, connect-channel msg), then dispatches:

| `AUTO_PUPPET_ON_LOGIN` | Session / `_last_puppet` state | Outcome |
|---|---|---|
| `False` | any | **OOC character-select menu rendered** (vanilla else-branch behaviour — short-circuits before the library's redirect logic). |
| `True` | `session.protocol_flags["SHARDS_TICKET_AUTHED"]` is `True` (URL contained `?ticket=`) | OOC menu. **No auto-redirect** — the session was just sent here from a shard's OOC command, looping back would be infinite. |
| `True` | `_last_puppet` set with usable `shard_id` (in `SHARD_URLS`, not `"*"`) | `_redirect_to_character_shard(...)` — ticket created, OOB `shard_redirect` sent, the JS plugin closes the current WebSocket and opens a new one to the destination shard's WS URL with the ticket. |
| `True` | `_last_puppet` set but `shard_id` is `None` / `"*"` / not in `SHARD_URLS` | Warning logged, OOC menu rendered. Login does not fail. |
| `True` | `_last_puppet` is `None` (fresh first login, no last char) | OOC menu rendered silently. |

The override honours the consumer's `AUTO_PUPPET_ON_LOGIN` setting as the first decision: if the consumer has disabled auto-puppet, the library applies *none* of its redirect logic and renders the OOC menu — same observable outcome as vanilla. The library's redirect machinery only activates when `AUTO_PUPPET_ON_LOGIN = True`.

`_is_redirectable_character()` is the predicate that distinguishes the redirect-eligible row from the broken-state row when AUTO_PUPPET is True. The redirect itself reuses the same `_redirect_to_character_shard()` helper (in `evennia_shards/handoff.py`) that `ShardAwareCmdIC` uses, so both router-side entry points (manual `ic <char>` and login-time auto-puppet) share one code path. The helper is the library's single mechanism for "send a player session to a character's owning shard"; the `cross_shard_character_move` primitive (also in `handoff.py`) uses it for per-session redirect after a programmatic handoff.

### The `SHARDS_TICKET_AUTHED` protocol flag

`ShardWebSocketClient.onOpen()` sets `self.protocol_flags["SHARDS_TICKET_AUTHED"] = bool(token)` alongside the other `protocol_flags` assignments (`OOB`, `XTERM256`, etc.), where `token` is whatever was in the WebSocket URL's `?ticket=` parameter. Captures **URL presence**, not validation outcome — the flag tells `at_post_login` "this connection arrived via a ticket-bearing redirect, don't immediately auto-redirect them again" (which would be the infinite shard↔router loop).

**Lifecycle.** The flag is per-WebSocket-connection. On a deliberate cross-shard transfer (`shard_redirect.js` swapping the WS), the new connection arrives with `?ticket=X` in its WS URL, so the flag is set on that connection — the player lands at the OOC menu (or auto-puppets, depending on path) without bouncing. The flag does NOT persist across page refresh: a refresh creates a new WebSocket without `?ticket=X` in its URL (the URL bar stays at the router's HTTP host throughout, and never carries a ticket in the new design). On refresh from the OOC menu, the flag is False → if `_last_puppet` is still set with a usable shard_id, the auto-puppet path triggers a redirect back to that character. This is acceptable — refresh is observably equivalent to a fresh login, which auto-puppets via `_last_puppet` per Evennia defaults; the player still has `@ooc` as an explicit way to return to OOC.

**Why `protocol_flags` and not a direct attribute on the session.** Evennia's Portal and Server are separate processes. The Portal-side WebSocket protocol instance and the Server-side `ServerSession` are distinct Python objects in distinct memory spaces; the Portal sends session state to the Server over AMP, and only attributes listed in `settings.SESSION_SYNC_ATTRS` survive that boundary. A custom attribute like `self._ticket_authed` set in the Portal's `onOpen` would be silently dropped on the Server side. `protocol_flags` is a dict in the synced set — that's how Evennia carries `OOB`, `XTERM256`, `CLIENTNAME` etc. across to the Server — so storing the flag there means the Server's `at_post_login` reads what the Portal set.

The flag is set on the Portal of every non-monolith role (router and shard alike). Currently only the router's `at_post_login` override consumes it; future role-specific consumers may interpret it according to their own role and the prevailing redirect topology (e.g. shard↔shard transfer, if introduced). The library uses its own ticket mechanism (which it owns end-to-end) as the inbound-redirect signal, leaving Evennia's `_last_puppet` semantics untouched.

### Scope

The full override is router-only. Monolith uses vanilla Evennia. Shards wrap Evennia's original `at_post_login` with a thin cache-busting preamble (`make_shard_at_post_login` in `hooks.py`) — see the "Auto-puppet and `_last_puppet`" section above — but delegate entirely to vanilla Evennia for the actual auto-puppet logic.
