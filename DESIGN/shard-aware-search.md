# Shard-aware global search

The substitute for `caller.search(name, global_search=True)` in code that runs on a sharded process. Returns enough metadata for the caller to decide whether to use a loaded instance (local match) or route via cross-shard primitives (foreign match), without ever instantiating a foreign row.

## The problem

Vanilla `caller.search(name, global_search=True)` walks Evennia's search machinery against the whole `ObjectDB` table. Match resolution returns instances. On a sharded process, instantiating any row whose `shard_id` doesn't match the current process trips the `from_db` chokepoint with `ShardIsolationError`. The call site does not get an opportunity to inspect the match before the chokepoint fires.

Anywhere a consumer (or the library itself) needs to find an object by name across the whole game world — admin commands, recall spells, `look <character>`, future cross-shard tells / who / where features — the same trip is waiting.

## The shape

```python
from evennia_shards import shard_aware_global_search, ShardSearchResult

result: ShardSearchResult = shard_aware_global_search(
    caller,
    name,
    tag=None,           # optional
    tag_category=None,  # optional, only consulted if tag is set
)
```

Inputs:

- `name` — dbref (`"#42"`) or case-insensitive exact `db_key`.
- `tag` (optional) — narrow the lookup to objects carrying this tag. When set, only rows with a matching tag participate in the search. Useful for scoping a key namespace to a smaller domain (e.g. a zone) so the same key can be reused without ambiguity across that domain.
- `tag_category` (optional) — only consulted when `tag` is set. When omitted, any category for the given tag key matches.

One output: a `ShardSearchResult` dataclass.

`ShardSearchResult` fields:

| Field | Type | Populated when |
|---|---|---|
| `state` | `"found"` / `"not_found"` / `"multiple"` | always |
| `obj` | loaded `ObjectDB` instance or `None` | `state == "found"` and match is local |
| `pk` | int | `state == "found"` |
| `shard_id` | str | `state == "found"` |
| `db_key` | str | `state == "found"` |
| `candidates` | list of `(pk, shard_id, db_key)` triples | `state == "multiple"` |
| `is_local` | bool (property) | derived from `shard_id` and `get_shard_id()` |
| `is_cross_shard` | bool (property) | derived: `state == "found"` and not `is_local` |

Three states drive caller dispatch:

- **`found`** — exactly one match. If `is_local`, `obj` is the loaded instance and the caller can use it as it would a vanilla search result. If `is_cross_shard`, `obj` is `None`; the caller routes via `cross_shard_move` (or another cross-shard primitive) using `pk` and `shard_id`.
- **`not_found`** — no match. The caller emits its own not-found message.
- **`multiple`** — more than one match. `candidates` is populated so the caller can render its own disambiguation prompt with shard context (e.g. "found `room` on shard0 (#5) and shard1 (#2238) — specify by dbref").

## The mechanism

Two-step lookup:

1. **SQL-level metadata query.** `ObjectDB.objects.filter(...).values_list("pk", "shard_id", "db_key")` against the matching rows. The filter chain composes whatever was supplied: `db_key` (or `pk` for dbref input), and — when set — `db_tags__db_key` / `db_tags__db_category` for tag scoping. All predicates AND into one SQL WHERE; the join through the m2m tag table is handled by Django. This step returns column data only — no `from_db` is called per row, so the chokepoint is not invoked.
2. **Conditional instantiation.** Once the match's shard is known:
   - If the match is local (`shard_id == get_shard_id()` or `shard_id == "*"`), load the instance via the regular ORM (`ObjectDB.objects.get(pk=...)`). The chokepoint sees a local row and allows the load.
   - If the match is on another shard, the helper returns the metadata only.

The chokepoint never fires inside the helper. The caller never receives a cross-shard instance.

### Why tag scoping and shard scoping compose

Tag filtering narrows by the m2m join through `db_tags`; shard filtering would narrow by the `shard_id` column on `ObjectDB`. The two are orthogonal — different columns / joins — and Django ANDs them into the same SQL WHERE. Either can be added independently; combining both just narrows the candidate set further. Every row that survives the filter still carries `shard_id`, so the helper's routing logic (`is_local` / `is_cross_shard`) works unchanged regardless of how many filters were applied.

In FCM's design, a zone never spans shards — so when the consumer scopes by zone tag, the tag scope effectively pre-scopes by shard too (every zone-tagged row will have the zone's owning shard_id). The helper still reports the shard_id explicitly, so callers don't have to know that invariant to dispatch correctly.

## Consumer usage pattern

The helper is the recommended substitute for any `caller.search(name, global_search=True)` call site in code that runs on a sharded process. The substitution is one-for-one — vanilla's three search calls in `CmdTeleport.parse`, for example, become three calls to `shard_aware_global_search` in `ShardAwareCmdTeleport.parse` (see [library-integration-risks.md](library-integration-risks.md) § `CmdTeleport` for the worked example).

## Scope

The current implementation handles:

- dbref lookups (`"#42"`).
- Case-insensitive exact `db_key` matches.
- Optional tag scoping via `tag` / `tag_category` to narrow the key namespace (e.g. zone-scoped lookup).

The current implementation does not handle:

- Alias matching.
- Partial / fuzzy name matches.
- Special tokens like `"here"`, `"me"`, `"self"`.
- Locality-aware preference ordering when both local and remote matches exist for the same name.

These are extensions that fit cleanly under the same call signature when a consumer needs them. The helper's contract — name (plus optional tag) in, `ShardSearchResult` out — is stable.

## Related

- [`library-integration-risks.md`](library-integration-risks.md) § `CmdTeleport` — the first consumer of this helper.
- [`shard-isolation.md`](shard-isolation.md) — the chokepoint architecture this helper is designed to navigate.
