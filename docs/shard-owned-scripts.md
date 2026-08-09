# Shard-owned scripts

`ScriptDB` is not tenant-scoped, so a persistent script is one row visible to every process, and each
process attaches its own `LoopingCall` to it. For a script whose work belongs to one shard, that is
wrong twice over: it can tick where it does not belong, and on the unscoped router the rows it creates
land with no shard stamp. This library lets a consumer declare which shard owns a script, and confines
the script's ticks to that process.

## Declaring ownership

Ownership is data, not inheritance. A consumer sets an Attribute on the script:

```python
from evennia_shards import OWNING_SHARD_ATTR

script.attributes.add(OWNING_SHARD_ATTR, "shard0")
```

There is no base class to inherit, no mixin to compose, and no runtime API to call. A script that does
not carry the Attribute is never confined, which is what keeps global scripts — and every script in a
non-sharded install — working exactly as before.

The natural place to stamp it is wherever the script is created, provided creation itself is confined
to the owning shard. `evennia-mob-spawner` does this in its Deployer: `ms_load` refuses to run from any
process but the owning shard, so the shard the deploy happens on *is* the owner.

Stamp only when sharding is in play. A successful `import evennia_shards` is not the test — `monolith`
is a non-sharded install where no shard context is ever set. See
[interoperability.md](interoperability.md) for the detection rule consumers should use.

## How confinement works

`install_script_confinement()` runs from `EvenniaShardsConfig.ready()`, beside the `ObjectDB` tenancy
install, and late-binds guards onto two methods of `ScriptBase` — the typeclass base, not `ScriptDB`,
which carries no task machinery of its own. Idempotent via a marker attribute, in the same style as
the tenancy install.

Both methods are guarded, because neither covers the other:

**`_unpause_task`** is what the boot walk calls. `ScriptDBManager.update_scripts_after_server_start()`
iterates every active row and calls it directly, never going through `_start_task`.

**`_start_task`** covers everything else — explicit `start()`, `unpause()`, and the autostart on
creation. It calls `_unpause_task` internally, but then starts the loop itself if that didn't, so
guarding `_unpause_task` alone is bypassable.

## Why the guard returns before touching `_paused_time`

This is the load-bearing detail.

At shutdown each process calls `_pause_task(auto_pause=True)` on its active scripts, and only the
process actually holding the task writes anything — the pause marker `db._paused_time` goes onto the
shared row. At boot, every process's walk sees that marker; the first to reach it attaches the
`LoopingCall` and then *clears* the marker. Everyone after it finds nothing to unpause.

Without confinement that is a race decided by boot order, not by ownership. And because processes boot
independently, a router that habitually starts first wins it every time — a systematic loss rather
than an intermittent one.

The guard returns before reading or clearing `_paused_time`, so a foreign process consumes nothing.
The marker survives for the owning process to claim whenever it boots. **Boot order stops mattering.**

## What this does and does not provide

It answers *which process may run this script*. It does not schedule anything, elect anything, or
guarantee a script runs at all — a script whose owning process never starts simply does not tick.

For a script that must run exactly once across the cluster without belonging to any particular shard,
this is not the tool; that remains the consumer's problem, solved by role, by nominating a shard, or by
their own election. See [shard-settings.md](shard-settings.md#global-scripts-run-one-instance-per-process).

## Coupling to Evennia internals

`_start_task` and `_unpause_task` are private methods. Wrapping them is a deliberate coupling, of the
same kind and for the same reasons as the `ObjectDB` wrapping in [tenancy.md](tenancy.md) — see
[library-integration-risks.md](library-integration-risks.md) for how this library treats that class of
risk generally.

Detection: the guards are covered by unit tests that call both wrapped methods directly, so a signature
change or a rename in a future Evennia version fails the suite rather than silently disabling
confinement.

## Verified behaviour

Confirmed on a live two-process deployment with instrumented guards, across repeated restarts in
varying boot orders:

- The router refused every owned script on every boot, without consuming any pause marker.
- The owning shard, booting up to 15 seconds later, still found its markers and reclaimed all of them.
- A second shard refused the first shard's scripts, and reclaimed its own — confinement is between
  shards, not merely router-versus-shard.
- `_start_task` was never reached. No current consumer command calls it for an owned script on a
  foreign process; it is guarded because it is the general "make it run" entry point, and a future
  command or a direct `start()` would otherwise bypass confinement silently.
