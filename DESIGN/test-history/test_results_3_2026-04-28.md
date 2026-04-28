# Test results — Case 1 gate re-run (after config.py landed)

**Date:** 2026-04-28
**Command:** `evennia test evennia` from `examples/demo_game/`
**Environment:** `c:\Users\micro\Documents\EvenniaShards\venv\` — Evennia 6.0.0 and `evennia_shards 0.0.1` (editable). Library now contains [config.py](../../evennia_shards/config.py) with the `get_role()` / `get_shard_id()` accessors.
**Purpose:** Re-run the Case 1 gate after the first piece of real library code landed. Confirm the library is still dormant — the new code is read-only Django settings access via `getattr` and is not imported by Evennia's test suite, so the result should be identical to [test_results_1_2026-04-28.md](test_results_1_2026-04-28.md).

## Result

```
Ran 1662 tests in 807.292s
FAILED (errors=2, skipped=38)
```

## Comparison to previous gate run

| | Previous (empty library) | This run (with `config.py`) | Delta |
|---|---|---|---|
| Tests run | 1662 | 1662 | 0 |
| Errors | 2 (`xyzgrid`, `git_integration`) | 2 (`xyzgrid`, `git_integration`) | 0 |
| Skipped | 38 | 38 | 0 |
| Wall time | 806.9s | 807.3s | +0.4s (noise) |

Both errors are the same missing optional contrib dependencies as before (`numpy`/`scipy` for xyzgrid, `GitPython` for git_integration). Zero shards-attributable change.

## Conclusion

Case 1 gate satisfied. The library's first real code does not perturb Evennia's own test suite. The gate must be re-run after each future addition that could execute at import or app-ready time.
