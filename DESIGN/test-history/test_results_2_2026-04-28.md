# Test results — config accessor wire proven (live, in-game)

**Date:** 2026-04-28
**Method:** Interactive smoke test inside a running demo game.
**Purpose:** Prove the library's `get_role()` / `get_shard_id()` accessors correctly read `SHARDS_ROLE` / `SHARD_ID` from Django settings, both when defaulted and when explicitly overridden.

## What was exercised

- Library code: [evennia_shards/config.py](../../evennia_shards/config.py) — `get_role()`, `get_shard_id()`.
- Throwaway probe: a temporary `@shards_debug` superuser command in the demo game (since reverted) that prints both accessor values to the caller.

## Run 1 — defaults (no settings declared)

Demo game's [settings.py](../../examples/demo_game/server/conf/settings.py) declared neither `SHARDS_ROLE` nor `SHARD_ID`.

Command output:

```
SHARDS_ROLE: 'monolith'
SHARD_ID:    None
```

Confirms `getattr(settings, "SHARDS_ROLE", DEFAULT_ROLE)` falls back correctly and `getattr(settings, "SHARD_ID", None)` likewise.

## Run 2 — explicit overrides

Demo game's [settings.py](../../examples/demo_game/server/conf/settings.py) declared:

```python
SHARDS_ROLE = "shard"
SHARD_ID = "shard0"
```

After `evennia reload`, command output:

```
SHARDS_ROLE: 'shard'
SHARD_ID:    'shard0'
```

Confirms the accessors return the consumer-declared values when the consumer overrides them.

## Conclusion

End-to-end wire proven: a value declared in the consumer's `settings.py` reaches library code via the accessors, and an undeclared value falls back to the documented default. The settings design described in [shard-settings.md](../shard-settings.md) is operational.

## Outstanding follow-ups (not part of this test)

- Revert the demo game changes used to enable this test: the `SHARDS_ROLE` / `SHARD_ID` lines in `settings.py`, the `CmdShardsDebug` class in `commands/command.py`, and the registration in `commands/default_cmdsets.py`.
- Re-run the Case 1 gate (`evennia test evennia` from `examples/demo_game/`) to confirm the new library code does not perturb Evennia's own test suite.
