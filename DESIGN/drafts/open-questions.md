# Open Questions

A tracker for design questions that have been raised in conversation but not yet resolved. As questions are worked through, they should be removed from this file and folded into whichever permanent doc is the right home (a settled design doc, a stub for a future doc, or just a captured decision).

This is not a place for speculation or invented problems — only questions that have come up in actual project conversation.

## Open

### Player-facing experience walkthrough (protocol/UX detail)

The detailed player-facing walkthrough — exact behaviour on `@ic`, `@ooc`, cross-shard movement, web client redirect handling — has not been designed. Specifics depend on decisions we have not yet made:

- Exact `RECONNECT_TO` message format.
- How tickets are presented (URL params, headers, initial WS message).
- Reachable error states (ticket expired, shard unreachable, character missing).
- What the player sees during the brief reconnect (loading indicator, blank, smooth).

Originally raised as a TBD in `deployment-topology.md` but identified as belonging in a future protocol/UX design doc rather than in topology.

### Client protocol support for the redirect mechanism

The library should aim to support all client protocols Evennia natively supports (web, telnet, SSH, etc.) — not preemptively defer non-web. Web clients have auto-reconnect built in, making the redirect transparent; telnet and SSH do not, so the player UX during cross-shard handoff for those protocols is open.

The mechanism for non-web protocols (printed reconnect prompt, automatic TCP-level forwarding, something else) is implementation-discovery work. Constraints on a particular protocol should only be documented if technical barriers actually force them, not assumed upfront.

Originally raised as a TBD in `deployment-topology.md` (framed there as "telnet handling deferred"); reframed as a positive aim of full client-protocol support.

### Is `ShardGatewayMixin` (or any consumer-facing room/exit mandate) needed at all?

Raised on 2026-04-29. The library can plausibly handle cross-shard cases entirely inside its own primitives (cross-shard message bus, cross-shard query helpers, cross-shard teleport), with no special "boundary room" concept and no mixin for consumers to apply. The original mandate's three jobs — stable identification, cross-shard data access, handoff landing — all reduce to *"every world-object row carries `(shard_id, pk)`"* plus the primitives.

If true, the consumer-facing surface of the library shrinks to "use the library's teleport / movement / messaging primitives in place of the corresponding raw Evennia ones" — possibly to zero, if the library can transparently override Evennia's own primitives without consumer participation.

Open until we prototype enough of the primitives to know whether any consumer-side marking is genuinely required.

### Cross-shard movement: what (if anything) must consumers mark or do?

If the gateway-room mandate is dropped, the previous consumer constraints around safe-state predicates and "narrative beat" framing need re-examination:

- Does the safe-state predicate live on the character (consumer-overridable, library-invoked from teleport), or somewhere else?
- Is "narrative beat" still a constraint, or just a default UX recommendation that consumers can violate at their own risk if they ship extended exit traversal?
- Do consumers need to declare *anywhere* that "this place is on shard X" — at the room level, at world-build time, or somewhere else?

Open pending resolution of the previous question.

### Dbref scoping across shards

Evennia treats `#42` as a globally meaningful, user-facing identifier (in command syntax, search, lock strings, exit destinations). With shared tables across shards, `#42` is no longer unambiguous. Options raised on 2026-04-29:

- Scope dbrefs (`#1:42` for shard 1's row 42) and update presentation everywhere.
- Stop using global IDs as user-facing identifiers in cross-shard contexts.
- Rely on the `(shard_id, pk)` tuple as the cross-shard reference shape and treat the user-facing dbref as shard-local only.

Open. Significant downstream impact on locks, exit destinations, command parsing.

### Misrouted vs deferred messages

The cross-shard message bus design ([cross-shard-message-bus.md](cross-shard-message-bus.md)) handles the case where a recipient cannot deliver a message right now: the recipient defers, retries on subsequent polls, and after ~10s gives up with an `undeliverable_reply` to the sender. This covers the *transient* case — a tell sent while the target character is briefly mid-handoff between shards.

It does not cleanly cover the *misrouted* case: the sender's view of "which shard owns character X" was stale, the character has actually moved to a different shard, and the message will *never* be deliverable by this recipient even after retries. Two approaches were raised on 2026-04-29:

- **Treat as undeliverable.** Simple. The sender (or the sender's app logic) refreshes their directory and either retries or surfaces to the user.
- **Re-route at the recipient.** Recipient looks up the current owner, addresses a new message to that shard, and deletes the original. More transparent to the sender; adds complexity (recipient has to know the directory and which lookups are authoritative).

Likely the first as a starting point; second can be added later if operational experience justifies it. Open until we build it.

### What cross-shard player interactions are feasible?

Some interactions are clearly impossible: real-time combat across shards requires mutation of state on another shard, ruled out by the first principle that any game object exists on exactly one shard. Some are clearly feasible: scry/locate spells that read character location via `.values()` queries against the shared DB, with no instantiation on the reading shard.

What sits in between — parties, follower trains, ambient effects on remote characters, channel-bridged interactions — depends on the messaging primitives the library will provide and on creative use of them. The full set of feasible cross-shard interactions is implementation-discovery work, not pre-determinable.

Originally captured in `consumer-constraints.md` as "cross-shard player interactions are second-class" — that constraint was too strong; removed pending discovery.

### Can shard membership / world topology change at runtime?

The original assumption (per the handover) was that room-to-shard mapping is a Python constant and reshuffles are planned downtime events. That assumption may not hold: it may be possible for a superuser to create new cross-shard transitions at runtime via in-game admin commands, or to reshuffle ownership without downtime. Open until the cross-shard handoff machinery is designed and the boundaries become clear.

Originally captured in `consumer-constraints.md` as "static zone-to-shard mapping; reshuffles are planned events" — removed pending implementation reality. (Also: the library partitions at the room level, not the zone level — see [shard-settings.md](../shard-settings.md) and the recent framing correction.)

### Do cross-shard transitions have any deploy-time requirements?

Open until the cross-shard handoff machinery is designed and prototyped. Specifically unknown:

- Whether anything needs to be deployed at any particular time, or via any particular mechanism, for cross-shard wiring to work.
- Whether the lookup machinery requires anything to pre-exist before a cross-shard reference can resolve, or can cope with fresh creation.
- Whether any specific operational model (Evennia's `at_initial_setup` hook, an `evennia shell -c` release command, manual in-game build) is preferred or required.

**Technical observation worth recording while it's fresh:** a standalone `python build_world.py` does not work as a deploy-time script because Evennia must be bootstrapped (Django settings loaded, models registered, idmapper alive) for `from evennia import create_object` to function. Any deploy-time build must run within an Evennia-aware context — `evennia shell -c "..."`, `at_initial_setup`, or invoked from within the running game. Relevant to whatever deploy story emerges.

Originally raised in conversation about possible `build-and-deploy-models.md` and `idempotent-builds.md` docs; both were speculative and have been deleted. Their substance — three operational models, source-vs-runtime attribute split, idempotency patterns, factory shapes — is preserved in our session history but not enshrined as design until the underlying mechanism question is resolved. Question reframed on 2026-04-29 to drop the gateway-room framing.
