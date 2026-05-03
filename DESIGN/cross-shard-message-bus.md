# Cross-shard message bus

How shards send messages to each other. This document captures decisions reached on 2026-04-29 about the bus's transport, lifecycle, and extension model.

## Transport: Postgres `messages` table

The bus is a `messages` table in the shared Postgres database. To send: insert a row. To receive: poll for rows addressed to your shard, process, delete.

**Why Postgres rather than Redis / Channels:**

- **No new infrastructure.** Postgres is already a hard dependency. Removing Redis from the picture means one less ops surface, one less local-dev requirement, one less production failure mode.
- **Durability across brief outages.** Messages are real DB rows, so a recipient process that's briefly offline (restart, transient error) doesn't lose them — they get picked up on the next poll after recovery. This is *process-level* durability, bounded by the lifecycle timeout below; it is **not** persistent storage for messages to offline players (see "What this bus is not").
- **Transactional integrity.** A message insert commits with the sender's surrounding transaction. If that transaction rolls back, no message exists. No "stale handoff signal because the save rolled back" failure mode.
- **Naturally Django-shaped.** The send/receive flow is just `Message.objects.create(...)` and `Message.objects.filter(...).delete()` — no new mental model for consumers or maintainers.

## What this bus is not

This is a **real-time messaging bus between shard processes**. It is not a persistent player-facing message store.

Concretely:

- A message that cannot be delivered within the lifecycle timeout (~10s) is dropped, with an `undeliverable_reply` returned to the sender. It does not sit in the table waiting for a recipient that may come back later.
- If a player tries to tell a character who is not currently in the game, the bus surfaces "could not be delivered" via the undeliverable_reply path. The sender sees a "character not in game" notification (the exact wording is consumer UX, not bus mechanics). The tell does not queue for delivery whenever-the-character-next-logs-in.
- Persistent player-facing messaging (in-game mail, "leave a note for character X," etc.) is **out of scope** for this primitive. If a consumer game wants that, it is a separate concept built on its own storage — a "post office" model the consumer ships, not something the bus provides.

This scope is deliberate: extending the bus into persistent player-messaging territory would change its operational properties (table grows unboundedly, processed-flag/cleanup machinery returns, mail-like UX expectations creep in) and change what the library is. If we ever decide to add persistent messaging, we would design it as a separate primitive at that time.

The pattern (Postgres-as-message-bus / outbox table) has prior art (`pgmq`, `procrastinate`, the general "outbox pattern" in microservices literature). We're applying a known technique, not blazing trail.

## Schema

Minimum columns:

- `id` — primary key.
- `created_at` — when the row was inserted.
- `to_shard` — recipient shard identifier.
- `from_shard` — sender shard identifier (for replies).
- `kind` — discriminator string (e.g. `character_handoff`, `undeliverable_reply`). Recipient routes on this.
- `payload` — JSONB. Free-form data for the specific kind.

Index on `(to_shard, created_at)` for the recipient's poll query. Plain index, not partial — at steady state the table is small because messages are deleted on process.

## Wakeup mechanism: polling

Each shard polls the `messages` table on a fixed cadence (baseline: every 0.5s) for messages addressed to itself.

**Why polling rather than `LISTEN`/`NOTIFY`:**

- **Simpler.** No socket-level integration with Twisted's reactor; no edge cases around connection drops, reconnects, or missed notifications.
- **Self-healing.** A missed wakeup signal is impossible because there are no signals — every poll catches up.
- **Predictable.** You can see it happen, throttle it, stop it for debugging.
- **Negligible load at our scale.** 2 queries/sec/shard against an indexed column on a steady-state-small table is sub-millisecond per query. Even with 100 shards, we're at 200 queries/sec — well within Postgres's capacity for trivial queries.

**Latency cost:** worst case 0.5s, average 0.25s. Acceptable for a MUD's message volume; well below the round-trip jitter players experience anyway.

### Where the polling loop lives

The polling loop is registered as a Twisted `LoopingCall` on the Server process's reactor — *not* as an Evennia `Script`. Rationale:

- Evennia `Scripts` carry DB-backed state (started / paused / stopped) and have a known operational failure mode where they can wedge in "stopped, needs admin restart" state. A foundational comms primitive cannot tolerate that failure mode.
- `LoopingCall` is the Twisted primitive that `Script` is built on top of. Using it directly skips the script bookkeeping, has no DB-backed "is it running" state, and shares fate with the reactor — if the loop is dead, Evennia is dead, and the failure is one visible problem rather than an asymmetric "bus is offline but the game looks fine" footgun.
- No new deployment surface. The loop lives in the Server process, alongside the rest of the game runtime.

The `LoopingCall` is started from the consumer's `at_server_start()` hook (typically in `server/conf/at_server_startstop.py`), via a one-line call to a library-provided startup function. This is the second piece of consumer-facing API the library exposes (after `get_role` / `get_shard_id`).

### LISTEN/NOTIFY as a future investigation

[**TBD task** — investigate `LISTEN`/`NOTIFY` as a wakeup *optimisation* layered on top of polling (poll for correctness, notify for latency). Concrete questions: how cleanly does a long-lived `LISTEN`-active Postgres connection integrate with Twisted's reactor? What's the reconnect story? Is the implementation effort small enough to justify the latency improvement? Unless something jumps out as a particularly strong candidate, the current lean is to ship with `LoopingCall` polling alone — it's adequate at our scale and removes a class of failure modes. LISTEN/NOTIFY can be added later if a real use case demands sub-250ms latency.]

## Message lifecycle

The recipient *processes* each message — what processing means depends on the message's `kind`. Examples: instantiating a character that's just been handed off into a target room; delivering a player-to-player tell; running a custom consumer-defined action. The lifecycle below is generic to all kinds; specific kinds plug into the dispatch via their registered handlers (see Extensibility).

1. **Send:** insert a row.
2. **Recipient polls:** queries for rows where `to_shard = self.shard_id`, ordered by `created_at`.
3. **Recipient attempts to process** each fetched message by routing on `kind` to the registered handler.
4. **On success:** delete the row in the same transaction as the handler's side-effects.
5. **On defer** (the handler cannot complete yet — e.g. the target character of a tell is mid-handoff and not yet present): leave the row in place. It will be retried on the next poll.
6. **On timeout** (`now - created_at > <timeout>` and the handler still cannot complete): delete the original message; insert a new `undeliverable_reply` message addressed to the original sender. Sender (or sender's app logic) decides whether to retry, give up, or surface the failure to the user.

### Timeout is configurable

The timeout is configurable, with two settings working together:

1. **A global default** — `SHARDS_MESSAGE_TIMEOUT_DEFAULT`. The library defaults this to **10 seconds**. Applies to any message kind that doesn't have a more specific timeout declared.
2. **A per-kind override map** — `SHARDS_MESSAGE_TIMEOUTS`, a dict mapping `kind` → timeout in seconds. Looked up first; the global default applies for any kind not present.

Example consumer settings:

```python
SHARDS_MESSAGE_TIMEOUT_DEFAULT = 10
SHARDS_MESSAGE_TIMEOUTS = {
    "character_handoff": 30,    # cross-shard handoff legitimately takes longer
    "tell": 5,                  # tells fail fast if the target isn't here
}
```

Both settings are read through a library accessor (`get_message_timeout(kind: str) -> int`) following the same pattern as `get_role()` / `get_shard_id()`. The accessor handles the resolution: per-kind map first, then the global default.

The override map applies equally to library-shipped kinds and consumer-defined kinds — the library doesn't get a privileged channel. A consumer wanting `character_handoff` to time out at 60 seconds simply sets that key in `SHARDS_MESSAGE_TIMEOUTS`; no code modification needed.

[**TBD** — whether the library should ship recommended per-kind defaults for *its own* kinds (e.g. `character_handoff: 30` baked into the library's resolution logic so it's used unless explicitly overridden in `SHARDS_MESSAGE_TIMEOUTS`), or whether all kinds — library's and consumers' — fall through to the global default unless the consumer explicitly overrides. The first gives better out-of-the-box behaviour at the cost of one resolution layer; the second is simpler but pushes configuration burden onto every consumer who uses library-shipped kinds with non-default needs.]

The `undeliverable_reply` is itself a message in the same system — the bus is recursive but still uses one primitive.

**No row-level locking.** Each message has a single addressee (`to_shard`); each shard is one process. No contention. `SELECT ... FOR UPDATE SKIP LOCKED` defensiveness pays off only with multiple worker processes pulling from the same queue, which we don't have.

**Delete-on-process, not flag-and-keep.** Messages are transient communication, not long-term data. Deleting keeps the table at steady-state size automatically — no separate cleanup job, no audit-trail growth. Postgres autovacuum handles dead-tuple reclamation as a normal operational pattern.

## Multicast: sender-side fan-out

The bus has no multicast primitive. To send a message to multiple shards: the sender inserts one row per recipient, in a single transaction. At our scale (typically <10 shards), 10 inserts is sub-millisecond.

Reasons fan-out is the right answer here:

- Per-recipient defer/timeout/undeliverable semantics work unchanged — no special multicast lifecycle to design.
- Per-recipient "did it land?" feedback is natural — if shard 7's message comes back undeliverable, the sender knows specifically which one didn't get it.
- No new code path; no library special case for "this is multicast." Consumers compose `send_message` in a loop.

This is library hygiene: we don't ship a primitive for a pattern that doesn't need one.

## Extensibility: `kind` + JSONB

Once the bus exists, adding a new message type is data, not code:

- Pick a new `kind` string.
- Define the JSONB payload shape.
- Register a handler on the recipient side that knows how to interpret that kind.

No schema migrations, no library release, no new table.

**Library-shipped message kinds:**

- `ping` / `ping_received` — diagnostic round-trip. Sender posts `ping`; the receiver replies with `ping_received` to the original `from_shard`, echoing the payload. Useful for operational health checks and end-to-end smoke testing.
- `undeliverable_reply` — the failure mode of the bus itself. Inserted automatically by `process_inbox` when an outbound message ages past its kind-specific timeout. Payload carries `original_kind`, `original_payload`, `reason`. Consumed silently in the base handler; consumers override to surface delivery failures.
- `obj_msg` — deliver a player-facing message to an `ObjectDB` row on the receiving shard. Payload: `{"pk": <int>, "kwargs": <dict>}`. Receiver does `ObjectDB.objects.get(pk=...)` and calls `obj.msg(**kwargs)`; Evennia's own `Object.msg` then handles local session fanout (covering `MULTISESSION_MODE 2/3`). Covers IC delivery: room broadcast targeting a remote character, channel msg targeting a body, ambient effects on remote NPCs. Mechanically generic — works for any puppetable `ObjectDB` (characters, vehicles, possessed objects), with the typeclass policy left to consumer-side helpers.
- `account_msg` — same shape, targeting an `AccountDB` row. Used for OOC delivery: tells/pages, system messages, account-level channel msgs. Receiver looks up the account and calls `account.msg(**kwargs)`. Required for messaging brand-new or perpetually-OOC players who have no character yet.

For `obj_msg` and `account_msg`, target-gone (DoesNotExist) is consumed silently with a warning log — the bus is real-time only, deferring won't bring a deleted target back. Misroute (target row owned by a different shard) trips the `from_db` chokepoint, raises `ShardIsolationError`, gets caught by `process_inbox` as defer, and eventually triggers `undeliverable_reply` to the sender — loud failure on misroute by construction.

The primitives splat their `kwargs` directly into `target.msg(**kwargs)`, which means kwargs must be JSON-serialisable (the `payload` JSONField enforces this at send time). Notably, `from_obj=` (a common `Object.msg` kwarg pointing at an `ObjectDB` instance) is not serialisable and not constructible cross-shard. Sender-side helpers (a separate layer, not yet shipped) will be responsible for rendering text on the sender side and dropping `from_obj` before constructing the payload.

**Sender-side helpers (deferred):** `send_cross_shard_message` and any specialised wrappers (cross-shard tells, channel propagation, room broadcast) build on top of the primitives once they are proven. The primitives are sender-API-agnostic — consumers can drive them directly via `send_message(kind="obj_msg", payload=..., to_shard=...)` until the helper layer lands.

**Consumer-defined kinds:** anything game-specific the consumer wants to layer on. The `obj_msg` / `account_msg` primitives cover the common "deliver text to a row" case; consumers add their own kinds for game-specific cross-shard signals (combat events, world-state propagation, etc.).

## TBDs

**Handler registry shape (decided):** a base class `MessageHandler` whose `handle(message) -> bool` method dispatches by `message.kind`. Library-shipped kinds (`ping`, `ping_received`, `undeliverable_reply`) are handled in the base. Consumers extend by subclassing and calling `super().handle(message)` before adding their own kind dispatch — single override point, OO composition via super-call. The polling cycle is wired through `start_message_bus(handler, interval)`, which registers a Twisted `LoopingCall` around `process_inbox(handler)`.

[**TBD** — payload schema evolution: when a `kind`'s payload shape changes (fields added or removed), a message inserted by older code might be processed by newer code (or vice versa) during rolling deploys. Standard solutions apply (versioned payloads, additive-only fields, ignore-unknown-keys), but we haven't picked a convention. Not a blocker for v1; worth deciding before we ship.]
