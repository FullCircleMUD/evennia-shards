# Test results — with `evennia_shards` installed (Case 1 gate)

**Date:** 2026-04-28
**Command:** `evennia test evennia` from `examples/demo_game/`
**Environment:** `c:\Users\micro\Documents\EvenniaShards\venv\` — Evennia 6.0.0 and `evennia_shards 0.0.1` (editable, `pip install -e .` from repo root) both installed.
**Purpose:** Case 1 verification gate. Confirm that installing the library does not perturb Evennia's test suite vs. the baseline in [test_results_0_2026-04-28.md](test_results_0_2026-04-28.md).

## Result

```
Ran 1662 tests in 806.874s
FAILED (errors=2, skipped=38)
```

## Comparison to baseline

| | Baseline (no library) | With library | Delta |
|---|---|---|---|
| Tests run | 1662 | 1662 | 0 |
| Errors | 2 (`xyzgrid`, `git_integration`) | 2 (`xyzgrid`, `git_integration`) | 0 |
| Skipped | 38 | 38 | 0 |
| Wall time | 797.8s | 806.9s | +9s (noise) |

Both errors are the same missing optional contrib dependencies as in the baseline (`numpy`/`scipy` for xyzgrid, `GitPython` for git_integration). No new errors, no errors disappeared, no skip-count change.

## Conclusion

Case 1 gate satisfied for the library's current (empty) state. The library is genuinely dormant when installed but unconfigured.

This gate must be re-run after each future change that could execute at import or app-ready time (e.g. `INSTALLED_APPS` registration, signal handlers, `AppConfig.ready()` hooks).
