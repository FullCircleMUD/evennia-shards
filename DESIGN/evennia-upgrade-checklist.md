# Evennia Upgrade Checklist

Things to verify when upgrading the Evennia dependency. Each item explains **what** to check, **why** it matters, and **where** the coupling lives in library code.

## WebSocketClient.onOpen() override

**File to diff:** `evennia/server/portal/webclient.py` → `WebSocketClient.onOpen()`
**Library code:** `evennia_shards/protocols.py` → `ShardWebSocketClient.onOpen()`
**Based on:** Evennia 6.0.0

**Why we override it:**

Ticket-based auth needs `self.uid` and `self.logged_in` set on the session *after* `init_session()` (which resets both to `None`/`False`) but *before* `sessionhandler.connect()` (which snapshots session state via `get_sync_data()` and sends it to the Server over AMP). The Server's `portal_connect()` auto-logins sessions that arrive with `logged_in=True` and a valid `uid`.

There is no method-level seam between `init_session()` and `sessionhandler.connect()` — both are called inline in `onOpen()`. We explored three alternatives and ruled them out:

- **Swapping call order** (ticket validation before `super().onOpen()`): `init_session()` wipes `uid`/`logged_in`, and `self.address` isn't set yet for IP validation.
- **Post-connect re-sync** (`sessionhandler.sync()`): the Portal's `sync()` deliberately excludes `uid` and `logged_in` from the data it sends to the Server. There is no Portal→Server "please login this session" AMP operation.
- **Overriding `get_sync_data()`**: would work mechanically but splits the auth logic across two methods with non-obvious interaction, and makes a data-serialisation method responsible for auth decisions.

Overriding `onOpen()` is the only clean approach. Our override reproduces the parent method and adds a ticket-auth check in the same position as the existing browser-session auth check (between `init_session()` and `sessionhandler.connect()`).

**What to look for in new Evennia versions:**

- New logic added between `init_session()` and `sessionhandler.connect()` — our override would miss it.
- Changes to the `init_session()` / `get_client_session()` / `sessionhandler.connect()` call sequence.
- New protocol flags or connection setup steps added to `onOpen()`.
- Changes to `SESSION_SYNC_ATTRS` that affect what `get_sync_data()` sends.

**How to check:** Diff the upstream `onOpen()` against the snapshot in our override. The override should carry a comment citing the Evennia version it was based on.
