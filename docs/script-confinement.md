# Script confinement

`ScriptDB` is not tenant-scoped, so a persistent script is one row visible to every process, and each
process attaches its own `LoopingCall` to it. For a script whose work belongs somewhere specific, that
is wrong twice over: it can tick where it does not belong, and on the unscoped router the rows it
creates land with no shard stamp. This library lets a consumer declare where a script belongs, and
confines it there.

## Declaring where a script belongs

Two declarations, as Attributes on the script. A consumer sets one or the other — never both.

**`owning_shard`** — exactly one shard owns this script. Compared against the process's `SHARD_ID`.

```python
from evennia_shards import OWNING_SHARD_ATTR

script.attributes.add(OWNING_SHARD_ATTR, "shard0")
```

**`owning_roles`** — a list of roles allowed to run it. Compared against the process's role. Use this
when there is no single owner: work belonging to every shard but not the router cannot be expressed as
one shard id.

```python
from evennia_shards import OWNING_ROLES_ATTR

script.attributes.add(OWNING_ROLES_ATTR, ["shard", "monolith"])
```

A bare string is accepted and normalised to a one-element list, so `"shard"` is not read character by
character.

**They are mutually exclusive.** "Only shard0" and "any shard" cannot both hold, so a script declaring
both is misconfigured rather than doubly-constrained. The guards refuse to run it anywhere and log the
contradiction at `ERROR` naming the script and both values. Failing closed is the safe direction: a
script that does not run shows up in the consumer's own status tooling, whereas one running in the
wrong place is the failure this module exists to prevent.

Declaring neither leaves the script unconfined. That is what keeps scripts genuinely belonging
everywhere — a keepalive ping, say — working untouched, and it means the mechanism is opt-in by data
with no cost to anything that doesn't use it.

There is no base class to inherit, no mixin to compose, and no runtime API to call.

## Where to stamp it

Wherever the script is created, provided creation itself happens where the declaration says it should.
`evennia-mob-spawner` stamps `owning_shard` in its Deployer, because `ms_load` refuses to run from any
process but the owning shard — so the shard the deploy happens on *is* the owner. A consumer declaring
global scripts by role stamps `owning_roles` from its own role table as it creates them.

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

## The failure this closes

At shutdown each process calls `_pause_task(auto_pause=True)` on its active scripts, and only the
process actually holding the task writes anything — the pause marker `db._paused_time` goes onto the
shared row. At boot, every process's walk sees that marker; the first to reach it attaches the
`LoopingCall` and then *clears* it. Everyone after finds nothing to unpause.

The walk knows nothing about roles or shards. It iterates every active row. So **the first process to
boot attaches every marked script in the cluster**, not merely its own — and processes booting later,
finding the markers consumed, quietly get only what they claim for themselves.

A consumer's own role table doesn't prevent this, because such a table governs what each process
*creates*, not what the walk *attaches*. Role gating is therefore only half-enforced at boot without
confinement: honoured for what a process claims, silently exceeded for what it sweeps up.

Because processes boot independently, this is not an intermittent race but a standing bias toward
whichever process habitually starts first.

## Why the guard returns before touching `_paused_time`

This is the load-bearing detail. The guard returns before reading or clearing the marker, so a process
that isn't entitled to the script consumes nothing. The marker survives for the process that *is*
entitled to claim it whenever it boots. **Boot order stops mattering** rather than being corrected
after the fact — there is no window in which the wrong process holds the ticker.

## What this does and does not provide

It answers *where may this script run*. It does not schedule anything, elect anything, or guarantee a
script runs at all — a script whose entitled process never starts simply does not tick.

For a script that must run exactly once across the cluster while belonging to no particular shard or
role, this is not the tool; that remains the consumer's problem, solved by nominating a role, a shard,
or by their own election. See
[shard-settings.md](shard-settings.md#global-scripts-run-one-instance-per-process).

## Coupling to Evennia internals

`_start_task` and `_unpause_task` are private methods. Wrapping them is a deliberate coupling, of the
same kind and for the same reasons as the `ObjectDB` wrapping in [tenancy.md](tenancy.md) — see
[library-integration-risks.md](library-integration-risks.md) for how this library treats that class of
risk generally.

Detection: the guards are covered by unit tests that call both wrapped methods directly, so a signature
change or a rename in a future Evennia version fails the suite rather than silently disabling
confinement.

## Verified behaviour

Confirmed on a live three-process deployment — router plus two shards — across repeated restarts in
varying boot orders.

**`owning_shard`**, with instrumented guards logging every decision:

- The router refused every owned script on every boot, without consuming a pause marker.
- The owning shard, booting up to 15 seconds later, still found its markers and reclaimed all of them.
- A second shard refused the first shard's scripts while reclaiming its own — confinement holds
  between shards, not merely router-versus-shard.
- `_start_task` was never reached in 55 logged decisions. No current consumer command calls it for an
  owned script on a foreign process; it is guarded because it is the general "make it run" entry
  point, and a future command or a direct `start()` would otherwise bypass confinement silently.

**`owning_roles`**, against a consumer's eleven role-declared global scripts:

- Before: the first process to boot held every marked script. Router-first left the router ticking six
  scripts declared shard-only, unscoped, on every boot.
- After, router-first: the router held its five and swept none.
- After, shard-first: the shards held their six plus the one declared for all roles, and left the
  four router-only scripts alone — including the reallocation service, where a second process running
  it concurrently would credit the same balance twice.

Both boot orders, all three processes, every script exactly where its declaration says it belongs.
