# Library Scope and Mandates

What this library provides and what it deliberately leaves to the consumer. The aim is the smallest possible mandatory surface — every requirement should be defensible by reference to the cross-shard correctness contract.

## Scope bound

The library is bound to the **single-Postgres era** — from one Evennia process today through however many shards run against a single, vertically scaled Postgres. Per the [archived handover](archive/evennia-shards-HANDOVER.md#project-identity-and-positioning), anything beyond that bound is explicitly out of scope.

## Mandate: use `ShardGatewayMixin` for cross-shard boundary rooms

The consumer must apply `ShardGatewayMixin` to any room typeclass that acts as a cross-shard boundary. The mixin provides the machinery for:

- Stable identification of gateway rooms across processes, independent of auto-generated DB IDs.
- Reading data about a gateway room from another shard without instantiating the object into the local idmapper (cache invariant).
- Landing a character into a gateway room during cross-shard handoff.

The form of that machinery — exact identifier scheme, lookup mechanism, attribute layout — is implementation work, not a scope commitment. The scope-level commitment is that the mixin exists and that consumers must use it for cross-shard boundary rooms.

The mixin handles both **intra-shard** and **inter-shard** movement transparently. When a gateway's destination is on the same shard, the mixin teleports the character locally; when the destination is on another shard, it triggers the cross-shard handoff. A consumer can therefore design their world with gateways at every place they might *eventually* want a shard boundary, run as monolith indefinitely, and later shard the deployment without changing the world — only the deployment configuration changes.

## What the library does not provide

- **Game concepts.** The library does not ship typeclasses for rooms, characters, items, exits, doors, or any other game-domain entity. These belong to the consumer.
- **Build helpers for non-gateway entities.** No `get_or_create_room`, no exit factories, no NPC builders. Patterns for these may be documented elsewhere as advisory, but the library does not ship the code.
- **Operational policy.** The library does not dictate when or how the consumer's build script runs.
- **Zone-to-shard mapping mechanism.** This is a Python constant in the consumer's source code. The library does not provide a runtime registry, config file, or service for it.

## Two principles that drove the scoping

- **The library does nothing in monolith mode.** Any feature must justify its presence in monolith. Non-trivial behaviour is gated on `SHARDS_ROLE != "monolith"`. This keeps the "library is dormant by default" guarantee strong.
- **The library does not own game concepts.** The temptation to ship "useful" helpers (room factories, world bootstrappers, generic exit builders) is real and should be resisted. Every such helper imposes opinions on the consumer that may not fit their existing patterns. The smallest contribution that makes split deployment a config option is what should ship; future additions should pass the same test.
