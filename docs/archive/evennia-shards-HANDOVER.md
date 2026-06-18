# Evennia Split-Deployment Library — Project Handover

## Purpose of this document

This document captures the full design context, decisions, and rationale for a new standalone open-source library that extends Evennia with optional split deployment and sharding. It is intended as the seed context for LLM sessions working in the new repository — the project has not yet been started; this is the design substrate it will be built on.

The design originated from scaling work for the FullCircleMUD (FCM) project but is deliberately being broken out as a separate, game-agnostic library. The originating design documents (FCM's `design/SCALING.md` and `design/WORLD_DEPLOYMENT.md`) contain additional FCM-specific context that informed but does not bind this library.

---

## Project identity and positioning

**What this is:** a drop-in extension to Evennia that adds optional split deployment and horizontal sharding via configuration alone. Install it, the game runs as vanilla Evennia. Flip a config setting, the same code runs as a split deployment (auth process separate from game process). Flip another, it runs as full multi-shard.

**Positioning statement:** *"Making split deployment a config option in Evennia."*

This framing is deliberately narrower than "scaling Evennia." It positions the library as a small, additive enhancement rather than a parallel ecosystem fork. Lower adoption barrier, easier to explain, less perceived risk for prospective users.

**What this is NOT:**

- Not a fork of Evennia.
- Not a parallel Evennia distribution.
- Not a "rewrite your stack to scale" project.
- Not a multi-region / multi-database / multi-datacenter design.
- Not a solution for "what if we had millions of players" — those concerns are deferred to a hypothetical future where revenue funds dedicated infrastructure expertise.

**What this library bounds itself to:** the **single-Postgres era**. From one Evennia process today through however many shards run against a single (vertically scaled) Postgres. That is the entire design surface. If a consumer ever exhausts that, they are at a revenue scale where dedicated infrastructure work is affordable, and the design beyond that point is explicitly deferred.

---

## Origin: why this is a separate project

The library was conceived during scaling design work for FCM, a MUD built on Evennia. As the architecture took shape, it became clear the router/shard pattern was genuinely game-agnostic — nothing about it cared about FCM's economy, NFTs, gold, or any other game-specific concern. Building it as an FCM module would mean either:

- Bloating FCM with infrastructure code unrelated to the game itself, or
- Smuggling FCM-specific assumptions (zone naming, tag conventions, economy state) into general-purpose code where they would compromise reusability.

Neither is desirable. The decision to break it out as a standalone library:

- Enforces clean abstraction discipline. Anything FCM-specific creeping into the library is a code smell.
- Produces an artifact with independent value, regardless of FCM's outcome.
- Aligns with Evennia's culture of community contrib packages.
- Keeps FCM focused on its game.

FCM will adopt this library as a dependency once it is functional.

---

## Survey of prior art (conducted 2026-04-28)

A thorough search of the Evennia ecosystem found **no prior work** on horizontal scaling or sharding. Specifically:

- Zero relevant entries in the official `evennia.contrib` library (53 contribs total).
- No third-party projects on GitHub or GitLab matching searches for `evennia` + `shard`/`scale`/`scaling`/`cluster`/`multi-instance`/`router`/`horizontal`.
- The single historical artifact, `evennia/procpool`, has been abandoned since 2015 and was solving an adjacent problem (offloading blocking work via `ampoule`), not sharding.
- Sharding is rarely discussed in the Evennia community. The recurring scaling questions are vertical (memory, concurrent capacity, blocking calls) and the standard answers are design patterns (procedural rooms) or "run a bigger box."
- Lead maintainer Griatch's architectural choices — the `SharedMemoryModel` idmapper, moving the webserver into the Server process (away from Portal) — explicitly assume single-Server. The Portal-and-Server doc states they "always run on the same machine."
- Multi-instance is **tacitly out of scope, not formally rejected.** No design hooks, no roadmap item, no RFC, but also no explicit refusal.

**Implication:** this project is genuinely first-mover space. There is nothing to adopt, fork, or contribute to. The architecture must live entirely outside Evennia core and treat single-Server assumptions as constraints to work around, not change.

The demand signal from the community is weak — nobody is waiting for this library. Build it because consumers (starting with FCM) need it; treat any community uptake as a bonus rather than the success criterion.

---

## Core architecture: the three-mode design

A configuration setting (`SHARDS_ROLE` or equivalent) per Evennia process determines its behavior:

### Mode: `monolith` (default)

Single process does everything. Library is essentially dormant:

- No redirect protocol registered.
- No `@ic` / `@ooc` overrides registered.
- No ticket system active.
- `ShardGatewayMixin`-decorated rooms behave as normal rooms (gateway traversal is just teleport).

A game dev who installs the library and changes nothing else gets vanilla Evennia. The library is opt-in to all its capabilities.

### Mode: `router`

Process is the auth front door:

- Listens on the public game port.
- Owns `AccountDB`.
- Handles login dialogue (vanilla Evennia code).
- Shows the OOC/character menu (vanilla Evennia code).
- `@ic` overridden: instead of puppeting locally, issues a one-time ticket and tells the web client to reconnect to a shard URL.
- Does **not** load game world, scripts, or character typeclasses for play.

### Mode: `shard`

Process is a game world:

- Listens on an internal port (or a port behind external auth controls).
- Login dialogue **disabled** — only ticket-based session attach is permitted.
- Loads the zones it owns; runs spawn scripts, world content, etc.
- `@ooc` overridden: instead of unpuppeting locally, tells the web client to reconnect to the router URL.

### Mode validation

- `monolith` is the default in any process where `SHARDS_ROLE` is unset.
- `shard_count > 1` requires `router` + multiple `shard` processes. The library refuses to start a `monolith` process if `shard_count > 1` is configured globally.
- `router + shard_count = 1` is valid (split deployment with one shard) — useful for proving the split mechanic without committing to multi-shard.

### Branching strategy: registration-time, not per-call

The library should branch on mode at **command-registration / mixin-activation time**, not inside every overridden method. In `monolith` mode, the library's overrides are never registered — Evennia's stock commands stay in place, untouched. In `router`/`shard` mode, the library registers its versions, which always do the redirect path.

This produces a strong "the library isn't there" guarantee in monolith mode, rather than a weak "the library is there but dormant." Easier to reason about, no runtime branching overhead in the hot path.

The same pattern applies to:

- `ShardGatewayMixin.at_traverse` — only triggers handoff logic in non-monolith modes.
- Session-attach handler — only registered on shards.
- Ticket-issuing logic — only registered on routers.

---

## The split-deployment model

The router and shard are separate Evennia processes against a shared database. They differ only in which code is active (per the mode branching above).

### How a player gets from auth to game

The choice of session-handoff mechanism between router and shard is the load-bearing architectural decision. Three options were considered:

**Option A: Backing service (out-of-band auth).** Router is an HTTP/RPC service that shards consult for auth. Players connect directly to a shard's Portal; the shard runs the login dialogue and RPCs the router to validate credentials.

- **Rejected because:** the "split" is just data ownership, not session ownership. Every shard runs its own login flow. Doesn't truly test or exercise session relocation, which is exactly what the library is meant to abstract.

**Option B: In-path proxy.** Router holds the TCP/websocket connection and forwards bytes to whichever shard owns the player's character. Player sees one continuous session.

- **Rejected because:** router is in the gameplay hot path forever. Switching backend mid-session (for cross-shard travel later) requires multiplexing multiple Server processes behind one Portal — explicitly out of scope per FCM's `SCALING.md`. Adopting the proxy model commits to a Phase 2 redesign.

**Option C: Ticket-based redirect (chosen).** Router authenticates, shows OOC menu. On `@ic`, router issues a one-time ticket (DB-backed, short TTL), sends the web client a `RECONNECT_TO {url, ticket}` message, and closes the session. Web client opens a fresh connection to the shard, presents the ticket, shard validates and attaches the session to the named character.

- **Why this won:** the same primitive (`RECONNECT_TO {url, ticket}`) handles every session-relocation event in the system — first login, cross-shard travel, future "graceful restart" features. Router stays out of the gameplay path entirely. Composes naturally with the disconnect-and-reconnect model already chosen for cross-shard handoff.
- **Cost:** a brief reconnect on first login. Web clients (auto-reconnect) hide it; raw telnet sees it. Library targets web-first; telnet is deferred.

### The redirect protocol

A custom websocket message from server to client:

```
RECONNECT_TO {
    "url": "wss://shard0.example.com/ws",
    "ticket": "TKT-7f3a..."
}
```

The web client handler:

1. Receives the message.
2. Closes the current websocket.
3. Opens a new websocket to the target URL.
4. Presents the ticket in the connection handshake (header, query string, or initial message — exact mechanism is a design point).
5. The receiving shard validates the ticket against the shared DB, marks it consumed, and attaches the session to the associated character.

This is roughly 30 lines of new code on the web client side and a small handler on each shard. Telnet equivalent is a printed *"please reconnect to host:port with this ticket"* message; deferred from PoC.

### The ticket system

- One Django model: `(token, character_id, expires_at, used_at)`.
- DB-agnostic — works on whatever the game uses (SQLite or Postgres).
- Single-use, short TTL (seconds, not minutes).
- Issued by router on `@ic`, by shards on `@ooc` (and on cross-shard travel in multi-shard mode).
- Validated on session attach.

Tickets live in the same database as the game. **Do not require Redis just for tickets.** Redis becomes a multi-shard-only dependency for channel bridging and cross-shard RPC.

---

## ShardGatewayMixin: the boundary primitive

In multi-shard mode, the library needs to identify which rooms are cross-shard transition points. Rather than introducing a new typeclass that would force consumers to inherit from a library-provided base, the library provides a **mixin** that any room typeclass can compose in.

```python
# In a consumer game (e.g., FCM):
class PortalRoom(ShardGatewayMixin, MyGame.BaseRoom):
    pass
```

### What the mixin owns

- A `destinations` field (list of composite triples — see "destinations and shard resolution" below).
- An override of the appropriate Evennia traversal hook (`at_traverse` or equivalent) that detects whether the destination is local or cross-shard. In monolith mode, it falls through to default movement. In split mode with a cross-shard destination, it triggers the handoff lifecycle.
- A default "am I safe to move?" predicate, with hooks for the consumer to override.
- Default narrative messaging (configurable via class attributes the consumer can override).

### What the consuming game owns

- The room typeclass that mixes it in.
- Population of `destinations` at world-build time.
- Anything game-specific that happens at gateway traversal (custom narrative, gateway-tied NPC encounters, travel costs).
- The mapping from zone identifier to shard id.

### Why a mixin and not a typeclass

Most Evennia game devs already have custom Room typeclasses with their own behavior (weather, rest mechanics, ambient descriptions). Forcing them to inherit from a library-provided `Room` would be hostile. A mixin is composable — they keep their hierarchy and add boundary semantics.

### Naming

Working name: `ShardGatewayMixin`. (Earlier draft was `ShardGatewayRoomMixin`; "Room" is redundant since the docstring clarifies it's for room typeclasses.)

### Narrative framing matters

These rooms are intended to feel like portals — a trailhead, a dock, a passage, a beat in the player's journey — *not* like standard exit rooms. The brief reconnect (visible to telnet players, invisible to web clients) is acceptable precisely because the room is narratively distinct from regular movement. The library's documentation should make this explicit: gateways are a UX concept, not just a technical seam.

---

## Cross-shard handoff lifecycle

When a player traverses a `ShardGatewayMixin` room whose destination is on another shard:

1. **Quiesce on source shard.** Verify the character is in safe state (not in combat, not casting, no in-flight delayed callbacks). The "safe to move?" predicate is consumer-overridable. Gateway rooms are designed to be safe rooms by convention.
2. **Persist non-DB state.** Drain `ndb` (preferred — most ndb keys are session-local UI state that re-derives) or serialize a small allowlist of keys.
3. **Update the character's location.** The character's `db_location` is set to the destination room (which is on the target shard). **This is the linearisation point of the handoff** — after this Postgres write, the target shard owns the character.
4. **Evict from source idmapper.** Remove the character (and carried inventory rows, followers) from the source shard's idmapper and `AttributeHandler` caches.
5. **Issue ticket and redirect.** Send `RECONNECT_TO {target_shard_url, ticket}` to the web client. Source shard closes the session.
6. **Load on target shard.** Web client reconnects with ticket. Target shard validates, attaches the session, loads the character into its idmapper.

This is the same protocol as initial login, just triggered by gateway traversal instead of `@ic`.

### The cache invariant

> **If a character/object is not resident on your shard, you do not cache it.**

This rule is what makes the design safe across shards. Evennia's `SharedMemoryModel` and `AttributeHandler._cache` are per-process with no cross-process invalidation. The handoff protocol enforces the invariant: a character is evicted from the source's caches before the target's caches load it.

The router's caches are equally bounded: it loads `AccountDB` rows but never instantiates `ObjectDB` rows for play. For the OOC menu, it does plain Django queries (`.values()`) that return dicts, never typeclass instances — keeping the idmapper clean by construction, not by discipline.

---

## Character→shard derivation

When one shard needs to know which shard a particular character currently lives on (for cross-shard tells, who, scry, mail-arrival routing), it derives the answer from the shared database rather than consulting a router-side registry or a cached attribute.

### The derivation

1. Look up the character's row by name (or id).
2. Read `db_location` (the room they're in).
3. Read the room's `zone` tag.
4. Map zone → shard via a Python constant loaded at process start (`ZONE_SHARD = {"zone_a": 0, "zone_b": 1, ...}`).

### Why this works

- The lookup is a single Django ORM query against shared Postgres, with a join. Cheap.
- Use `.values()` so the query returns a dict, not a model instance — no idmapper involvement, no cache invariant concern.
- The zone→shard mapping doesn't change during a deployment. It only changes during reshuffles, which are planned downtime events. So the in-process mapping is immutable for the life of the process; no invalidation protocol needed.
- No separate `current_shard` attribute on the character to keep in sync with reality. The character's location *is* the source of truth.

### Why this is better than the alternatives we considered

A `current_shard` attribute (or a router-side character→shard registry) was the original design. The location-derivation approach replaces both with a property that falls out of existing data:

- No new field to maintain.
- No write-on-every-zone-cross to keep `current_shard` consistent.
- No router-side state that could drift from reality.
- The handoff's linearisation point becomes the `db_location` write itself, which is a stronger guarantee — the field that changes *is* the field that defines ownership.

### Edge cases

The window between "source writes db_location to the target room" and "target shard has the character loaded into idmapper" is brief (single Postgres write + Portal reconnect). During that window, a tell from a third party will resolve the target as "on shard B" but shard B doesn't have them in cache yet. Bounce the message with *"player is in transit, try again."* This is acceptable UX and keeps the cache invariant clean — a character lives in exactly one shard's idmapper, never two, never zero-but-being-fetched.

This window is small enough that detailed protocol design can be deferred to implementation; flag it but don't pre-solve.

---

## Gateway destinations and shard stamping

`ShardGatewayMixin` rooms hold a `destinations` list. The shape is decided in conjunction with the zone→shard mapping strategy.

**Chosen approach: stamp at build time.**

`destinations` entries are 4-tuples: `(zone, district, key, shard)`. When the consumer game builds its world (e.g., FCM's `build_world_base()`), it resolves shard from its `ZONE_SHARD` constant and writes the resolved shard id directly onto the gateway row.

**Runtime lookup is purely local.** A traversal hook reads `destinations`, finds the target tuple, and immediately knows whether the destination is on this shard or another. No process-level config dictionary consulted at traversal time.

To rebalance shards (a planned-downtime event), edit the source-of-truth zone→shard mapping, run `build_world_base()` on every shard, and the new shard ids are stamped onto every gateway row.

**Source of truth: Python constant in the consumer game.** The library does not provide a runtime registry or JSON config layer — that is left to the consumer. FCM's pattern (per its `WORLD_DEPLOYMENT.md`) is hard-coded branches inside `build_world_base()` reading a `SHARD_ID` env var. The library accommodates this without prescribing it.

### Only gateways need shard data on them

Regular rooms do not need a `shard` tag. The shard a room lives on is implied by which shard's `build_world_base()` / `build_zone()` instantiated it — i.e., by which `ACTIVE_ZONES` includes it. The only rows that carry shard data are the **outbound destinations on gateway rooms**, because they point across a boundary.

---

## Cross-shard messaging (multi-shard only)

Three classes of cross-shard message:

### Channels

Evennia's `ChannelDB` is a database-backed registry but message broadcast is in-process: when shard A's `Channel.msg()` fires, only sessions on shard A receive it. To bridge:

1. Each shard subscribes to a Redis topic per channel on startup.
2. On local broadcast, also publish to the topic.
3. On receiving a published message from another shard, deliver to local subscribers without re-publishing (loop break via shard-id stamp).

### Tells, who, scry

RPC by character→shard derivation. Sender's shard derives the target's current shard (via the location-derivation pattern above), RPCs the target shard with the message. Single Redis-backed message channel per shard is sufficient transport.

### Mail

Already shard-agnostic. Mail items live in DB rows; recipient's shard reads them at delivery time (e.g., when player visits a post office). No bridging needed.

### Implementation note

Redis is the **multi-shard-only** dependency. Single-shard split deployments do not need it (no other shard to bridge to). The library should make the Redis configuration optional and unused when `shard_count == 1`.

---

## Database support

### SQLite (single-writer)

- **Monolith mode:** fully supported. SQLite WAL handles single-writer-per-file fine.
- **Single-shard split mode (`router + 1 shard`):** supported with caveats. Two processes write to the same SQLite file: the shard writes gameplay state, the router writes AccountDB updates and tickets. Both are low-volume on the router side; SQLite WAL serializes writers but they shouldn't contend much in practice.
- **Multi-shard mode (`shard_count > 1`):** **not supported.** SQLite's single-writer constraint serializes the parallelism multi-shard exists to provide. The library should refuse-or-warn at startup if `SHARD_COUNT > 1` and SQLite is detected.

### Postgres

- Required for multi-shard production.
- Handles `SELECT FOR UPDATE` and proper MVCC.
- Standard Django ORM is enough for everything the library does.

### Implementation

Use Django ORM throughout. Avoid Postgres-only features in core library paths. Ticket model is one Django model, DB-agnostic. The boundary is honest: *"the database you're already using stays the database you use, until you flip `shard_count` above 1."*

---

## Deployment shapes

### Local development

Two terminals when in split mode:

```
DJANGO_SETTINGS_MODULE=server.conf.settings_router evennia start
DJANGO_SETTINGS_MODULE=server.conf.settings_shard0 evennia start
```

Both processes share one DB (SQLite file or Postgres URL). Web client connects to router's port; gets redirected to shard's port on `@ic`.

The library provides role base-settings modules to keep game-side settings small:

```python
# settings_router.py
from evennia_shards.role.router import *   # placeholder name
PORT = 4000
SHARD_MAP = {0: "ws://localhost:4010/ws"}
```

```python
# settings_shard0.py
from evennia_shards.role.shard import *
PORT = 4010
SHARD_ID = 0
ROUTER_URL = "ws://localhost:4000/ws"
```

### Railway production (FCM example)

- **Monolith:** 1×Evennia + 1×Postgres.
- **Single-shard split:** 2×Evennia (router + shard0) + 1×Postgres. Both Evennia services publicly addressable.
- **Multi-shard:** N+1×Evennia (router + N shards) + 1×Postgres + 1×Redis.

Both Evennia services are publicly addressable in split mode because the web client redirects directly to the shard URL after auth — the router does not stay in the path. Security on shards relies on **ticket validation**: a connection without a valid one-time ticket gets bounced.

A reverse proxy fronting all services under a single hostname is a future polish, not a PoC requirement.

### Single-process "monolith for dev" mode

Considered and rejected. Running both router and shard roles in one process for dev convenience would diverge from production behavior, hide bugs in the redirect mechanism (the most novel part of the library), and add code paths nobody runs in production. Two terminals is fine.

---

## Phased PoC plan

### Phase 1: Router + 1 shard, no actual sharding

The smallest configuration that proves the structural split works:

- Router process on its own port. Vanilla Evennia auth + OOC menu. `@ic` overridden to issue ticket and redirect.
- Shard process on its own port. Login disabled; ticket-based session attach only. `@ooc` overridden to redirect back to router. Loads a small game world.
- Shared SQLite or Postgres.
- Web client extended to handle `RECONNECT_TO` messages.
- Demonstrates: a player connects to router, logs in, picks a character, gets seamlessly handed off to the shard, plays, types `@ooc`, lands back at the router's character menu.

Out of Phase 1: cross-shard travel, a second shard, channel bridging, per-shard SINK, telnet support.

### Phase 2: Real sharding

Add a second shard:

- Static zone-to-shard mapping in config.
- One zone on shard A, one on shard B, connected by exactly one gateway pair.
- Cache invariant enforced (shard A loads no rows owned by shard B's zones).
- One handoff path tested end-to-end: walk through the gateway, brief reconnect, arrive on the other shard with inventory intact.
- `ShardGatewayMixin` exercised in real cross-shard mode.

### Phase 3: Cross-shard messaging

- Redis pub/sub bus.
- Channel bridging: `say` (channel) works across shards.
- Cross-shard tells: `tell <player on B>` from shard A delivers correctly.

### Out of PoC entirely

- Telnet support (deferred; web-first project).
- Cross-shard combat, party mechanics, follower trains.
- Dynamic zone reassignment.
- Automated rebalancing.
- Live (non-reconnect) session migration.
- Reverse-proxy single-domain deployment.

---

## Bit-rot risk and mitigation

A consequence of the three-mode design: if a consumer game runs `monolith` in production indefinitely, the split-mode and multi-shard code paths never fire in production. They could rot — bugs introduced by upstream Evennia changes, library refactoring, or untested edge cases would not be caught.

**Mitigation:** the consumer game runs **staging permanently in split mode** (router + shard0, possibly + shard1), even when production is monolith. The redirect/ticket/handoff paths stay exercised continuously. Flipping production from monolith to split then becomes low-risk because the paths have been live somewhere for months.

For the library itself, this argues for:

- A robust integration test suite that spins up router + shard processes and exercises full session flows.
- Example deployments (docker-compose? makefile target?) that run split mode by default for development.

---

## Things explicitly out of scope

These were considered and deliberately ruled out, either because they are deferred to a post-single-Postgres era or because they conflict with the design's core simplicity:

- **Multi-Postgres / sharded database.** Single Postgres is the bound.
- **Read replicas / read-write splitting.**
- **Cross-region or multi-datacenter deployment.**
- **Geographic distribution / latency-driven sharding.**
- **Live session migration mid-combat / mid-spell / mid-script.** Handoffs are gated by safe state.
- **Dynamic load-aware zone reassignment.**
- **Cross-shard combat, party mechanics across shards, follower trains across shards.**
- **Modifying Evennia's Portal to multiplex across multiple Server processes.** Explicitly out of scope per FCM's `SCALING.md`.
- **Telnet support in the redirect protocol.** Web-first project; telnet is a future addition (likely via a TCP-level proxy or "please reconnect" prompts).
- **Automatic rollback of failed handoffs.** The `db_location` write is the linearisation point; if anything fails after that, target shard owns the character and the player reconnects to it.
- **A registry / config service for runtime zone-to-shard mapping.** The mapping lives as a Python constant in source control. Reshuffles are planned downtime events with a runbook, not hot operations.

---

## Open decisions (to be resolved during project setup)

1. **Project name.** Working candidates: `evennia-shards`, `evennia-multi`, `evennia-split`. No commitment yet.
2. **Public vs private at start.** Leaning private until PoC works; open later.
3. **Repo location.** New GitHub org, personal account, or under a FullCircleMUD org. Affects perceived independence.
4. **License.** Likely BSD-3 (matches Evennia) or MIT.
5. **Distribution form.** PyPI package (`pip install <name>`) is the natural target — stays a healthy dependency rather than a fork or patch.

---

## Glossary

| Term | Meaning |
|------|---------|
| **Mode** | One of `monolith`, `router`, `shard`. Set per Evennia process via config. |
| **Monolith** | Default mode. Single process does everything. Library is dormant. |
| **Router** | Process role: auth front door. Owns AccountDB. Public-facing. |
| **Shard** | Process role: game world. Holds zones, characters in those zones, world content. Ticket-based session attach. |
| **`shard_count`** | Number of shard processes in a deployment. 1 = single-shard split, >1 = multi-shard. |
| **Gateway / Gateway room** | A room marked with `ShardGatewayMixin`. Functions as a shard-boundary crossing point. Narratively a portal/trailhead/dock. |
| **Ticket** | One-time, short-TTL credential used to attach a web client session to a shard after redirect. |
| **Redirect** | The `RECONNECT_TO` mechanism: server tells web client to close current connection and open a new one to a different URL with a ticket. |
| **Handoff** | Full lifecycle of moving a character from one shard to another: quiesce → evict → redirect → load. |
| **Cache invariant** | "If an object is not resident on your shard, you do not cache it." Enforced by the handoff protocol and by the router never instantiating `ObjectDB` rows for play. |
| **Linearisation point** | The single Postgres write that defines when ownership of a character transfers from one shard to another. In this design: the `db_location` write to the target room. |

---

## Reference: originating FCM design documents

The following FCM-internal documents contain the originating design discussion. They include FCM-specific concepts (FCM's `RoomGateway` typeclass, FCM's `FungibleGameState` SINK refactor, FCM's specific zones) that will **not** be in the library — they are FCM's responsibility as a consumer. Worth reading for context but not for direct adoption:

- **`design/SCALING.md`** — Full multi-shard architecture from FCM's perspective. Sharding seam, cache invariant, shared-state problem, handoff protocol, PoC scope.
- **`design/WORLD_DEPLOYMENT.md`** — World build/redeploy/hot-reload pipeline for FCM. Three-tier architecture (world base, zones, districts). Composite identity (`zone, district, key`). Sharding interaction section commits to several decisions reused in the library.

These documents informed but do not bind the library. Where they reference FCM-specific implementations, those implementations stay in FCM. The library's job is to provide the general primitives FCM (and others) compose into their own systems.

---

## Reference: design conversation summary

This library's design emerged from a conversation between the FCM project owner and an LLM session, walking from the abstract goal of "scale beyond one Evennia process" through a series of refinements:

1. Initial proposal: build multi-shard infrastructure inside FCM, run at `shard_count=1` until needed.
2. Refinement: separate the multi-shard pieces into a standalone library because they are game-agnostic.
3. Survey of prior art: confirmed nothing exists in the Evennia ecosystem.
4. Refinement: extend the library to support a `monolith` mode (library dormant) so adoption is config-only, not a topology change.
5. Refinement: branch on mode at registration time, not per call. Strong "library isn't there" guarantee in monolith.
6. Refinement: `RoomGateway` becomes `ShardGatewayMixin` — composable onto consumer typeclasses rather than imposing inheritance.
7. Refinement: character→shard derives from `db_location`, not from a `current_shard` attribute or router-side registry.
8. Refinement: gateway destinations stamped with shard id at build time, not looked up at runtime.

The cumulative effect is a smaller, more conservative library than the original design implied. The architecture is the same; the surface area exposed to consumers is dramatically smaller, and the default state (monolith) is "library does nothing."

---

## Implementation principles for new sessions

When a new LLM session picks up this project, the following heuristics should guide architectural decisions:

1. **Default to "library does nothing" in monolith mode.** Any feature must justify its presence in monolith mode. If it doesn't add value there, gate it behind `SHARDS_ROLE != "monolith"`.
2. **Branch at registration time, not per call.** Per-call branching is acceptable only when registration-time branching is genuinely impossible.
3. **Resist runtime config registries.** Source-controlled Python constants in the consumer game are the canonical config surface for things like zone→shard maps. Reshuffles are planned events, not hot operations.
4. **The library does not own game concepts.** Rooms, characters, zones, items belong to the consumer game. The library provides infrastructure (ticket system, redirect protocol, handoff lifecycle, gateway mixin). When tempted to add a game concept (e.g., "starting room"), check whether it's actually game-specific and should stay in the consumer.
5. **Cache invariant by construction, not discipline.** Use `.values()` queries, mode-specific code registration, and clear ownership boundaries. Don't rely on developers remembering to not cache things.
6. **Web-first.** Telnet support is a deliberate post-PoC concern.
7. **Postgres for multi-shard, anything for everything else.** Don't gate features on Postgres unnecessarily.
8. **Resist FCM-specific assumptions creeping in.** When the design references FCM concepts (RoomGateway as typeclass, FungibleGameState, specific zone names), ask: is this a library concern or a consumer concern? Default to consumer.

---

## Closing note

This library has not yet been started. This document is the design substrate. The next step is project setup (naming, repo, license, license headers, scaffold) followed by Phase 1 of the PoC. Decisions captured here are deliberate but not immutable — refinements during implementation are expected, but should be made consciously rather than drifted into.
