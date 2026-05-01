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
- **`_last_puppet` is cleared** before the ticket is created. This is a deliberate divergence from vanilla `CmdOOC`, which sets `_last_puppet = old_char` so a subsequent bare `ic` re-enters that character. We diverge because under sharding the router has an `at_post_login` override that auto-redirects to whatever shard `_last_puppet` lives on. If `_last_puppet` were preserved across the OOC redirect, the router's auto-puppet path would immediately bounce the player back to the shard they just left — an infinite loop. Clearing it lets the router fall through to the OOC menu (since `_is_redirectable_character(None) == False`). The trade-off: after `ooc`, a bare `ic` no longer re-enters the previous character; the player must type `ic <name>`.
- **No explicit unpuppet**: the redirect triggers a full page navigation (`window.location.href`), which closes the WebSocket connection. Evennia's disconnect handler (`sessionhandler.disconnect()` → `account.unpuppet_object()`) automatically releases the character on the shard when the connection drops.
- **Router**: vanilla `CmdOOC` stays — normal unpuppet, player stays on the router OOC.
- **Monolith**: vanilla `CmdOOC` stays; the override is never injected.

The asymmetry with IC (which patches both router and shard) is intentional: IC on a shard without the override would attempt a local puppet, which would either hit chokepoints or cause confusion. OOC on a router is harmless — the vanilla command does exactly what's needed (unpuppet, show OOC menu).

## Auto-puppet on login (`AUTO_PUPPET_ON_LOGIN = True`)

Evennia's default `AUTO_PUPPET_ON_LOGIN = True` calls `account.puppet_object(session, self.db._last_puppet)` from inside `at_post_login`. On a router that's broken — see [library-integration-risks.md](library-integration-risks.md#defaultaccountat_post_login-override) for why.

The library replaces `DefaultAccount.at_post_login` on routers with `shard_aware_at_post_login` (in `evennia_shards/hooks.py`). The replacement reproduces Evennia's prelude verbatim (protocol flags, `logged_in` OOB, connect-channel msg), then dispatches three ways:

| `_last_puppet` state | Outcome |
|---|---|
| set with usable `shard_id` (in `SHARD_URLS`, not `"*"`) | `_redirect_to_character_shard(...)` — ticket created, OOB `shard_redirect` sent, player navigates to the correct shard. |
| set but `shard_id` is `None` / `"*"` / not in `SHARD_URLS` | Warning logged, OOC character-select menu rendered. Login does not fail. |
| `None` (normal first login) | OOC menu rendered silently. |

`_is_redirectable_character()` is the predicate that distinguishes the first two rows. The redirect itself reuses the same `_redirect_to_character_shard()` helper that `ShardAwareCmdIC` uses, so both router-side entry points (manual `ic <char>` and login-time auto-puppet) share one code path.

The override is router-only. Monolith uses vanilla Evennia. Shards keep vanilla `at_post_login` because that's the auto-puppet path that puts a player IC after ticket-auth has populated `_last_puppet`.
