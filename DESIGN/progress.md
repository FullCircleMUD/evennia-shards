# Progress

A running log of high-level milestones as the project moves from design into build. Each entry is a brief note pointing to whatever artefact (test result, design doc, code change) is the evidence for that milestone. New entries go at the top.

This is not a changelog (use `git log` for that) and not a roadmap (the phasing lives in [archive/evennia-shards-HANDOVER.md](archive/evennia-shards-HANDOVER.md#phased-poc-plan)). It is a thin index of "what has actually happened so far."

## Milestones

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
- **Phase 2** (reproduced Evennia `onOpen()`): `init_session()` → inject `uid` + `logged_in` from ticket → `sessionhandler.connect()`. The injection point is between these two calls — the only clean seam (see [evennia-upgrade-checklist.md](evennia-upgrade-checklist.md) for rationale and rejected alternatives).

**New helpers**: `_get_client_address()` (proxy-aware IP resolution), `_validate_ticket()` (pure validation, returns `(bool, data|error)`), `_extract_ticket_token()` (URL query parsing), `_send_text()` (Evennia JSON protocol wrapper).

**New files**: `DESIGN/evennia-upgrade-checklist.md` (documents the `onOpen()` override and what to diff on Evennia upgrades), `settings_router.py` / `settings_shard0.py` (role-specific settings for the demo game).

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

- Cross-shard ownership handoff and the bypass primitive (`shard_writes_allowed_for(...)`).
- Backfill migration for legacy NULL rows.
- Revisit the comparison with `django-multitenant` on the parallel `django-multitenant` branch.

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
