# Progress

A running log of high-level milestones as the project moves from design into build. Each entry is a brief note pointing to whatever artefact (test result, design doc, code change) is the evidence for that milestone. New entries go at the top.

This is not a changelog (use `git log` for that) and not a roadmap (the phasing lives in [archive/evennia-shards-HANDOVER.md](archive/evennia-shards-HANDOVER.md#phased-poc-plan)). It is a thin index of "what has actually happened so far."

## Milestones

### 2026-05-03 — Pre-emptive session detach: zombie session fix for cross-shard round-trips

Live smoke testing of `cross_shard_move_to` round-trips (shard0 → shard1 → shard0) exposed a zombie session bug that caused a black screen on the return move. Root cause: Evennia's asynchronous disconnect handler (`unpuppet_object`) runs after the WebSocket close triggered by the redirect, but by that point the character's `shard_id` has been mutated to the target shard and the bypass context has exited — so `pre_save` refuses.

**Full causal chain:**

1. Forward move (shard0 → shard1) commits correctly. WebSocket close fires `unpuppet_object` on the in-memory character whose `shard_id` is now `"shard1"` — `pre_save` refuses, creating a zombie session on shard0.
2. Return move (shard1 → shard0) commits correctly. Player arrives on shard0, `disconnect_duplicate_sessions` finds the zombie from step 1, tries to unpuppet it, `del obj.account → save` fails with the stale `shard_id`. AMP exception kills the entire `portal_connect`. `at_post_login` never fires. Black screen.

**First fix attempt (calling `unpuppet_object` inside bypass) failed.** Evennia's `at_post_unpuppet` hook does `self.db.prelogout_location = self.location`, which dereferences the location FK to the room on the target shard. The room is NOT in the bypass set, so `from_db` refuses.

**Working fix: minimal session detach.** Instead of calling Evennia's full `unpuppet_object`, `cross_shard_move_to` now clears `session.puppet = None` and `session.puid = None` for each puppeting session, and removes the `"puppeted"` tag from the character. This prevents the disconnect handler from entering the puppet cleanup path (its `if obj:` guard finds `None`), and prevents `server_maintenance` from trying to `from_db` a now-foreign row via `get_by_tag("puppeted")`.

**Why minimal detach is safe:** The destination shard's `puppet_object` overwrites `db_sessid` and `db_account` when the player arrives, so stale values in those fields are harmless. The skipped hooks (`at_pre_unpuppet`, `at_post_unpuppet`) are not needed because the character is leaving this process entirely.

**Files changed:** `handoff.py` (step 5 replaced: `unpuppet_object` call → minimal session detach), `tests.py` (`unpuppet_object` on `_FakeAccount`, `remove` on `_FakeSessionHandler`, `flush_from_cache` stub on `_FakeCharacter`).

164 tests passing. Full round-trip verified live: forward move, return move, OOC/IC cycling, cross-shard dig, all clean.

### 2026-05-03 — Idmapper / Attribute-cache staleness fix for cross-shard moves

Live smoke testing of `cross_shard_move_to` (shard0 → shard1 → shard0 round-trip) exposed two bugs caused by Evennia's in-memory caching defeating cross-process DB updates. Both share the same root cause: when one process updates a row's `shard_id`, other processes' caches still hold the old value.

**Bug 1: Router IC command redirects to wrong shard.** After moving a character from shard0 to shard1, going OOC back to the router, then typing `ic` — the router redirected to shard0 (old `shard_id`) instead of shard1.

- **Root cause:** Evennia's idmapper (`SharedMemoryModelBase.__call__`) returns the cached instance from `from_db()`, ignoring fresh DB values. This makes `refresh_from_db()` a no-op for cached instances.
- **Fix:** Added `flush_from_cache(force=True)` before `refresh_from_db()` in `ShardAwareCmdIC.func()` and `_is_redirectable_character()`.

**Bug 2: Black screen on return move to shard.** After a shard0 → shard1 move, then a shard1 → shard0 return move, the client arrives on shard0 but gets a blank screen. Portal log: `pre_save refused: shard 'shard0' cannot persist Character pk=1 owned by shard 'shard1'`.

- **Root cause:** The Account's Attribute-handler cache on shard0 still held the Python object from the outbound move (whose `shard_id` field was `"shard1"`). Evennia's default `at_post_login` read `_last_puppet` from this stale cache and handed the stale object to `puppet_object`, which tried to save it — tripping the `pre_save` chokepoint.
- **Fix:** Installed a thin `at_post_login` wrapper on shards (`make_shard_at_post_login` in `hooks.py`) that flushes the `_last_puppet` character from the idmapper and refreshes its fields from the DB before delegating to Evennia's original `at_post_login`.

**General pattern documented:** Any code path that reads an `ObjectDB` field which may have been updated by another process must use `flush_from_cache(force=True)` + `refresh_from_db()` before acting on the value. See [shard-isolation.md](shard-isolation.md#cross-process-cache-staleness) for the full write-up.

**Files changed:** `hooks.py` (flush+refresh in `_is_redirectable_character`, new `make_shard_at_post_login` factory), `commands.py` (flush+refresh in `ShardAwareCmdIC.func()`), `apps.py` (shard-side `at_post_login` wrapper installation alongside existing router override), `tests.py` (`flush_from_cache` stub on `_FakeCharacter`).

**Docs updated:** [ticket-auth-flow.md](ticket-auth-flow.md) (shards no longer use vanilla `at_post_login`), [library-integration-risks.md](library-integration-risks.md) (shard wrapper added to `at_post_login` coupling section), [shard-isolation.md](shard-isolation.md) (new "Cross-process cache staleness" section).

164 tests passing.

### 2026-05-02 — `cross_shard_move_to` spike 1: single-object move (unit-tested)

The first slice of the cross-shard handoff primitive landed in [`evennia_shards/handoff.py`](../evennia_shards/handoff.py). Spike 1 scope: move a single `ObjectDB`-derived row across shards, no recursion through `obj.contents`, with proper composition of the three primitives the handoff needs (atomic DB writes via the chokepoint bypass, idmapper eviction, per-session ticket+redirect).

The primitive composes:

1. Validate `target_shard` is configured and `target_location_pk` exists on it.
2. Atomic DB writes + idmapper eviction inside one `transaction.atomic()` block. Save and `flush_from_cache` are inside the bypass; on any exception, a defensive second eviction runs in the `except` branch so a rolled-back move doesn't leave the in-memory `obj` (whose `shard_id` was already mutated) lingering in the idmapper.
3. Per-session redirect via `_redirect_to_character_shard(session.account, session, obj)`. Uses the session's authenticated account (via `session.account`, not the character's FK) — more accurate semantically and decoupled from the character's `db_account` FK descriptor. Per-session failures captured in the returned `MoveResult`; the move itself doesn't roll back.

Three findings worth recording for future work:

- **`session.account`, not `obj.account`.** The session is the canonical source of "the account doing the move" — independent of the character's FK and consistent across multisession modes.
- **`_safe_contents_update` flag suppresses Evennia's post-save contents-cache update**, which would otherwise dereference `self.db_location` (the target room on the remote shard) and trip the `from_db` chokepoint. Same flag Evennia itself uses for analogous location-change paths.
- **Test setup uses `obj.__dict__["sessions"] = ...`** to shadow the lazy_property descriptor without going through Evennia's protective `__setattr__`. Real `ObjectDB` for the chokepoint-exercising parts; fake session handler for the redirect-counting parts.

156 tests passing (148 prior + 8 new in `CrossShardMoveToTests`): no-sessions, one-session, multi-session, target-shard-not-configured, target-location-doesn't-exist, target-location-on-wrong-shard, atomic-rollback-on-save-failure, session-redirect-failure-captured.

**Deferred to subsequent spikes:** recursion through `obj.contents` (spike 3), generalisation to non-character objects (spike 2 — same machinery, no session redirect needed), live smoke testing with real router+shard processes, contrib-layer typeclasses (`CrossShardExit`, `CrossShardCmdTeleport`).

### 2026-05-02 — Shard isolation refactor + `shard_writes_allowed_for` bypass primitive

The shard isolation mechanism was reorganised into a dedicated module and gained the long-anticipated bypass primitive — together they're the foundation Phase 2's `cross_shard_move_to` will be built on.

**Refactor.** The four chokepoints (`pre_save`, `pre_delete`, `from_db`, `QuerySet.update`) were extracted from `apps.py` into [`evennia_shards/isolation.py`](../evennia_shards/isolation.py). `apps.py` now calls a single `install_chokepoints()` entry point. Pure relocation, no behavioural change — the existing chokepoint test suite (~30 cases) passed without modification.

**Bypass primitive.** [`shard_writes_allowed_for(*objs)`](../evennia_shards/isolation.py) — a context manager that lifts the chokepoints for specific objects within a `with` block. Tracks identity two ways:

- `id(instance)` — checked by `pre_save` and `pre_delete` (instance-receiving chokepoints). Works for unsaved rows.
- `(concrete_model, pk)` — checked by `from_db` and `QuerySet.update` (which receive class + pk, not the instance). Normalised via `_meta.concrete_model` so a bypass entered with an Evennia typeclass instance (a Django proxy of `ObjectDB`) matches `from_db` calls where `cls` is `ObjectDB` itself.

Scoped to the `with` block, nesting-safe, exception-safe (cleanup runs in `finally`). Public API, exported from `evennia_shards.__init__`.

Documented in [shard-isolation.md](shard-isolation.md#bypass-shard_writes_allowed_for) — the doc gained a new "Bypass" section explaining the semantics, identity tracking, and composition with `transaction.atomic()` and `flush_from_cache()` for handoff scenarios.

148 tests passing (138 prior + 10 new in `ShardWritesAllowedForTests`): allows-remote-save, scoped-cleanup, no-auto-stamp-of-explicit-id, allows-remote-delete, only-listed-objects, nested-bypass, exception-cleanup, allows-from-db, allows-qs-update, partial-qs-update-still-raises.

### 2026-05-02 — `AUTO_PUPPET_ON_LOGIN = True` path: live smoke-test green

The router-side `at_post_login` override (landed 2026-05-01) verified end-to-end with live smoke testing under both auto-puppet modes. Two corrections were applied during the smoke test cycle:

1. **Portal/Server AMP sync.** First-cut implementation stored the OOC-return signal as a direct attribute (`self._ticket_authed`) on the WebSocket protocol instance. Live testing showed the flag was set on the Portal-side object but read as `False` on the Server side — Python object ids differed between set and read. Cause: Evennia's Portal and Server are separate processes; only attributes listed in `settings.SESSION_SYNC_ATTRS` survive the AMP crossing, and arbitrary attributes don't. Fix: store the flag in `protocol_flags["SHARDS_TICKET_AUTHED"]` instead — `protocol_flags` is in the synced set (it carries `OOB`, `XTERM256`, etc.), so the value reaches the Server intact.

2. **Honour `AUTO_PUPPET_ON_LOGIN = False`.** Smoke testing under `AUTO_PUPPET_ON_LOGIN = False` showed the override was *still* auto-redirecting — vanilla Evennia would render the OOC menu unconditionally in that case. The override was forcing True-shaped behaviour regardless of the consumer's setting (a divergence from Evennia and a violation of the two-audiences principle). Fix: short-circuit at the top of the override after the prelude — when `AUTO_PUPPET_ON_LOGIN` is `False`, render the OOC menu and return without applying any of the library's redirect logic.

After both fixes, both modes work as expected:

| `AUTO_PUPPET_ON_LOGIN` | Behaviour |
|---|---|
| `False` | OOC menu always rendered; library redirect machinery dormant. Vanilla parity. |
| `True` | Auto-puppet path produces a redirect to the character's owning shard; OOC return from a shard lands at the router's OOC menu (no loop). |

Debug instrumentation added during diagnosis (`SHARDS-DEBUG-TICKET-FLAG` markers in `protocols.py` and `hooks.py`) has been removed. **138 tests passing** (137 prior + 1 new `test_auto_puppet_disabled_renders_ooc_menu_unconditionally`).

Phase 1 of the original PoC plan (router + 1 shard, both auto-puppet modes) is now functionally complete and verified live. Phase 2 (cross-shard handoff, gateway primitives, character movement between shards) is the next milestone.

### 2026-05-01 — `AUTO_PUPPET_ON_LOGIN = True` path: router `at_post_login` override

Closes the auth/redirect feature: both `AUTO_PUPPET_ON_LOGIN = True` and `False` paths now work on the router. With auto-puppet=True, login itself triggers the redirect; with False, the player goes through the OOC menu and types `ic <char>`.

`shard_aware_at_post_login` (in [evennia_shards/hooks.py](../evennia_shards/hooks.py), new module) replaces `DefaultAccount.at_post_login` on routers via monkey-patch in `AppConfig.ready()`. The override reproduces Evennia 6.0.0's prelude verbatim, then dispatches three ways via the `_is_redirectable_character()` predicate:

| `_last_puppet` state | Outcome |
|---|---|
| set with usable `shard_id` | redirect via `_redirect_to_character_shard(...)` (the helper extracted in commit `946bc2e`, now reused by both the IC command and the login hook) |
| set with broken `shard_id` (`None`, `"*"`, not in `SHARD_URLS`) | log warning, render OOC menu — login does not fail |
| `None` | render OOC menu silently |

Install gating: inline `if get_role() == ROLE_ROUTER:` in `AppConfig.ready()`, alongside the existing CmdIC/CmdOOC patches. Shards originally kept vanilla `at_post_login` — that was their auto-puppet path after ticket-auth. *(Later amended: shards now wrap the original with a cache-busting preamble — see 2026-05-03 milestone.)*

New coupling section added to [library-integration-risks.md](library-integration-risks.md#defaultaccountat_post_login-override) covering Evennia upgrade and consumer override risks.

`ShardAwareCmdOOC` was extended to clear `account.db._last_puppet` before creating the ticket. Without this, a player on a shard typing `ooc` would land on the router, the router's `at_post_login` would see `_last_puppet` still set, and the player would be auto-redirected straight back to the shard — an infinite loop. The clearing diverges from vanilla `CmdOOC` (which sets `_last_puppet = old_char`); rationale documented in [ticket-auth-flow.md](ticket-auth-flow.md#ooc-command-override). A multi-puppet edge case (modes 2/3) is captured in [open-questions.md](open-questions.md) as an Evennia upstream limitation that the library inherits.

**Post-smoke-test correction (same day):** the `_last_puppet = None` clearing approach failed live testing. Cross-process Attribute writes don't propagate fast enough — the router reads the stale value (or its idmapper / AttributeHandler serves a cached one), redirects back to the shard, and the shard's vanilla `at_post_login` then sees `None` (clear has caught up) and dies with `"The Character does not exist."` Replaced with a per-session signal set in `ShardWebSocketClient.onOpen()` based on URL presence of `?ticket=`, stored in `protocol_flags["SHARDS_TICKET_AUTHED"]` (initially attempted as a direct attribute `self._ticket_authed`, but the Portal/Server AMP sync drops attributes not listed in `SESSION_SYNC_ATTRS` — only `protocol_flags` survives the crossing). The router's `at_post_login` checks the flag before consulting `_last_puppet` — any session whose URL carried a ticket is, by construction, an OOC-return target and gets the OOC menu without auto-redirect. `_last_puppet` is left vanilla; the `_last_puppet = None` mutation in `ShardAwareCmdOOC` was reverted, replaced by a canary asserting we *don't* mutate it. Aligns with the two-audiences principle (minimum divergence from Evennia).

137 tests passing (128 prior + 3 in `AtPostLoginRouterTests` + 1 ticket-flag test in `AtPostLoginRouterTests` + 4 in `TicketAuthedFlagTests` + 1 canary on `ShardAwareCmdOOCShardTests`). Live smoke test pending.

### 2026-05-01 — OOC command override: shard→router redirect proven end-to-end

`ShardAwareCmdOOC` completes the bidirectional ticket flow. A player IC on a shard types `ooc`, the shard creates a ticket targeting the router, and redirects the client back. The router ticket-auths the account back into OOC state. Full round-trip proven with live smoke test (router + shard0, localhost multi-instance).

**What was proven**:

| Step | Result |
|------|--------|
| `ooc` on shard (while IC) | Ticket created with `old_char.id`, `to_shard="router"` |
| `shard_redirect` OOB sent | Browser navigates to router webclient with ticket |
| Router ticket auth | Player is OOC on the router |
| Full round-trip: router → IC → shard → OOC → router | Works end-to-end |

**Implementation**:

- `ShardAwareCmdOOC` (`evennia_shards/commands.py`): subclasses Evennia's `CmdOOC`, overrides `func()`. Character ID fallback chain: `old_char.id` → `_last_puppet.id` → `0` (sentinel with `log_warn`). Always redirects — even error states with no puppet.
- `AppConfig.ready()` monkey-patch: replaces `CmdOOC` on `evennia.commands.default.account` module, gated on `get_role() == "shard"` only. Asymmetric with IC (which patches both router and shard) because vanilla OOC on the router is correct behaviour.
- No explicit unpuppet: page navigation closes WebSocket, Evennia's disconnect handler releases the character automatically.

127 tests passing. See [ticket-auth-flow.md](ticket-auth-flow.md) for the OOC command override design and remaining work (`at_post_login` override for auto-puppet = true).

### 2026-05-01 — Router shard ID: library mandate and accessor

Added `get_router_shard_id()` accessor returning the hardcoded constant `"router"`. This is a library mandate — the router's `SHARD_ID` must be `"router"`, not configurable. Needed by shards to populate `to_shard` in OOC redirect tickets (and any future cross-shard ticket targeting the router).

**What landed:**

- `ROUTER_SHARD_ID = "router"` constant and `get_router_shard_id()` accessor in `config.py`
- Exported in `__init__.py` and `__all__`
- `RouterShardIdAccessorTests` in `tests.py`
- `ROUTER_SHARD_ID` documented in `shard-settings.md` settings table and usage example

121 tests passing.

### 2026-05-01 — IC command override: router→shard redirect proven end-to-end

The `AUTO_PUPPET_ON_LOGIN = False` path is complete. A player logs into the router OOC, types `ic <character>`, and the router resolves the character, creates a ticket, and redirects the client to the character's shard — where ticket auth + auto-puppet puts them IC. Proven with live smoke test (router + shard0, localhost multi-instance).

**What was proven**:

| Step | Result |
|------|--------|
| `ic <character>` on router | Character resolved, `_last_puppet` set, ticket created |
| `shard_redirect` OOB sent | Browser navigates to shard webclient with ticket |
| Shard ticket auth + auto-puppet | Player is IC on the shard |
| `ic` on shard | "Leave this character before trying to enter another one." |

**Implementation**:

- `ShardAwareCmdIC` (`evennia_shards/commands.py`): subclasses Evennia's `CmdIC`, overrides `func()` with role-gated behaviour. Character resolution logic extracted into `_resolve_character()` (copied from parent — can't call `super().func()` since it calls `puppet_object()`).
- `AppConfig.ready()` monkey-patch: replaces `CmdIC` on `evennia.commands.default.account` module. Same injection pattern as WebSocket protocol and middleware overrides.
- Router exemption from shard isolation chokepoints (implemented in previous session) is load-bearing — the router must load characters from any shard to resolve them.

120 tests passing. See [ticket-auth-flow.md](ticket-auth-flow.md) for remaining work (`at_post_login` override for auto-puppet = true, OOC command override).

### 2026-04-30 — Client redirect spike proven end-to-end

The client-side redirect mechanism from [ticket-auth-flow.md](ticket-auth-flow.md) is validated on the `bespoke` branch. An OOB `shard_redirect` message triggers a full page navigation to the target instance's webclient with a ticket token. The target instance's middleware injects the token into the WebSocket connection, and the auth cascade logs the player in.

**What was proven** (live smoke test on router instance redirecting to itself):

| Step | Result |
|------|--------|
| Server sends `shard_redirect` OOB | JS plugin catches it (direct emitter listener bypasses `default_out.js`) |
| `window.location.href` navigates to target | Browser loads target webclient page |
| Middleware injects `&ticket=TOKEN` into `window.csessid` | Token flows into WebSocket URL query string |
| `onOpen()` auth cascade: session first, then ticket | Ticket validated, auto-login succeeds |
| Page refresh with stale `?ticket=` in URL | Browser session takes priority, stale token ignored |

**Key implementation decisions**:

- **Full page redirect** (not WebSocket-level reconnect): avoids fighting Evennia's private `WebsocketConnection` class, URL construction conflicts (`wsurl + '?' + csessid`), and emitter lifecycle. Browser address bar updates, so refresh stays on the target instance.
- **Auth priority reorder**: browser session checked before ticket (was ticket-first). Load-bearing for page refresh — tickets are single-use, so checking them first would fail on refresh after consumption.
- **Middleware dual role**: injects both the `shard_redirect.js` plugin (always) and the ticket injection script (only when `?ticket=` in page URL). Zero consumer config beyond `INSTALLED_APPS`.

**Components**:

- `shard_redirect.js`: OOB listener → `window.location.href = url`
- `middleware.py`: `ShardRedirectScriptMiddleware` — script tag injection + ticket-to-csessid injection
- `protocols.py`: `onOpen()` refactored from two-phase to single-phase with three-way auth cascade (session → ticket → role gate)

96 tests passing. See [ticket-auth-flow.md](ticket-auth-flow.md) for remaining work (auto-puppet, IC/OOC command overrides).

### 2026-04-30 — Ticket auth: auto-login spike proven end-to-end

The ticket-based auto-login flow from [ticket-auth-flow.md](ticket-auth-flow.md) is validated end-to-end on the `bespoke` branch. A WebSocket connection arriving with `?ticket=<token>` is intercepted, validated, consumed, and the session is auto-logged-in before `sessionhandler.connect()` fires — so the Server sees `logged_in=True` and `uid` already set, triggering Evennia's built-in `portal_connect()` auto-login path.

**What was proven** (live smoke test with `test_ticket_ws.py` and browser against the demo game):

| Case | Mode | Result |
|------|------|--------|
| Valid ticket | Router | Auto-login (`["logged_in", ...]` received) |
| Valid ticket | Shard | Auto-login (`["logged_in", ...]` received) |
| Invalid/bogus token | Router | Rejected with error + close code 4001 |
| Invalid/bogus token | Shard | Rejected with error + close code 4001 |
| No token (browser) | Router | Normal login screen shown |
| No token (browser) | Shard | Rejected ("this shard requires a ticket") |
| Reused token | Both | Rejected (single-use enforced) |

**Key implementation**: two-phase `onOpen()` override in `ShardWebSocketClient`:

- **Phase 1** (pre-session): extract token from URL, validate ticket, abort on failure — no session registered, no login screen. Role gating: shards reject tokenless connections, routers allow them.
- **Phase 2** (reproduced Evennia `onOpen()`): `init_session()` → inject `uid` + `logged_in` from ticket → `sessionhandler.connect()`. The injection point is between these two calls — the only clean seam (see [library-integration-risks.md](library-integration-risks.md) for rationale and rejected alternatives).

**New helpers**: `_get_client_address()` (proxy-aware IP resolution), `_validate_ticket()` (pure validation, returns `(bool, data|error)`), `_extract_ticket_token()` (URL query parsing), `_send_text()` (Evennia JSON protocol wrapper).

**New files**: `DESIGN/library-integration-risks.md` (documents the `onOpen()` override and what to diff on Evennia upgrades), `settings_router.py` / `settings_shard0.py` (role-specific settings for the demo game).

96 tests passing. See [ticket-auth-flow.md](ticket-auth-flow.md) for remaining work (auto-puppet, client-side redirect). Note: the two-phase `onOpen()` was later refactored into a single-phase auth cascade — see the "Client redirect spike" milestone above.

### 2026-04-30 — Ticket auth: full validation spike proven

The ticket-based auth flow from [ticket-auth-flow.md](ticket-auth-flow.md) is validated end-to-end on the `bespoke` branch. A WebSocket connection arriving with `?ticket=<token>` is intercepted, looked up by token PK, IP-validated, consumed (single-use), and the result reported to the client.

**What was proven** (live smoke test with `test_ticket_ws.py` against the demo game):

| Case | Result |
|------|--------|
| Valid ticket + matching IP | `Ticket validated` with correct account/character IDs |
| Valid ticket + wrong IP | `Ticket rejected: IP mismatch` |
| Valid ticket + no IP pinning | `Ticket validated` (IP check skipped) |
| No ticket in URL | Normal login screen, no ticket messages |
| Bogus/nonexistent token | `Ticket not found or wrong shard` |
| Reused consumed token | `Ticket not found` (single-use enforced) |

**Components**:

- **`Ticket` model** (`models.py`): `token` (PK), `account_id`, `character_id`, `to_shard`, `client_ip` (nullable, for IP pinning), `created_at`. Migrations `0003` + `0004`.
- **Ticket primitives** (`tickets.py`): `create_ticket()`, `get_ticket()`, `delete_ticket()`.
- **`ShardWebSocketClient`** (`protocols.py`): token extraction from URL query string, ticket validation with IP check, single-use consumption. Dynamic base class preserves consumer customisations.
- **`AppConfig.ready()` wiring** (`apps.py`): protocol override gated on `get_role() != "monolith"`.

90 tests passing. See [ticket-auth-flow.md](ticket-auth-flow.md) for remaining work (auto-login, puppet hook, client-side redirect).

### 2026-04-30 — Ticket auth: protocol override PoC proven

The WebSocket protocol override mechanism from [ticket-auth-flow.md](ticket-auth-flow.md) is wired and proven on the `bespoke` branch:

- **`ShardWebSocketClient`** (`evennia_shards/protocols.py`): subclass of the consumer's configured `WEBSOCKET_PROTOCOL_CLASS` (not Evennia core directly — see "dynamic base class" below). Overrides `onOpen()` to intercept WebSocket connections. Currently sends a PoC message; next step is extracting `?ticket=<token>` from the URL.
- **`AppConfig.ready()` wiring** (`evennia_shards/apps.py`): stashes the consumer's current `WEBSOCKET_PROTOCOL_CLASS` value, then overwrites it to point to `ShardWebSocketClient`. Gated on `get_role() != "monolith"` — monolith mode uses normal login exclusively.
- **Dynamic base class**: `protocols.py` resolves the stashed original class via `class_from_module` at import time and subclasses *that*, preserving any consumer customisations to the WebSocket protocol. The library layers on top rather than replacing.
- **Proven live**: demo game running as `shard` role displays `[evennia-shards] Protocol override active.` on WebSocket connect — before Evennia's own connection screen — with zero changes to the demo game's code.

72 tests passing. See [ticket-auth-flow.md](ticket-auth-flow.md) for the full design and remaining work.

### 2026-04-29 — Cross-shard message bus: primitives + lifecycle land

The bus from [cross-shard-message-bus.md](cross-shard-message-bus.md) is in place on the `bespoke` branch, end-to-end:

- **`Message` model + migration `0002`** (commit `463f2b6`): `id`, `created_at`, `to_shard`, `from_shard`, `kind`, `payload` (JSONField), composite index on `(to_shard, created_at)`. Also `get_message_timeout(kind)` accessor + `SHARDS_MESSAGE_TIMEOUT_DEFAULT` / `SHARDS_MESSAGE_TIMEOUTS` settings, same override pattern as `get_role` / `get_shard_id`.
- **Primitives** — `send_message` (`4511139`), `poll_messages` (`88547b7`), `delete_message` (`7ac2dd5`). Module-level functions in `evennia_shards/messagebus.py`.
- **Same-shard send guard** (`e3a992c`): new `MessageBusError` exception; `send_message` refuses if `to_shard == from_shard` (after defaulting), since the bus is for cross-shard communication and same-shard sends are almost always a misconfigured `SHARD_ID`.
- **Polling cycle + handler hook** (`566bf11`): `process_inbox(handler)` runs one cycle (poll → dispatch → delete on success); `start_message_bus(handler, interval)` wraps it in a Twisted `LoopingCall`. `MessageHandler` base class is the consumer-overrideable hook — subclasses call `super().handle(message)` to compose library handling with their own kind dispatch. Library-shipped kinds: `ping` (replies with `ping_received`), `ping_received` and `undeliverable_reply` (silently consumed in the base). `examples/demo_game/server/conf/at_server_startstop.py` calls `start_message_bus()` from `at_server_start()` when role is non-monolith.
- **Timeout / undeliverable_reply lifecycle** (`779f611`): `process_inbox` now does the three-way decision per message — handler truthy → delete, falsy + age ≤ lifespan → defer, falsy + age > lifespan → insert `undeliverable_reply` to original `from_shard` (with `original_kind`, `original_payload`, `reason="timeout"`) and delete the original. If `from_shard` is missing or equals current shard, log a warning and drop without reply.

60 tests passing in ~1.5s. The bus is primitive-complete; deeper kinds (`character_handoff` for the gateway protocol) are deliberately deferred to Phase 2.

### 2026-04-29 — Bespoke spike: all four chokepoints land with isolated tests

The `bespoke` branch now carries all four chokepoints documented in [shard-isolation.md](shard-isolation.md), with full automated test coverage. The four-chokepoint spike is functionally complete.

- **`pre_save` chokepoint** (commit `80226be`): the existing auto-stamp handler grew a second arm — refuse the save if `instance.shard_id` is set and is neither the current shard nor `"*"`. New `ShardIsolationError` exception type. Live smoke test confirmed via in-game `@py`.
- **`pre_delete` chokepoint** (commit `0bcce76`): mirrors `pre_save` minus the auto-stamp arm. Refuses to delete a row whose `shard_id` is neither current nor `"*"` (and not `None`, since legacy/unstamped rows are tolerated). Covers both `instance.delete()` and `qs.delete()` because Django fires `pre_delete` per affected row even on bulk queryset deletes.
- **`from_db` chokepoint** (commit `c7510ed`): patches `ObjectDB.from_db` from `AppConfig.ready()` with a closure-captured replacement that inspects `shard_id` in the row data before delegating to the original. Refuses construction if `row.shard_id` is set and is neither current nor `"*"`. Inherited automatically by typeclass subclasses (Room, Character, ...) via Python MRO. Covers all three Django call sites: `ModelIterable` (queryset iteration), `RawModelIterable` (raw queries), `RelatedPopulator` (select_related). Idempotent via a marker attribute against dev reload.
- **`QuerySet.update()` chokepoint** (this commit): patches the queryset class returned by `ObjectDB.objects.get_queryset()` so that `update()` runs an upfront `values_list("shard_id")` SELECT to detect any non-owned, non-global rows in the queryset's scope. Raises before issuing the UPDATE if any are found, so owned rows in a mixed queryset are not partially modified. Inner `issubclass(self.model, ObjectDB)` guard so the patched class only enforces for ObjectDB-derived models in case the queryset class is shared.

**Test infrastructure decoupled from `examples/demo_game/`:** `tests/test_settings.py` + `runtests.py` run the suite against an in-memory sqlite database with `evennia_shards` in `INSTALLED_APPS`, using `BaseEvenniaTestCase` to force `evennia.game_template.*` fallbacks. No gamedir needed. See [testing-setup.md](testing-setup.md). 23 tests passing (3 config + 1 app setup + 4 pre_save + 5 pre_delete + 5 from_db + 5 qs.update) in ~0.7s.

**What this proves:**

- All four chokepoints function exactly as designed in `shard-isolation.md` — read, save, delete-instance, delete-queryset, and bulk-update operations on remote-shard rows raise loudly with shard ids in the message.
- The chokepoints compose cleanly: `from_db` catches read-side leaks, the signals catch instance-level writes, and the QuerySet override catches bulk updates. No idmapper subclassing, no broad manager replacement.
- `.values()` / `.values_list()` correctly bypass `from_db` (per design — they return row data without constructing instances), and the `QuerySet.update` chokepoint uses this bypass internally to inspect remote rows for the upfront check.
- The library has a deterministic, hermetic test suite that runs in under a second.

**Beyond the four-chokepoint spike** (Phase 2 / out of scope here):

- ~~Cross-shard ownership handoff and the bypass primitive (`shard_writes_allowed_for(...)`).~~ *Both landed 2026-05-02 — bypass primitive and cross_shard_move_to spike 1 (single-object move) are working with full unit-test coverage. See milestones above.*
- Backfill migration for legacy NULL rows.
- ~~Revisit the comparison with `django-multitenant` on the parallel `django-multitenant` branch.~~ *Decided in favour of bespoke chokepoints — see [shard-isolation.md](shard-isolation.md#decision-bespoke-chokepoints-vs-django-multitenant). The `django-multitenant` branch was discontinued without merging.*

### 2026-04-29 — Auto-stamp on save works (hybrid pre_save signal)

A pre_save signal handler in `EvenniaShardsConfig.ready()` now stamps `shard_id` to the current process's `SHARD_ID` whenever an `ObjectDB` (or subclass) is saved with `shard_id == None`. Explicit values (e.g. those set during a cross-shard handoff) are respected. Verified end-to-end: after a clean DB wipe + `evennia migrate` + `evennia start`, the bootstrap rows (`#1` superuser character, `#2` Limbo) and a runtime-dug `test` room (`#3`) all reported `shard_id = 'shard0'` via both the ORM and a raw SQL probe.

**Key implementation finding** (worth recording): Evennia's typeclass system uses concrete Django subclasses of `ObjectDB` — `Room`, `Character`, `Exit`, and consumer-defined typeclasses — that all share the `ObjectDB` table. Django dispatches `pre_save` with `sender = type(instance)`, which is the subclass, never the `ObjectDB` base. A naïve `pre_save.connect(handler, sender=ObjectDB)` therefore matches *zero* saves of game-world objects. The fix is to connect without a sender filter and do an `isinstance(instance, ObjectDB)` check inside the handler. Performance cost of the universal handler is negligible (microseconds per save).

**What this proves:**

- Auto-population works for both bootstrap-time saves (via `at_initial_setup`) and runtime saves (via `dig` or any `create_object` path).
- The "if shard_id is None" guard is load-bearing: it lets explicit consumer/library code (cross-shard handoff, central seed scripts) set values that the signal will respect.
- Lazy-backfill side effect: legacy NULL rows would auto-populate on their next save, useful for monolith-to-shard adoption but not a substitute for an explicit migration backfill.

**What this does *not* prove** (next spikes):

- Backfill of pre-existing rows that never save again (the explicit `RunPython` migration is still required for that).
- Auto-filtering manager composition with Evennia's `SharedMemoryManager` (idmapper) — the next big architectural unknown.
- Cross-shard `UPDATE` semantics during handoff.

### 2026-04-29 — Migration spike confirmed: `shard_id` column on `ObjectDB` is viable

A small spike proved the foundational partitioning mechanism. Library now ships an `apps.py` AppConfig and a `0001_add_shard_id_to_objectdb` migration; in shard mode the demo game adds `evennia_shards` to `INSTALLED_APPS` via a one-line conditional in `settings.py`. After `evennia migrate`, an in-game `@shard_check` command confirmed both ORM-level (`ObjectDB._meta` knows the field) and database-level (raw `SELECT shard_id` returns) presence of the column on existing rows.

**What this proves:**

- A library-shipped Django migration can add a column to Evennia's `ObjectDB` table via `RunSQL`, anchored to Evennia's own migration history.
- `add_to_class` from `AppConfig.ready()` makes the new field visible to the ORM without a model fork.
- The library can be a Django app conditionally (only when `SHARDS_ROLE != "monolith"`), and the cross-app migration sequencing under `evennia migrate` works without bespoke command flow.
- Consumer adoption is three lines in `settings.py` (`SHARDS_ROLE`, `SHARD_ID`, conditional `INSTALLED_APPS`).

**What this does *not* prove** (next spikes):

- Auto-population of `shard_id` on object creation (pre_save signal mechanism untested).
- Backfill of pre-existing rows (`#1` superuser, `#2` Limbo currently `NULL`).
- Auto-filtering manager composition with Evennia's `SharedMemoryManager` (idmapper).
- Cross-shard `UPDATE` semantics during handoff.

### 2026-04-28 — Case 1 gate re-run with first library code (still satisfied)

Re-ran `evennia test evennia` after the `config.py` accessors landed. Result identical to the previous gate run: 1662 / 2 errors / 38 skipped, same two errors (both missing optional Evennia contrib dependencies, unrelated to evennia-shards). The library's first real code is provably non-perturbing of Evennia's test suite. See [test-history/test_results_3_2026-04-28.md](test-history/test_results_3_2026-04-28.md).

### 2026-04-28 — Config accessor wire proven (live, in-game)

First piece of real library code: [evennia_shards/config.py](../evennia_shards/config.py) with `get_role()` / `get_shard_id()` accessors. Settings design documented in [shard-settings.md](shard-settings.md) and load-bearing principle 9 added to [CLAUDE.md](../CLAUDE.md). Wire proven end-to-end with a temporary `@shards_debug` superuser command in the demo game (since reverted): both accessors return the documented defaults when the consumer declares nothing, and return the consumer-declared values when overridden. See [test-history/test_results_2_2026-04-28.md](test-history/test_results_2_2026-04-28.md). Case 1 gate re-run with the new library code is still outstanding.

### 2026-04-28 — Case 1 gate satisfied (empty-library state)

Re-ran `evennia test evennia` with `evennia_shards 0.0.1` installed (`pip install -e .` from repo root). Result identical to baseline: 1662 / 2 errors / 38 skipped, same two errors. Zero delta — the library is genuinely dormant in monolith mode at its current (empty) state. Gate must be re-run after each future change that could execute at import or app-ready time. See [test-history/test_results_1_2026-04-28.md](test-history/test_results_1_2026-04-28.md).

### 2026-04-28 — Baseline test run (vanilla Evennia)

Ran `evennia test evennia` against vanilla Evennia 6.0.0 (with `evennia_shards` *not* installed) to establish the reference for the Case 1 verification gate. Result: 1662 tests, 2 errors (both missing optional contrib dependencies — `xyzgrid` needs scipy, `git_integration` needs GitPython), 38 skipped. See [test-history/test_results_0_2026-04-28.md](test-history/test_results_0_2026-04-28.md).
