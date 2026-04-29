# Progress

A running log of high-level milestones as the project moves from design into build. Each entry is a brief note pointing to whatever artefact (test result, design doc, code change) is the evidence for that milestone. New entries go at the top.

This is not a changelog (use `git log` for that) and not a roadmap (the phasing lives in [archive/evennia-shards-HANDOVER.md](archive/evennia-shards-HANDOVER.md#phased-poc-plan)). It is a thin index of "what has actually happened so far."

## Milestones

### 2026-04-29 — Bespoke spike chokepoints 1 & 2 land with isolated tests

The `bespoke` branch now carries the first two of the four chokepoints documented in [shard-isolation.md](shard-isolation.md), with full automated test coverage:

- **`pre_save` chokepoint** (commit `80226be`): the existing auto-stamp handler grew a second arm — refuse the save if `instance.shard_id` is set and is neither the current shard nor `"*"`. New `ShardIsolationError` exception type. Live smoke test confirmed via in-game `@py` (the chokepoint raised with the expected stack trace pointing at the offending caller).
- **`pre_delete` chokepoint** (this commit): mirrors `pre_save` minus the auto-stamp arm. Refuses to delete a row whose `shard_id` is neither current nor `"*"` (and not `None`, since legacy/unstamped rows are tolerated). Covers both `instance.delete()` and `qs.delete()` because Django fires `pre_delete` per affected row even on bulk queryset deletes.

**Test infrastructure decoupled from `examples/demo_game/`:** `tests/test_settings.py` + `runtests.py` run the suite against an in-memory sqlite database with `evennia_shards` in `INSTALLED_APPS`, using `BaseEvenniaTestCase` to force `evennia.game_template.*` fallbacks. No gamedir needed. See [testing-setup.md](testing-setup.md). 13 tests passing (3 config + 1 app setup + 4 pre_save + 5 pre_delete).

**What this proves:**

- Both write-side chokepoints function exactly as designed in `shard-isolation.md`.
- The single `pre_delete` handler covers both delete entry points (instance + queryset) without a separate qs.delete override, confirming Django's per-row signal dispatch on bulk deletes.
- The library has a deterministic, hermetic test suite that runs in under half a second.

**What this does *not* prove** (next steps in the spike):

- `from_db` override (chokepoint 3) — the read-side guard.
- `QuerySet.update()` override (chokepoint 4) — the bulk-update guard.
- Cross-shard ownership handoff and the bypass primitive.

### 2026-04-29 — Auto-stamp on save works (hybrid pre_save signal)

A pre_save signal handler in `EvenniaShardsConfig.ready()` now stamps `shard_id` to the current process's `SHARD_ID` whenever an `ObjectDB` (or subclass) is saved with `shard_id == None`. Explicit values (e.g. those set during a cross-shard handoff) are respected. Verified end-to-end: after a clean DB wipe + `evennia migrate` + `evennia start`, the bootstrap rows (`#1` superuser character, `#2` Limbo) and a runtime-dug `test` room (`#3`) all reported `shard_id = 'shard0'` via both the ORM and a raw SQL probe.

**Key implementation finding** (worth recording): Evennia's typeclass system uses concrete Django subclasses of `ObjectDB` — `Room`, `Character`, `Exit`, and consumer-defined typeclasses — that all share the `ObjectDB` table. Django dispatches `pre_save` with `sender = type(instance)`, which is the subclass, never the `ObjectDB` base. A naïve `pre_save.connect(handler, sender=ObjectDB)` therefore matches *zero* saves of game-world objects. The fix is to connect without a sender filter and do an `isinstance(instance, ObjectDB)` check inside the handler. Performance cost of the universal handler is negligible (microseconds per save).

**What this proves:**

- Auto-population works for both bootstrap-time saves (via `at_initial_setup`) and runtime saves (via `dig` or any `create_object` path).
- The "if shard_id is None" guard is load-bearing: it lets explicit consumer/library code (cross-shard handoff, central seed scripts) set values that the signal will respect.
- Lazy-backfill side effect: legacy NULL rows would auto-populate on their next save, useful for monolith-to-shard adoption but not a substitute for an explicit migration backfill.

**What this does *not* prove** (next spikes):

- Backfill of pre-existing rows that never save again (the explicit `RunPython` migration is still required for that).
- Auto-filtering manager composition with Evennia's `SharedMemoryManager` (idmapper) — the next big architectural unknown.
- Cross-shard `UPDATE` semantics during handoff.

### 2026-04-29 — Migration spike confirmed: `shard_id` column on `ObjectDB` is viable

A small spike proved the foundational partitioning mechanism. Library now ships an `apps.py` AppConfig and a `0001_add_shard_id_to_objectdb` migration; in shard mode the demo game adds `evennia_shards` to `INSTALLED_APPS` via a one-line conditional in `settings.py`. After `evennia migrate`, an in-game `@shard_check` command confirmed both ORM-level (`ObjectDB._meta` knows the field) and database-level (raw `SELECT shard_id` returns) presence of the column on existing rows.

**What this proves:**

- A library-shipped Django migration can add a column to Evennia's `ObjectDB` table via `RunSQL`, anchored to Evennia's own migration history.
- `add_to_class` from `AppConfig.ready()` makes the new field visible to the ORM without a model fork.
- The library can be a Django app conditionally (only when `SHARDS_ROLE != "monolith"`), and the cross-app migration sequencing under `evennia migrate` works without bespoke command flow.
- Consumer adoption is three lines in `settings.py` (`SHARDS_ROLE`, `SHARD_ID`, conditional `INSTALLED_APPS`).

**What this does *not* prove** (next spikes):

- Auto-population of `shard_id` on object creation (pre_save signal mechanism untested).
- Backfill of pre-existing rows (`#1` superuser, `#2` Limbo currently `NULL`).
- Auto-filtering manager composition with Evennia's `SharedMemoryManager` (idmapper).
- Cross-shard `UPDATE` semantics during handoff.

### 2026-04-28 — Case 1 gate re-run with first library code (still satisfied)

Re-ran `evennia test evennia` after the `config.py` accessors landed. Result identical to the previous gate run: 1662 / 2 errors / 38 skipped, same two errors (both missing optional Evennia contrib dependencies, unrelated to evennia-shards). The library's first real code is provably non-perturbing of Evennia's test suite. See [test-history/test_results_3_2026-04-28.md](test-history/test_results_3_2026-04-28.md).

### 2026-04-28 — Config accessor wire proven (live, in-game)

First piece of real library code: [evennia_shards/config.py](../evennia_shards/config.py) with `get_role()` / `get_shard_id()` accessors. Settings design documented in [shard-settings.md](shard-settings.md) and load-bearing principle 9 added to [CLAUDE.md](../CLAUDE.md). Wire proven end-to-end with a temporary `@shards_debug` superuser command in the demo game (since reverted): both accessors return the documented defaults when the consumer declares nothing, and return the consumer-declared values when overridden. See [test-history/test_results_2_2026-04-28.md](test-history/test_results_2_2026-04-28.md). Case 1 gate re-run with the new library code is still outstanding.

### 2026-04-28 — Case 1 gate satisfied (empty-library state)

Re-ran `evennia test evennia` with `evennia_shards 0.0.1` installed (`pip install -e .` from repo root). Result identical to baseline: 1662 / 2 errors / 38 skipped, same two errors. Zero delta — the library is genuinely dormant in monolith mode at its current (empty) state. Gate must be re-run after each future change that could execute at import or app-ready time. See [test-history/test_results_1_2026-04-28.md](test-history/test_results_1_2026-04-28.md).

### 2026-04-28 — Baseline test run (vanilla Evennia)

Ran `evennia test evennia` against vanilla Evennia 6.0.0 (with `evennia_shards` *not* installed) to establish the reference for the Case 1 verification gate. Result: 1662 tests, 2 errors (both missing optional contrib dependencies — `xyzgrid` needs scipy, `git_integration` needs GitPython), 38 skipped. See [test-history/test_results_0_2026-04-28.md](test-history/test_results_0_2026-04-28.md).
