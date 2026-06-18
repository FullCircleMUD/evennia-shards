# Test results — baseline (vanilla Evennia, library not installed)

**Date:** 2026-04-28
**Command:** `evennia test evennia` from `examples/demo_game/`
**Environment:** `c:\Users\micro\Documents\EvenniaShards\venv\` — Evennia 6.0.0 installed; `evennia_shards` **not** installed.
**Purpose:** Establish the reference result for the Case 1 verification gate. Subsequent runs (with the library installed) must produce an identical result for the gate to pass.

## Result

```
Ran 1662 tests in 797.796s
FAILED (errors=2, skipped=38)
```

## Errors

Both errors are missing optional third-party dependencies of Evennia contribs. Neither involves `evennia_shards` (which is not on the path). They are environmental, not signal:

- **`evennia.contrib.grid.xyzgrid`** — `ImportError: No module named 'numpy'`. The XYZgrid contrib requires `scipy` (and transitively `numpy`).
- **`evennia.contrib.utils.git_integration`** — `ModuleNotFoundError: No module named 'git'`. The git integration contrib requires `GitPython`.

## Interpretation

For the Case 1 gate, the absolute pass count does not matter — what matters is the **delta** when the library is added. This run is the reference. A subsequent run with `evennia_shards` installed must produce the same 1662 / 2 errors / 38 skipped to satisfy the gate.
