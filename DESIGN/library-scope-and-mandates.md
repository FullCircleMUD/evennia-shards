# Library Scope and Mandates

What this library provides and what it deliberately leaves to the consumer. The aim is the smallest possible mandatory surface — every requirement should be defensible by reference to the cross-shard correctness contract.

## Scope bound

The library is bound to the **single-Postgres era** — from one Evennia process today through however many shards run against a single, vertically scaled Postgres. Per the [archived handover](../archive/evennia-shards-HANDOVER.md#project-identity-and-positioning), anything beyond that bound is explicitly out of scope.

## Library primitives (working set)

A 2026-04-29 conversation reframed the library's value as a small set of cross-shard primitives rather than a consumer-facing typeclass mandate. The primitives identified, in build order:

1. **Cross-shard message bus.** Foundational. A way for one shard's process to send a message to another. Used internally for the teleport handoff signal and externally for cross-shard tells, channel propagation, and similar.
2. **Cross-shard query helpers.** `.values()`-shaped reads of rows owned by another shard, returning data without instantiating typeclass objects locally (preserving the cache invariant).
3. **Cross-shard teleport.** Library-extended movement primitive that handles same-shard and cross-shard targets transparently. Internally: serialise row, evict from source idmapper, signal destination via (1), destination loads row, redirect player session.

A possible fourth, deferred:

4. **Extended exit traversal.** Consumer exits whose destinations are on other shards. Mechanically a thin wrapper on (3) once (3) exists; deferred because the UX risk is real — `east` taking a noticeable moment will feel unresponsive in a way that "use portal" or "recall" does not. Worth doing only if the implementation is genuinely cheap by then and the UX is acceptable.

## External dependencies

Research on 2026-04-29 surfaced existing Django ecosystem libraries relevant to our partitioning machinery. The picture as of end of that day:

- **No external partitioning library.** `django-multitenant` was evaluated as off-the-shelf prior art for `tenant_id`-style row tagging and auto-filtering. After parallel prototyping it was not adopted — the bespoke four-chokepoint approach won on idmapper-composition simplicity, loud-failure semantics, and zero new runtime dependencies. See [shard-isolation.md](shard-isolation.md#decision-bespoke-chokepoints-vs-django-multitenant) for the decision in detail.
- **No external messaging dependency.** Earlier thinking considered `channels_redis` for the cross-shard message bus. A subsequent conversation reframed the bus as a Postgres `messages` table with polling — see [cross-shard-message-bus.md](cross-shard-message-bus.md). Removing Redis from the picture means one less ops dependency; the only infrastructure required by the library is Postgres, which Evennia already requires.

## Mandate: TBD pending review

The previous mandate of this document was: *"Consumers must apply `ShardGatewayMixin` to any room typeclass that acts as a cross-shard boundary."* The 2026-04-29 conversation called this into question. The reasoning:

- If the cross-shard machinery lives in library-provided primitives (teleport, query helpers, message bus), there is no need for a special "boundary room" typeclass — any room can be a cross-shard target, transparently.
- The mixin's three jobs (stable identification, cross-shard data access, handoff landing) all reduce to *"every row has `(shard_id, pk)`"* plus the primitives above.
- Removing the mandate makes the library invisible to consumers: they write rooms and exits the way they always did; the library handles cross-shard cases inside its own primitives.

[**TBD** — needs discussion: whether the library has any consumer-facing mandate at all, or whether the entire surface is "use the library's teleport/exit/messaging primitives in place of the corresponding raw Evennia ones." The original mixin mandate is no longer treated as canonical; the replacement (if any) is open.]

## What the library does not provide

- **Game concepts.** The library does not ship typeclasses for rooms, characters, items, exits, doors, or any other game-domain entity. These belong to the consumer.
- **Build helpers for game entities.** No `get_or_create_room`, no exit factories, no NPC builders. Patterns for these may be documented elsewhere as advisory, but the library does not ship the code.
- **Operational policy.** The library does not dictate when or how the consumer's build script runs.
- **Higher-level organisational concepts** (zones, regions, areas, biomes). Whether and how a consumer groups rooms is their world-design choice; the library partitions at the **room** level, not at any higher organisational level.

## Two principles that drove the scoping

- **The library does nothing in monolith mode.** Any feature must justify its presence in monolith. Non-trivial behaviour is gated on `SHARDS_ROLE != "monolith"`. This keeps the "library is dormant by default" guarantee strong.
- **The library does not own game concepts.** The temptation to ship "useful" helpers (room factories, world bootstrappers, generic exit builders) is real and should be resisted. Every such helper imposes opinions on the consumer that may not fit their existing patterns. The smallest contribution that makes split deployment a config option is what should ship; future additions should pass the same test.
  - *Worked example.* `cross_shard_character_move` does not validate that the target location is a Room (vs. a Character, Item, Exit, etc.). A library-level "rooms only" rule would forbid legitimate consumer use cases — moving a character into a vehicle, mount, or container on another shard. The primitive validates only that the target row exists and is on the target shard; choice of valid destinations is the consumer's. See the function's docstring for the long form.
