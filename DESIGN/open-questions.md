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

### Client protocol support for the redirect mechanism

The library should aim to support all client protocols Evennia natively supports (web, telnet, SSH, etc.) — not preemptively defer non-web. Web clients have auto-reconnect built in, making the redirect transparent; telnet and SSH do not, so the player UX during cross-shard handoff for those protocols is open.

The mechanism for non-web protocols (printed reconnect prompt, automatic TCP-level forwarding, something else) is implementation-discovery work. Constraints on a particular protocol should only be documented if technical barriers actually force them, not assumed upfront.

Originally raised as a TBD in `deployment-topology.md` (framed there as "telnet handling deferred"); reframed as a positive aim of full client-protocol support.

### What cross-shard player interactions are feasible?

Some interactions are clearly impossible: real-time combat across shards requires mutation of state on another shard, ruled out by the first principle that any game object exists on exactly one shard. Some are clearly feasible: scry/locate spells that read character location via `.values()` queries against the shared DB, with no instantiation on the reading shard.

What sits in between — parties, follower trains, ambient effects on remote characters, channel-bridged interactions — depends on the messaging primitives the library will provide and on creative use of them. The full set of feasible cross-shard interactions is implementation-discovery work, not pre-determinable.

Originally captured in `consumer-constraints.md` as "cross-shard player interactions are second-class" — that constraint was too strong; removed pending discovery.

### Partial wrap improvement for `at_post_login` patches

The library currently patches `DefaultAccount.at_post_login` directly (full replacement on routers, thin wrapper on shards). This means a consumer override of `at_post_login` on their custom account class shadows our patch via MRO — bypassing the library's redirect/cache-bust logic unless they call `super()`. Documented in [library-integration-risks.md](library-integration-risks.md#defaultaccountat_post_login-override) with the recommended `super()` pattern.

There's a partial-improvement path worth revisiting: switch the patch target to the consumer-configured `BASE_ACCOUNT_TYPECLASS` (matching the chargen wrapper's pattern), and on routers delegate to the original for the `AUTO_PUPPET_ON_LOGIN = False` branch. That branch is just OOC-menu rendering — vanilla / consumer-override does the right thing. The library only needs to inline the `AUTO_PUPPET=True` branch where the redirect logic actually fires. Result: consumer overrides compose cleanly for the no-auto-puppet case (the more common case for many games), and only get bypassed for the auto-puppet path the library specifically exists to handle.

The trade-off: in the `AUTO_PUPPET=True + consumer-override` case, behaviour shifts from "library silently shadowed by consumer" to "consumer silently overwritten by library" — a different failure mode, not strictly better. Worth picking up if a real consumer game lands an `at_post_login` override and wants composition semantics; not load-bearing for current MVP scope.

### Can the room-to-shard partition be reshuffled without downtime?

The library partitions at the room level — every `ObjectDB` row carries a `shard_id`, and the `cross_shard_character_move` primitive can change a character's owning shard at runtime. What's unclear is whether the *room layout itself* can be reshuffled without downtime: moving rooms between shards (changing `shard_id` on a room and its contents) while a process is running, or carving out a new shard from rooms previously owned by another. The current working assumption is that such reshuffles are planned downtime events with full redeploy; whether the live machinery could support hot reshuffles without losing players is open. Likely deferred until a real consumer game wants to reorganise their world live.
