# Library Integration Risks

Where `evennia-shards` couples to Evennia internals, and the risks each coupling carries. Two audiences read this doc:

- **Library maintainer upgrading Evennia** — diff each coupling against the new Evennia version.
- **Consumer game subclassing or customising Evennia** — know which Evennia surfaces the library has already taken a position on, and the recommended composition pattern.

Each coupling section follows the same template:

- **What we patch / extend** — the Evennia symbol the library touches, and where the library code lives.
- **Why** — the requirement that drives the coupling.
- **Risk on Evennia upgrade** — what to diff in the new Evennia version.
- **Risk in consumer override** — what consumer-side customisation would collide, and the recommended pattern.

This doc is filled in lazily — a coupling is added when it first lands or is next touched. Currently covered: WebSocketClient.onOpen. Other library couplings (CmdIC/CmdOOC patches, ObjectDB.from_db, pre_save/pre_delete signals, QuerySet.update, WEBSOCKET_PROTOCOL_CLASS rewiring, middleware injection) will be backfilled as we revisit them.

## WebSocketClient.onOpen() override

**What we patch / extend:** `evennia/server/portal/webclient.py` → `WebSocketClient.onOpen()`. Library code: `evennia_shards/protocols.py` → `ShardWebSocketClient.onOpen()`. Based on Evennia 6.0.0.

**Why:**

Ticket-based auth needs `self.uid` and `self.logged_in` set on the session *after* `init_session()` (which resets both to `None`/`False`) but *before* `sessionhandler.connect()` (which snapshots session state via `get_sync_data()` and sends it to the Server over AMP). The Server's `portal_connect()` auto-logins sessions that arrive with `logged_in=True` and a valid `uid`.

There is no method-level seam between `init_session()` and `sessionhandler.connect()` — both are called inline in `onOpen()`. Three alternatives were ruled out:

- **Swapping call order** (ticket validation before `super().onOpen()`): `init_session()` wipes `uid`/`logged_in`, and `self.address` isn't set yet for IP validation.
- **Post-connect re-sync** (`sessionhandler.sync()`): the Portal's `sync()` deliberately excludes `uid` and `logged_in` from the data it sends to the Server. There is no Portal→Server "please login this session" AMP operation.
- **Overriding `get_sync_data()`**: works mechanically but splits the auth logic across two methods with non-obvious interaction, and makes a data-serialisation method responsible for auth decisions.

Overriding `onOpen()` is the only clean approach. Our override reproduces the parent method and adds a ticket-auth check in the same position as the existing browser-session auth check (between `init_session()` and `sessionhandler.connect()`).

**Risk on Evennia upgrade:**

- New logic added between `init_session()` and `sessionhandler.connect()` — our override would miss it.
- Changes to the `init_session()` / `get_client_session()` / `sessionhandler.connect()` call sequence.
- New protocol flags or connection setup steps added to `onOpen()`.
- Changes to `SESSION_SYNC_ATTRS` that affect what `get_sync_data()` sends.

How to check: diff the upstream `onOpen()` against the snapshot in our override. The override carries a comment citing the Evennia version it was based on.

**Risk in consumer override:**

A consumer that sets a custom `WEBSOCKET_PROTOCOL_CLASS` is **safe by construction**: `AppConfig.ready()` stashes the consumer's class as `_SHARDS_ORIGINAL_WS_PROTOCOL` and `ShardWebSocketClient` subclasses *that* dynamically. Consumer customisations are preserved underneath the library's onOpen logic.

The hazard is a consumer overriding `onOpen()` on their custom class without calling `super().onOpen()` — that bypasses our ticket-auth injection entirely. Recommended pattern: any consumer override of `onOpen()` must call `super().onOpen()` (or accept that ticket-auth will not run on their connections).
