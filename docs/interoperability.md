# Interoperability

This library against every sibling library in `libraries/`. What it does that can constrain a sibling:
it installs django-multitenant tenancy on **`ObjectDB` and nothing else**, keeps the active shard in
**thread-local** storage, and runs the router process **unscoped**. Any library that writes `ObjectDB`
rows, dispatches ORM work off the reactor thread, or owns a persistent script is affected. See
[consumer-constraints.md](consumer-constraints.md) for what the library demands of the consuming game,
and [tenancy.md](tenancy.md) for the mechanism.

**Detecting a sharded deployment.** A successful import is not the test. `monolith` is a non-sharded
installation — no shard context is set and `get_shard_id()` returns `None` — so a sibling gating
behaviour on shards must check *the import succeeded **and** `get_role()` is not `monolith`*. Under
monolith a co-installed library should behave exactly as it does standalone.

Three constraints recur across the sections below, and are stated once here.

**Off-thread ORM work loses the shard context.** The active tenant lives in thread-local storage, so
code dispatched via `deferToThread`, `run_async`, `ThreadPoolExecutor.submit` or `asyncio.to_thread`
runs with a fresh thread-local: queries go unscoped and the auto-stamp on insert is skipped, landing
rows with `shard_id=NULL`. Wrap the callable with `preserve_tenant_context` **at the dispatch site**,
which captures the tenant eagerly at wrap time. The canonical consumer pattern — optional import with
an identity fallback — is in that function's docstring.

**`ScriptDB` is not tenant-scoped.** Only `ObjectDB` carries a `shard_id` column, so a persistent
script is a single row visible to every process, and each process attaches its own `LoopingCall` to it.
A script therefore ticks once per process.

A library whose script's work belongs to one shard declares that by stamping an `owning_shard`
Attribute, and this library confines the script's ticks to that process — see
[shard-owned-scripts.md](shard-owned-scripts.md). Ownership is declared as data: no base class to
inherit, no runtime API to call, and scripts that declare nothing are untouched. Stamp it wherever the
script is created, provided creation is itself confined to the owning shard.

Note that `Script.stop()` is not a confinement tool: it writes `db_is_active=False` to the shared row
and so stops the script cluster-wide. Nor is `pause()` — it reads the local `ndb._task` and, finding
none, writes nothing at all, so from a foreign process it silently does nothing. See
[shard-settings.md](shard-settings.md#global-scripts-run-one-instance-per-process).

**The router runs unscoped.** It sees and can write every shard's rows, and inserts from it carry no
shard stamp. Operations that place content into a shard's world belong on that shard, not the router.

## evennia-mob-spawner

**Optional integration.** mob-spawner imports `preserve_tenant_context` behind a `try` with an identity
fallback; this library imports nothing of mob-spawner's.

All three constraints above apply: it dispatches its deploy pipeline off-thread, it creates one
persistent `MobSpawnerScript` per rule-set file, and its tick calls `create_object`.

It is the first consumer of the shard-owned script mechanism: its Deployer stamps `owning_shard` at
deploy time, so each rule-set script ticks only on the shard it belongs to. The pairing also imposes a
naming rule on mob-spawner's YAML — the first declared level must be `shard`. Both rules are
mob-spawner's, since they constrain mob-spawner's data model and lifecycle; they are documented in
[its `interoperability.md`](../../evennia-mob-spawner/docs/interoperability.md) and not restated here.

## evennia-shards

This library.

## evennia-targeting

**No coupling.** Neither library imports the other. Targeting is a set of thin wrappers over
`caller.search()` that filter candidate lists already in hand; it issues no `ObjectDB` query of its
own, creates nothing, and dispatches nothing off-thread. It therefore inherits whatever scoping
`caller.search()` already has and introduces no unscoped path of its own.

## evennia-world-builder

**Optional integration.** world-builder imports `preserve_tenant_context` behind a `try` with an
identity fallback; this library imports nothing of world-builder's.

The off-thread and router constraints apply — `wb_build` defers its whole pipeline to a worker thread
and creates `ObjectDB` rows, so a build run from the router would produce rooms with `shard_id=NULL`.
The pairing also imposes a naming rule on world-builder's YAML — the first declared level must be
`shard` — which gates `wb_build` to the owning process and refuses the build-everything scope. That
rule is world-builder's, since it constrains world-builder's data model; it is documented in
[its `interoperability.md`](../../evennia-world-builder/docs/interoperability.md) and not restated here.

The `ScriptDB` constraint does not apply: world-builder creates no persistent scripts.

## evennia-yaml-reader

**No coupling.** Neither library imports the other. yaml-reader depends only on `pyyaml`, has no
Evennia dependency at all, and touches no database — so nothing it does is visible to the tenancy
layer.
