# Tenancy: django-multitenant integration

The per-shard partition is enforced at the SQL layer via [django-multitenant](https://github.com/citusdata/django-multitenant). Every query through `ObjectDB.objects` carries `WHERE shard_id IN (current, '*')` automatically; the boundary is in the database, not in Python.

## What the library demands of django-multitenant

The integration uses django-multitenant 4.1.1 with three departures from its stock usage:

1. **No `Shard` Django model.** Multitenant expects tenants to be model instances with a `tenant_value` attribute. We provide a lightweight stand-in class (`Shard` in `tenancy.py`) that exposes `tenant_value` and nothing else — equality and hash by `shard_id`. Avoids the schema overhead of a real Tenant model when all we need is the protocol.

2. **Two-element tenant list, always.** The active "tenant" set by `set_current_shard()` is always `[Shard(SHARD_ID), Shard("*")]` — never a single object. Multitenant interprets list-valued tenants by switching from `WHERE tenant = value` to `WHERE tenant IN (...)`, which is exactly the semantics we need: current shard's rows AND global `"*"`-stamped rows visible together. The list shape is the single trick that gives us global-rows-visible-from-every-shard without subclassing any QuerySet or Manager.

3. **Late-binding onto `ObjectDB`** rather than subclassing. Evennia consumers depend on the exact `ObjectDB` class; we can't put `TenantModelMixin`/`TenantManagerMixin` in its MRO via inheritance. So we attach the relevant behaviour at runtime in `apps.ready()`.

## Where it runs

Everything lives in [`evennia_shards/tenancy.py`](../src/evennia_shards/tenancy.py). The install is invoked from `EvenniaShardsConfig.ready()` in [`apps.py`](../src/evennia_shards/apps.py), gated on `get_role() != ROLE_MONOLITH` — so monolith processes (which don't load the library) and tests configured for monolith mode never touch any of it.

The install is two function calls:

```python
# apps.py
from .tenancy import bootstrap_tenant_context, install_tenancy_on_objectdb

bootstrap_tenant_context()       # sets the process-wide scope
install_tenancy_on_objectdb()    # attaches the mixins to ObjectDB
```

## Step 1: `bootstrap_tenant_context()` — set the process scope

Reads `SHARDS_ROLE` and `SHARD_ID` via the config accessors and routes to one of three branches:

| Role | Action | Effective scope |
|---|---|---|
| `shard` | `set_current_shard(SHARD_ID)` → sets `_context.tenant = [Shard(SHARD_ID), Shard("*")]` | Auto-filter: `WHERE shard_id IN (SHARD_ID, '*')` |
| `router` | `clear_shard_context()` → sets `_context.tenant = None` | No auto-filter applied; sees every row |
| `monolith` | No-op (defensive — branch should be unreachable since the library isn't loaded) | Untouched |

Multitenant's tenant context is thread-local. Each shard process holds its context for the full process lifetime; the only switches during normal operation are inside `shard_context()` blocks (handoff, chargen, admin tooling).

Multitenant defaults to `threading.local` but supports `asgiref.local.Local` via the `TENANT_USE_ASGIREF` setting. Twisted (Evennia's reactor) is single-threaded for most operations but uses `deferToThread` for some blocking calls; if those threads ever do ORM work they would inherit nothing from the main thread's tenant context. See *Known gaps* below.

## Step 2: `install_tenancy_on_objectdb()` — attach the mixins

Idempotent via the marker `ObjectDB._evennia_shards_tenancy_installed`. Five sub-steps, executed in this order:

### 2.1 Attach `TenantMeta`

```python
class TenantMeta:
    tenant_field_name = "shard_id"
ObjectDB.TenantMeta = TenantMeta
```

The single declaration that makes multitenant treat `ObjectDB` as tenant-tagged. Multitenant's `get_tenant_column(model)` reads `model.TenantMeta.tenant_field_name` to know which column carries the tenant id.

### 2.2 Copy the three identity properties

```python
ObjectDB.tenant_field = TenantModelMixin.tenant_field      # → "shard_id"
ObjectDB.tenant_value = TenantModelMixin.tenant_value      # → row's shard_id value
ObjectDB.tenant_object = TenantModelMixin.tenant_object    # → same as tenant_value (no FK)
```

These properties only read `self.TenantMeta` / `self.shard_id`. No `super()` involvement, so a straight copy onto ObjectDB works. Multitenant's internals call these on instances during filter / stamp / update-scope operations.

### 2.3 Wrap `save`, `_do_update`, `__setattr__`

`TenantModelMixin`'s versions of these all use bare `super()`. If copied onto `ObjectDB`, Python's implicit `__class__` cell still binds to `TenantModelMixin`, and `super()` would resolve to TenantModelMixin's MRO parent (`object`) — bypassing `ObjectDB.save` entirely. So instead of copying, we *inline the logic* with explicit captured-original calls:

```python
_original_save = ObjectDB.save

def _tenant_aware_save(self, *args, **kwargs):
    ...
    return _original_save(self, *args, **kwargs)   # explicit, not super()

ObjectDB.save = _tenant_aware_save
```

**`save()`** — auto-stamp on insert. Multitenant's stock `set_object_tenant` skips list-valued tenants (it doesn't know which item to stamp). Since our context is always a list, we replace the stock helper with explicit list-aware logic: stamp with the first item of the list (the active shard, not the global `"*"` sentinel).

We also deliberately **drop multitenant's temporary tenant-switch** during save. Upstream calls `set_current_tenant(self.tenant_value)` around the inner save to scope the UPDATE WHERE clause to the row's own tenant. That logic doesn't compose with our two-element list shape — it collapses the IN-filter to a single shard and loses the `"*"` arm. The `_do_update` wrapper below already applies the full filter, which correctly handles current-shard rows, globals, and silently no-ops on foreign rows. The switch is both unnecessary and harmful in our setup.

The same wrapper carries the **unstamped-INSERT guard**. When no tenant is set the stamp above is skipped, and the row would insert with `shard_id=NULL` — owned by no shard, matched by no shard's auto-filter, not a valid IC destination. The wrapper refuses instead, raising `UnstampedInsertError`. See [the guard](#the-unstamped-insert-guard) below.

**`_do_update()`** — adds the tenant filter to the per-row UPDATE WHERE clause. Signature-tolerant via `*args, **kwargs`:

```python
def _tenant_aware_do_update(self, base_qs, *args, **kwargs):
    if get_current_tenant():
        base_qs = base_qs.filter(**get_tenant_filters(self.__class__))
    return _original_do_update(self, base_qs, *args, **kwargs)
```

Django 6.0 added `returning_fields` as an extra positional argument to `_do_update`; django-multitenant 4.1.1 still has the older 6-arg signature. The passthrough approach insulates us from future signature drift — we only need to mutate `base_qs`; everything else forwards untouched.

**`__setattr__()`** — flags attempts to change `shard_id` on an existing row. The next `save()` checks for the `_try_update_tenant` marker and raises `NotSupportedError`. Together they enforce "tenant column is immutable after insert."

One subtlety: Django's `Model.__init__` sets `self._state = ModelState()` as its very first attribute assignment. Our wrapper reads `self._state.adding` to decide whether the row is new or existing — but during the assignment of `_state` itself, the attribute doesn't yet exist. The wrapper guards with `hasattr(self, "_state")` and short-circuits to the raw setter during construction.

### 2.4 Install global Django decorators (`_install_global_query_decorators()`)

Multitenant normally installs these on the first instantiation of a tenant-tagged model (via `TenantModelMixin.__init__`). Because we late-bind rather than putting the mixin in the MRO, that `__init__` never runs as the mixin's own — so we trigger the global setup directly at install time.

Four decorators get applied:

| Decorator | Target | Effect |
|---|---|---|
| `wrap_get_compiler` | `DeleteQuery.get_compiler` | DELETE WHERE clause gets the tenant filter |
| `wrap_delete` + `related_objects` | `Collector.delete`, `Collector.related_objects` | FK cascade collection respects the tenant filter (won't collect rows from other shards) |
| `wrap_update_batch` | `UpdateQuery.update_batch` | Bulk `UPDATE WHERE pk IN (...)` gets the tenant filter |
| `wrap_forward_many_to_many_manager` | `create_forward_many_to_many_manager` | M2M `.add()` stamps the through-row with current tenant |

Each is idempotent via a `_sign` marker attribute set by the wrap helpers.

### 2.5 Patch the manager class

The read-side auto-filter and bulk-insert stamping go on the manager. We *patch the existing manager class* rather than swapping in a tenant-mixed subclass:

```python
manager_cls = type(ObjectDB.objects)
manager_cls.get_queryset = _tenant_aware_get_queryset
manager_cls.bulk_create = _tenant_aware_bulk_create
manager_cls._evennia_shards_tenant_patched = True
```

**Why patch rather than swap.** Django's `contribute_to_class` *appends* a manager to `cls._meta.local_managers`. Manager resolution then walks `local_managers` in insertion order and takes the first one matching the name (`"objects"`) — which would be the original `ObjectDBManager` Evennia registered at class-definition time. Our `TenantObjectDBManager` would be appended after it and silently ignored. Patching the class methods avoids the dedup machinery entirely.

The patch wraps rather than replaces multitenant's `TenantManagerMixin.get_queryset`. Upstream constructs a queryset from scratch (`self._queryset_class(self.model)`), losing the `using` and `hints` arguments the original passes. We instead call the original, then apply the tenant filter on top — preserving Django's manager-construction semantics underneath.

`bulk_create` is patched explicitly because Django bypasses `pre_save` for bulk inserts; we have to stamp each unsaved instance ourselves before delegating to the original.

`bulk_update` deliberately has **no** equivalent patch. It needs none: the call routes through the patched `get_queryset` above, and the `UPDATE ... WHERE pk IN (...)` it compiles to also picks up the `wrap_update_batch` decorator from §2.4. Both layers are generic rather than `bulk_update`-specific, so the behaviour is inherited rather than implemented — which is why it is pinned by test rather than by code. See `AutoStampAndFilterTests.test_bulk_update_scopes_to_current_shard` (foreign rows in a batch are a silent no-op) and `test_bulk_update_can_reassign_shard_id` (the tenant-column escape, below).

## What's active after install

From the moment `ready()` returns:

| Operation | Behaviour |
|---|---|
| `ObjectDB.objects.filter(...)` / `.all()` / `.get()` | Auto-filter: `WHERE shard_id IN (current, '*')` |
| `ObjectDB.objects.create(...)` | Row stamped with current shard automatically |
| `ObjectDB.objects.create(...)` with **no tenant set** | Raises `UnstampedInsertError` — a `shard_id=NULL` row is owned by no shard. Opt in via `allow_unstamped_insert()` only when the row is stamped immediately after |
| `obj.save()` | UPDATE WHERE pk=X AND shard_id IN (current, '*') — current-shard and global rows update; foreign rows silent no-op |
| `obj.delete()` | DELETE WHERE same filter |
| `qs.update(**fields)` / `qs.delete()` | Bulk operations with tenant filter, including cascade collection |
| `ObjectDB.objects.bulk_create([...])` | Each instance auto-stamped before insert; with no tenant set, raises `UnstampedInsertError` |
| `ObjectDB.objects.bulk_update([...], fields)` | Tenant filter inherited, no dedicated patch — current-shard and global rows update; foreign rows named in the same batch are a silent no-op |
| `obj.shard_id = "other"` then `obj.save()` | Raises `NotSupportedError` — tenant column is immutable |
| `ObjectDB.objects.bulk_update([obj], ["shard_id"])` | **Reassigns the tenant column.** The immutability check lives on `__setattr__` + `save()`, which this path never touches — the same deliberate escape `cross_shard_move` takes via `qs.update`, reached by a different door |
| `with shard_context("shard1"):` | All of the above behaves as `shard1` (or unscoped if `None`) inside the block |

All of Evennia core and any consumer game code inherits this transparently. No call-site changes required.

## The unstamped-INSERT guard

A row with `shard_id=NULL` belongs to no shard. No shard's auto-filter matches it, it is not a valid IC destination, and nothing in normal operation will ever find it again. It is not a lesser version of a stamped row — it is lost.

The auto-stamp only runs when a tenant is set. With no tenant set, an INSERT would simply skip the stamp and write `NULL` — no error, no log. The guard closes that: **both INSERT paths refuse rather than write an unowned row.**

| Path | Behaviour with no tenant set |
|---|---|
| `obj.save()` on a new instance | Raises `UnstampedInsertError` |
| `ObjectDB.objects.bulk_create([...])` | Raises `UnstampedInsertError` naming the count and first offending key |
| `obj.save()` on an existing NULL row | **Allowed** — see below |

Every refusal is logged at `ERROR` to `shards.log` *before* the raise. That is deliberate: a raise is not a record. Any caller with a broad `except` swallows the exception and the blocked object simply never appears, with nothing anywhere saying why. See [logging.md](logging.md).

### INSERT only, never UPDATE

The guard checks Django's `self._state.adding`. UPDATEs of an already-NULL row pass through untouched.

This is load-bearing, not tidiness. It is exactly how the sanctioned bypass repairs itself: insert unstamped, then `save(update_fields=["shard_id"])` to write the real shard. A guard that fired on UPDATEs would block the repair and make the bypass useless. It also keeps any pre-existing NULL rows backfillable.

### Not active in monolith

`install_tenancy_on_objectdb()` only runs when `SHARDS_ROLE != "monolith"`, so the wrapper — and therefore the guard — is never attached in monolith mode. No monolith consumer can trip it.

### The router refuses too

The router runs unscoped by design, so *every* `ObjectDB` INSERT from it reaches the guard. That is intended: content belongs on a shard, not the router (see [interoperability.md](interoperability.md)). Router-side code that legitimately inserts must opt in explicitly.

One consequence to plan for: on a **fresh database booted router-first**, Evennia's `initial_setup` creates Limbo and the superuser character, and those inserts now fail rather than landing `shard_id=NULL`. Boot `shard0` first — which [deployment-topology.md](deployment-topology.md) already directs for the same underlying reason.

## `allow_unstamped_insert()` — the one way through

Some inserts genuinely must land unstamped and are repaired immediately afterwards. They opt in:

```python
from evennia_shards import allow_unstamped_insert

with allow_unstamped_insert():
    obj = create_object(...)      # lands shard_id=NULL, permitted
obj.shard_id = "shard0"           # repair, under the guard as normal
obj.save(update_fields=["shard_id"])
```

**Keep the block as tight as the insert.** One call, not a function body. Everything inside loses the protection, so the narrower the block, the smaller the hole. The repair save deliberately sits *outside* it.

The flag is **thread-local**, matching multitenant's own tenant storage. A block opened on the reactor thread does not open the guard for concurrent thread-pool work. Nesting is safe — the previous value is restored on exit, so an inner block cannot close a hole an outer one opened.

`unstamped_insert_allowed()` reports the current state, for code that needs to branch on it.

### Reach for it second, not first

A guard hit means no tenant was set. There are two reasons for that, and they need opposite fixes:

1. **The insert is meant to be unstamped** and is stamped a moment later — router-side chargen, an account-scoped global. Use `allow_unstamped_insert()`.
2. **The tenant was lost**, most often because the work was dispatched into another thread. Use [`preserve_tenant_context`](#cross-thread-tenant-propagation) instead. Adding a bypass here would suppress the symptom and reintroduce the exact silent-NULL behaviour the guard exists to stop.

If the failing insert is inside async or off-thread work, assume case 2 until proven otherwise.

### Current use sites

- **Router-side chargen**, `chargen.py`. Vanilla `Account.create_character` inserts the character row while the router is unscoped; the wrapper then stamps it from its start location. The library's only internal use.

Consumers add their own as they find them. FCM's account-bank creation in `at_post_login` is one such site.

### `refresh_from_db()` needs a visibility guard

Django's `Model.refresh_from_db()` routes through `_base_manager` (a plain `models.Manager()` instance), not the model's `.objects`. We only patch `.objects`, so `refresh_from_db()` runs unscoped and will load a foreign-shard row into the in-memory instance without complaint.

Shard-side callers gate it behind an explicit existence check on the patched manager:

```python
if ObjectDB.objects.filter(pk=obj.pk).exists():
    obj.refresh_from_db()
else:
    # Row not visible from this shard — moved or deleted.
    ...
```

Live use site: `make_shard_at_post_login` in [hooks.py:204-237](../src/evennia_shards/hooks.py#L204-L237). Router-side `refresh_from_db()` calls don't need the guard — the router runs unscoped, so `_base_manager` and `.objects` agree.

## Cross-shard handoff

`handoff.py`'s `cross_shard_move(obj, target_shard, target_location_pk)` is the library's primary write path that legitimately changes a row's tenant column. The `__setattr__` immutability check refuses `obj.shard_id = X; obj.save()`, so the write uses `qs.update(shard_id=..., db_location_id=...)` — a single SQL `UPDATE` that bypasses the instance-level immutability machinery entirely. `qs.update` doesn't fire `post_save`, so the FK-dereference into a now-foreign target room never happens.

The write block:

```python
with transaction.atomic():
    objs_updated = ObjectDB.objects.filter(
        pk=obj.pk, shard_id=current_shard,
    ).update(
        shard_id=target_shard,
        db_location_id=target_location_pk,
    )
    # Sync the in-memory instance to match the DB (see compromise #2 below).
    object.__setattr__(obj, "shard_id", target_shard)
    object.__setattr__(obj, "db_location_id", target_location_pk)
    obj.flush_from_cache(force=True)
    # ... bulk-update contents' shard_id ...
```

Validation failures raise `ValueError`.

### Two compromises

`cross_shard_move` carries two intentional rule-breaks. Both are narrow, both are documented inline at the call site, neither hides what it does.

**1. `with shard_context(None):` for the target-shard validation read.** Step 2 of `cross_shard_move` validates that `target_location_pk` exists and is on `target_shard`. That row lives on `target_shard` by definition, and the auto-filter excludes it from any default-scoped query — even `.values_list` (which bypasses `from_db`) is filtered out at the SQL `WHERE` level. So the validation reads inside an unscoped block:

```python
with shard_context(None):
    target_rows = list(
        ObjectDB.objects.filter(pk=target_location_pk)
        .values_list("shard_id", flat=True)[:1]
    )
```

Narrow scope (single query, single column), but it is a deliberate escape from the auto-filter inside a normal write primitive.

**2. `object.__setattr__` to sync in-memory state after `qs.update`.** `qs.update` writes the DB but not the Python instance. The redirect loop two lines later reads `character.shard_id` to stamp tickets — without a sync, tickets get stamped with the pre-move shard. The natural sync (`obj.shard_id = target_shard`) goes through our `__setattr__` wrapper, which flags it as a tenant-column mutation. So we bypass our own wrapper:

```python
object.__setattr__(obj, "shard_id", target_shard)
object.__setattr__(obj, "db_location_id", target_location_pk)
```

The immutability check is meant to catch *accidental* tenant-column mutation. `cross_shard_move` is the one place in the library that legitimately needs to mutate it, and this is the escape hatch. Cleaner long-term designs (dedicated `_post_move_sync` API, or an in-tenancy "scope-escape" context that suppresses the check) are follow-up work — see *Outstanding investigation* below.

### Other steps in the move

`cross_shard_move` also handles target validation (`target_shard` is in `SHARD_URLS`, `target_location_pk` exists on `target_shard` or is global), a session snapshot before the move, recursive inventory collection via `_collect_all_contents`, atomic transaction + idmapper eviction, pre-emptive session detach, per-session redirect via `_redirect_to_character_shard`, and a `flush_from_cache` bus message to the destination shard. Returns a `MoveResult(objects_moved, sessions_redirected, failures)`.

## Cross-shard reads: the load-evict pattern

For operations that need to materialise a foreign-shard row's Python instance and call read-only methods on it (e.g. evaluating a destination room's `teleport_here` lock against a local caller), use a scope switch around the load and an explicit eviction afterwards:

```python
with shard_context(target_shard):
    target = ObjectDB.objects.get(pk=target_pk)
    result = target.some_read_only_method(...)
    target.flush_from_cache(force=True)
```

Four steps:

1. **Scope-switch** to the target shard so the auto-filter admits its rows.
2. **Materialize** the row through the normal ORM. Real Python instance, full Evennia semantics.
3. **Call** the read-only method (`access`, attribute reads, etc.). Pure in-memory; no further DB hit.
4. **Evict** the foreign instance from the idmapper. Without this, the next default-scoped read on the same pk on this process would return the cached foreign instance instead of the auto-filter excluding it. Subtle bug class — always pair the load with the evict.

**Caveats:**

- **Side-effecting lockfuncs:** Most stock lockfuncs (`perm()`, `id()`, `tag()`, `holds()`, etc.) are read-only and scope-safe. Custom lockfuncs that write DB state will write to the target shard inside the `with` block — usually the right thing, but worth knowing.
- **Lockfuncs that themselves query:** A lockfunc that reads the caller's own rows (e.g. `same_guild(caller)` looking up the caller's guild membership) runs under the *target* shard's scope inside the block. If the caller's relevant rows live on the caller's home shard, the lookup will silently see only the target shard's rows. Wrap such cases in a nested `shard_context(caller.shard_id)` if you hit one.
- **Strictly for reads.** Don't use this pattern to *write* to foreign rows — that's what `cross_shard_move`'s `qs.update` path is for. Writes via instance `save()` inside `shard_context(target)` would land on the target, but they'd also fight the tenant-column immutability machinery if the row's `shard_id` is touched. Stay on the read side.

A future helper in `tenancy.py` (e.g. `with_remote_row(pk, shard_id) as obj:`) could encapsulate the load + evict pair so call sites don't have to repeat the eviction step. Worth doing once a second use case lands.

## Cross-thread tenant propagation

`django-multitenant` stores the active tenant in `threading.local`. Code dispatched into a separate thread — Twisted's `deferToThread` / `run_async`, `ThreadPoolExecutor.submit`, `asyncio.to_thread` — runs under a fresh `threading.local` with no tenant set. ORM work inside the worker thread sees `get_current_tenant() is None`: queries run unscoped, and the auto-stamp on insert is skipped (the stamp condition requires both `tenant_value is None` *and* a current tenant set).

For consumer code dispatching ORM work into a worker, use `preserve_tenant_context`:

```python
from evennia_shards import preserve_tenant_context

# Wraps the callable so it carries the current tenant into the worker.
deferToThread(preserve_tenant_context(do_work), *args)
```

Behaviour:

- **Capture happens at wrap time, not at call time.** Wrap and dispatch in the same expression so the captured value is the tenant active on the reactor thread.
- **The wrapped callable sets the captured tenant on entry, restores the worker thread's previous tenant on exit** (which is `None` for a fresh thread, restored as `None`).
- **No-op when no tenant is active at wrap time** — captures `None`, wrapped callable runs unscoped, same as without the wrap.
- **Exception-safe** — the restore runs in a `finally` block.

### Pattern for other Evennia libraries

`preserve_tenant_context` is designed as the canonical integration point for other infrastructure libraries that dispatch ORM work into threads. The library-to-library composition rule: the sibling library try-imports the helper, falling back to an identity passthrough when shards isn't installed:

```python
try:
    from evennia_shards import preserve_tenant_context
except ImportError:
    def preserve_tenant_context(fn):
        return fn
```

Three lines. After that, the sibling library uses `preserve_tenant_context(callable)` at every dispatch point. In a sharded deployment the wrap captures-and-restores tenant; in a non-sharded deployment it's a transparent passthrough. Same code path either way — no `if shards_installed:` branching in the consumer.

Live consumer: [`evennia-world-builder`'s `wb_build` command](https://github.com/FullCircleMUD/evennia-world-builder/blob/main/src/evennia_world_builder/commands.py) wraps its `run_async` pipeline dispatch so worker-thread-created rooms get correctly stamped under multi-shard deployments.

## Known gaps

- **Idmapper cache.** Evennia's `SharedMemoryManager` caches model instances by pk and serves them on `get()` without re-hitting the DB. The cache is implicitly scoped because every populating read goes through the auto-filter — but cross-shard reads via `shard_context()` can poison the cache with foreign instances. Always pair such reads with `flush_from_cache(force=True)`.
- **`TENANT_STRICT_MODE`.** Multitenant supports raising `EmptyTenant` on `_do_update` when no tenant is set; worth enabling in dev/CI to catch "forgot to set tenant context" bugs.

## Outstanding investigation

- Whether Evennia's typeclass-system manager subclassing (the consumer's `Character.objects`, `Room.objects`, etc. via `TypeclassManager`) inherits the patch correctly. Should — the patch is on the manager class, which proxy typeclass managers inherit from — but not yet verified end-to-end.
- Whether `select_related` / `prefetch_related` joins into ObjectDB still scope correctly when the *root* model is also tenant-tagged. Multitenant's filter injection is per-model; nested joins may need explicit handling.
- The interaction with Evennia's existing `select_related` calls in `accounts/manager.py:117` (the partial-match search path).
- Whether the two compromises in `cross_shard_move` (`shard_context(None)` validation read and `object.__setattr__` in-memory sync) want a dedicated "admin/handoff context" API rather than the ad-hoc escapes they are today.
