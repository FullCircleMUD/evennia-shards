# Open Questions

A tracker for design questions that have been raised in conversation but not yet resolved. As questions are worked through, they should be removed from this file and folded into whichever permanent doc is the right home (a settled design doc, a stub for a future doc, or just a captured decision).

This is not a place for speculation or invented problems — only questions that have come up in actual project conversation.

## Open

### Auto-puppet on login under `MULTISESSION_MODE` 2 or 3 (Evennia upstream limitation)

Evennia's `_last_puppet` is a single account-scoped Attribute. Under `MULTISESSION_MODE = 2` or `3`, an account can have multiple simultaneous puppets across sessions, and a single attribute can't represent "which puppet belongs to which session for reconnect." Evennia itself documents this in [`settings_default.py:766`](../../venv/Lib/site-packages/evennia/settings_default.py): *"This will only work if the session/puppet combination can be determined (usually `MULTISESSION_MODE 0` or `1`)."*

The conceptual fix is per-session puppet memory — e.g. `account.db._last_puppets = {session_key: char_id, ...}` keyed by something durable, cleared per-session on OOC. That's an Evennia-level change (the session-puppet mapping lives in `AccountDB`/`puppet_object`, not in this library).

**Library posture:** `evennia-shards` inherits the limitation. AUTO_PUPPET on routers works under modes 0 and 1; modes 2/3 are documented-broken to the same degree as vanilla Evennia. This is the same "labour-intensive divergence" carve-out we apply to telnet support — explicit deferral, not silent gap.

If Evennia upstream ever ships per-session puppet memory, the library's `at_post_login` override should adopt it transparently and this entry can fold into the design docs.

Originally surfaced 2026-05-01 while designing the `AUTO_PUPPET_ON_LOGIN = True` path on the router.

### Player-facing experience walkthrough (protocol/UX detail)

The detailed player-facing walkthrough — exact behaviour on `@ic`, `@ooc`, gateway traversal, web client redirect handling — has not been designed. Specifics depend on decisions we have not yet made:

- Exact `RECONNECT_TO` message format.
- How tickets are presented (URL params, headers, initial WS message).
- Reachable error states (ticket expired, shard unreachable, character missing).
- What the player sees during the brief reconnect (loading indicator, blank, smooth).

Originally raised as a TBD in `deployment-topology.md` but identified as belonging in a future protocol/UX design doc rather than in topology.

### Client protocol support for the redirect mechanism

The library should aim to support all client protocols Evennia natively supports (web, telnet, SSH, etc.) — not preemptively defer non-web. Web clients have auto-reconnect built in, making the redirect transparent; telnet and SSH do not, so the player UX during cross-shard handoff for those protocols is open.

The mechanism for non-web protocols (printed reconnect prompt, automatic TCP-level forwarding, something else) is implementation-discovery work. Constraints on a particular protocol should only be documented if technical barriers actually force them, not assumed upfront.

Originally raised as a TBD in `deployment-topology.md` (framed there as "telnet handling deferred"); reframed as a positive aim of full client-protocol support.

### Do gateway rooms have any deploy-time requirements?

Open until the gateway lookup / handoff machinery is designed and prototyped. Specifically unknown:

- Whether gateway rooms need to be deployed at any particular time, or via any particular mechanism, for cross-shard wiring to work.
- Whether they need to be idempotent across deploys (may not matter if gateway rooms are effectively stateless).
- Whether the lookup machinery requires them to pre-exist before a cross-shard reference can resolve, or can cope with fresh creation.
- Whether any specific operational model (Evennia's `at_initial_setup` hook, an `evennia shell -c` release command, manual in-game build) is preferred or required for gateway rooms.

We previously assumed gateway rooms would need idempotent deployment via a library-provided helper, but that assumption is anticipatory and pre-implementation. Pending the actual lookup/handoff mechanism design to determine.

**Technical observation worth recording while it's fresh:** a standalone `python build_world.py` does not work as a deploy-time script because Evennia must be bootstrapped (Django settings loaded, models registered, idmapper alive) for `from evennia import create_object` to function. Any deploy-time build must run within an Evennia-aware context — `evennia shell -c "..."`, `at_initial_setup`, or invoked from within the running game. Relevant to whatever deploy story emerges.

Originally raised in conversation about possible `build-and-deploy-models.md` and `idempotent-builds.md` docs; both were speculative and have been deleted. Their substance — three operational models, source-vs-runtime attribute split, idempotency patterns, factory shapes — is preserved in our session history but not enshrined as design until the underlying mechanism question is resolved.

### What cross-shard player interactions are feasible?

Some interactions are clearly impossible: real-time combat across shards requires mutation of state on another shard, ruled out by the first principle that any game object exists on exactly one shard. Some are clearly feasible: scry/locate spells that read character location via `.values()` queries against the shared DB, with no instantiation on the reading shard.

What sits in between — parties, follower trains, ambient effects on remote characters, channel-bridged interactions — depends on the messaging primitives the library will provide and on creative use of them. The full set of feasible cross-shard interactions is implementation-discovery work, not pre-determinable.

Originally captured in `consumer-constraints.md` as "cross-shard player interactions are second-class" — that constraint was too strong; removed pending discovery.

### Can shard membership / world topology change at runtime?

The original assumption (per the handover) was that zone-to-shard mapping is a Python constant and reshuffles are planned downtime events. That assumption may not hold: it may be possible for a superuser to create new gateway rooms (and by implication, new shard boundaries) at runtime via in-game admin commands, or to reshuffle zone ownership without downtime. Open until the gateway lookup / handoff machinery is designed and the boundaries become clear.

Originally captured in `consumer-constraints.md` as "static zone-to-shard mapping; reshuffles are planned events" — removed pending implementation reality.

### How do cross-shard messages behave during in-transit state?

A character mid-handoff is briefly not resident on either source or target shard (between the source's eviction from idmapper and the target's load). If a third party sends them a message during that window — a tell, a channel message, a directed effect — what happens? The handover proposed bouncing with *"player is in transit, try again,"* but the actual mechanism for detecting in-transit state, the UX implications, and whether the same logic applies to all message classes (tells, channels, persistent mail) haven't been worked through.

Originally captured in `consumer-constraints.md` as "do not assume in-process synchronicity for cross-shard operations" — moved here as it's an unresolved design question, not a settled constraint.
