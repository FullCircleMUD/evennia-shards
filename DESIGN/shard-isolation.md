# Shard Isolation Mechanism

How the library enforces the partition between shards at the Django/Evennia level — what stops one shard's process from accidentally reading, instantiating, or writing to another shard's rows.

## Invariants

The library enforces two invariants:

1. **A non-owning shard never instantiates an object it doesn't own.** No remote-shard `ObjectDB` row may be constructed as a Python instance on this shard. (The "owner" of a row is the shard whose `SHARD_ID` matches the row's `shard_id` column. The sentinel `"*"` denotes a row owned by all shards — see [shard-settings.md](shard-settings.md).)
2. **A non-owning shard never persists changes to an object it doesn't own.** No save, update, or delete may commit to a remote-shard row.

These together prevent cache poisoning, cross-shard data corruption, and silent state divergence.

## The four chokepoints

| # | Hook | Covers | Rule |
|---|---|---|---|
| 1 | **`Model.from_db()` override on `ObjectDB`** | All read paths that construct an instance from DB row data — normal queryset iteration, `raw()`, `select_related()`. Verified in Django source: three call sites, all use this method. | Refuses construction when `row.shard_id != current_shard` (and isn't `"*"`). |
| 2 | **`pre_save` signal handler on `ObjectDB`** | Every `instance.save()` (whether triggered by code, typeclass factory, or post-load resave). | Refuses if `instance.shard_id != current_shard`. *(The same signal also runs the auto-stamp logic for new rows where `shard_id is None`; the two behaviours coexist via the same handler.)* |
| 3 | **`pre_delete` signal handler on `ObjectDB`** | Both `instance.delete()` and `qs.delete()` — Django fires `pre_delete` per affected row, even for queryset bulk deletes (it has to, for cascade handling). | Refuses if `instance.shard_id != current_shard`. |
| 4 | **`QuerySet.update()` override** *(in a thin custom QuerySet on the `ObjectDB` manager)* | Queryset bulk updates — the one write operation Django does **not** fire signals for. | Refuses if the queryset would touch any row whose `shard_id` is neither current nor `"*"`. Loud failure, consistent with the other three chokepoints. |

Plus, for ownership handoff (future): `instance.flush_from_cache()` — Evennia's idmapper exposes this; the handoff protocol will call it on the source shard after writing the new ownership.

## Why this set is sufficient

- **Reads are exhaustive.** Every Django read path that produces a `Model` instance from a row goes through `from_db()`. Verified by source inspection — three call sites (`ModelIterable`, `RawModelIterable`, `RelatedPopulator`), no others.
- **Writes are exhaustive.** Instance saves fire `pre_save`; instance deletes fire `pre_delete`; queryset deletes fire `pre_delete` per row; queryset updates are caught by the QuerySet override.
- **Each chokepoint is a low-level Django-native extension point.** No idmapper subclassing, no broad manager replacement that filters every query, no monkey-patching beyond `add_to_class` and signal connections.
- **Failure mode is "raise and surface,"** not silent skip. If any chokepoint fires, it means a leak vector reached a place it shouldn't have — the stack trace points at the calling code so the bug can be fixed.

## What is *deliberately* not enforced

The chokepoints cover **instantiation, persistence, and per-row mutation**. They do not cover scalar SQL inspection or row-data extraction that never builds an instance:

- **Aggregate operations** (`count()`, `exists()`, `aggregate(...)`) return cross-shard answers. They don't construct instances or modify data; they return scalars. Wrong-but-not-damaging. Consumers should be aware that "how many characters exist in this table?" is a global question.
- **`.values()` / `.values_list()`** return dicts/tuples of column data directly from rows, never going through `from_db`. So `Character.objects.values("db_key", "shard_id", "db_location_id")` happily pulls field data for remote rows. No instance is constructed and nothing is persisted, so neither invariant is violated — but consumers should know that row-data inspection is a global question, the same way aggregates are.
- **Raw cursor SQL** (`with connection.cursor() as cur: cur.execute(...)`) bypasses every chokepoint by definition. Discipline-dependent — the same as it would be in any Django app. Documented as a consumer responsibility.
- **Pickling/unpickling typeclass instances** bypasses `from_db`. Rare in practice; not currently a concern. Can be addressed if it ever surfaces.

Worth disambiguating: **`Manager.raw()` *is* covered** — its iteration goes through `RawModelIterable`, which is one of `from_db`'s three call sites. People sometimes lump `raw()` with cursor SQL, but Django actually hooks raw queries through the same construction path.

The general framing: the library prevents **cache poisoning, cross-shard data corruption, and silent state divergence**. It does not provide an information wall — a shard process can still ask scalar or row-data questions about rows it doesn't own. If we wanted information walls we'd be in schema-based multi-tenancy territory.

## How this meets the architectural goals

- **Library does nothing in monolith mode.** Hooks register only when the library is in `INSTALLED_APPS`, which the consumer's settings only do when `SHARDS_ROLE != "monolith"`.
- **No surprising consumer-side behaviour change.** Normal Evennia code — `obj.save()`, `obj.delete()`, `Character.objects.get(pk=42)`, FK lookups — works exactly as before in monolith and works correctly-scoped in shard mode.
- **Targeted enforcement.** Each chokepoint solves one specific concern without affecting unrelated behaviour. Aggregates and reads of single rows on the current shard pay zero overhead.
- **Clear failure mode.** Invariant violations raise visibly rather than silently producing wrong data — easy to find and fix during development.
