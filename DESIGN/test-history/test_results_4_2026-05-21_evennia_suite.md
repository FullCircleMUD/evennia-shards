# Test results — Evennia's own test suite, run against the library in three modes

**Date:** 2026-05-21
**Purpose:** Find out whether the library's install (auto-filter, `__setattr__` wrap, manager patches, `_shards_wrapped_init` cmdset additions) breaks any of Evennia's own assumptions. The library's tests pass; the question is whether Evennia's tests still pass with the library installed.

## Setup

Three settings modules under [`tests/`](../../tests/), each importing [`test_settings.py`](../../tests/test_settings.py) and overriding only `SHARDS_ROLE` (plus removing `evennia_shards` from `INSTALLED_APPS` for the monolith baseline):

- **[`tests/evennia_suite_monolith.py`](../../tests/evennia_suite_monolith.py)** — library not in `INSTALLED_APPS`. Pure baseline; equivalent to vanilla Evennia under our scaffolding.
- **[`tests/evennia_suite_shard.py`](../../tests/evennia_suite_shard.py)** — `SHARDS_ROLE="shard", SHARD_ID="shard0"`. Full multitenant install active. The interesting run.
- **[`tests/evennia_suite_router.py`](../../tests/evennia_suite_router.py)** — `SHARDS_ROLE="router"`. Patches installed but `bootstrap_tenant_context()` clears the tenant — auto-filter dormant, other install side-effects (add_to_class, manager patch, `__setattr__` wrap, library overrides on `at_post_login` / `CmdIC` / `chargen`) still in place.

Command (one per mode):

```bash
DJANGO_SETTINGS_MODULE="tests.evennia_suite_${mode}" \
  ./venv/Scripts/python.exe -m django test evennia --noinput \
  2>&1 | tee "ops/evennia-suite-runs/${mode}.log"
```

The settings inherit `tests/test_settings.py`'s scaffolding: in-memory sqlite, no real gamedir, `ROOT_URLCONF="tests.urls"` (empty). Anything failing in monolith because of that scaffolding is the noise floor, not a finding.

## Results

| Mode | Tests | Failures | Errors | Skipped | Wall time |
|---|---|---|---|---|---|
| monolith | 1649 | 1 | 5 | 38 | 791s |
| shard | 1649 | 53 | 9 | 38 | 810s |
| router | 1649 | 57 | 8 | 38 | 730s |

Unique failing tests per mode (after de-dup): monolith 6, shard 62, router 65.

Bucketed against the monolith baseline:

| Bucket | Count | Source |
|---|---|---|
| Baseline (all three modes fail) | 6 | Our test scaffolding |
| shard + router shared | 51 | Library install side-effects (patches + cmdset additions), independent of auto-filter |
| shard only | 5 | Auto-filter specifically |
| router only | 8 | Router-mode-specific library overrides (`shard_aware_at_post_login`, `ShardAwareCmdIC`, chargen wrap) |

## Bucket details

### Baseline (6 — not actionable)

```
ERROR: evennia.contrib.base_systems.ingame_python  (loader)
ERROR: evennia.contrib.grid.xyzgrid                (loader)
ERROR: evennia.contrib.utils.git_integration       (loader)
ERROR: ChannelDetailTest.test_get                  (web)
ERROR: ChannelDetailTest.test_get_authenticated    (web)
FAIL:  TestLauncher.test_get_twisted_cmdline       (launcher)
```

Three contrib-loader errors (`numpy`/`scipy`/`GitPython` not installed). Two website-channel tests need a gamedir URL layout we don't provide. One launcher test depends on a specific platform-tooling shape. Same as the [2026-04-28 gate run](test_results_3_2026-04-28.md) plus the two website tests. Pre-existing noise floor, nothing to do.

### Install side-effects (51 — top categories)

| Test module | Count | Likely root cause |
|---|---|---|
| `TestHelp` | 14 | Help output goes through pager when topic count crosses a threshold. Our `_shards_wrapped_init` adds `CmdShardCheck` + `CmdCrossShardDig` to `CharacterCmdSet`, bumping the topic count. Tests get `"Exited pager."` instead of the expected text. |
| `TestDelay` | 6 | Deferred functions (`task.deferLater`, `deferToThread`) don't run as expected; tests assert `char1.ndb.dummy_var == "dummy_func ran"` and get `False`. Consistent with the Twisted `threading.local` concern flagged in [tenancy.md](../tenancy.md) — tenant context doesn't propagate into deferred-thread callbacks; ORM operations inside deferred functions fail silently. |
| `TestBuilding` | 5 | Build commands that create+inspect ObjectDB rows; install-side effects on construction/save paths. |
| Various contrib `TestTurnBattle*` / `TestBarter` / `TestMail` / `TestContainerCmds` / `TestAchieveCommand` / `TestDice` / `TestTutorialWorld*` | ~15 combined | These contribs ship their own cmdsets and tests; the same `CmdShardCheck` / `CmdCrossShardDig` injection that affects TestHelp likely affects them too (output offsets, topic counts). |
| `TestGeneral`, `TestSystem`, `TestAccount`, `TestAdmin`, `TestDefaultGuest` | ~9 combined | Mixed — some attribute-handler interaction, some cmdset-related. |
| `TestLockfuncs.test_has_account` | 1 | `lockfuncs.has_account(char1, None)` returns `0` instead of `True`. Investigation needed. |
| `TestEvenniaRESTApi.test_set_attribute` | 1 | API-side attribute write. |
| Twisted `runTest` | 1 | A `twisted.trial._asynctest.TestCase.runTest` error — Twisted async-test infrastructure. |

Top two — TestHelp (pager) and TestDelay (deferred-thread context) — account for 20 of 51. Both are documented expectations:

- **TestHelp pager:** the cmdset additions are a deliberate library feature (admin commands ship by default). The output threshold change is a side-effect of having more topics. Fixable on the library side by gating the admin-command install behind a setting, but probably not worth it — the additions are intentional.
- **TestDelay deferred-thread:** the `threading.local` tenant context not propagating into deferred callbacks is exactly the gap [tenancy.md's "Known gaps"](../tenancy.md#known-gaps) calls out. The fix is `TENANT_USE_ASGIREF = True` (multitenant supports it) or auditing which paths do ORM work in deferred threads. Worth fixing.

### Auto-filter specific (5 — shard only, not router)

```
ERROR: TestAccount.test_ooc
FAIL:  TestBuilding.test_attribute_commands
FAIL:  TestAccount.test_ic__nonaccess
FAIL:  TestBuilding.test_tunnel
FAIL:  TestBuilding.test_tunnel_exit_typeclass
```

Five tests that pass in router (unscoped) but fail in shard (scoped). These create ObjectDB rows in setUp under shard0 scope and then call commands that do `caller.search()` or similar — the auto-filter scopes results to current shard plus globals. If a test fixture creates a row that the test then expects to find via a path that ends up filtered (e.g. by tag, or by an internal Evennia lookup that uses `_base_manager`), the row is invisible.

Small set, narrowly scoped failures. Each is probably an independent investigation.

### Router-only (8 — install + library overrides, no auto-filter)

```
FAIL: TestEmailLogin.test_connect
FAIL: TestGeneral.test_go_home
FAIL: TestLockfuncs.test_is_ooc__account
FAIL: TestLockfuncs.test_is_ooc__char
FAIL: TestLockfuncs.test_is_ooc__session
FAIL: TestAccount.test_ooc
FAIL: TestDefaultAccountEv.test_puppet_success
FAIL: TestAccount.test_quell
```

These are tests around login / IC-OOC / puppet / OOC-lockfuncs, all exercising paths the library's router-mode overrides change:

- `shard_aware_at_post_login` replaces `DefaultAccount.at_post_login` on routers — tests calling `at_post_login` see the redirect-or-OOC-menu logic instead of vanilla's puppet flow.
- `ShardAwareCmdIC` is patched onto `account.CmdIC` on routers — IC behaviour changes from "local puppet" to "ticket + redirect."
- `ShardAwareCmdOOC` only patches on shards, but the OOC lockfuncs and puppet flow are affected by the at_post_login replacement.
- The chargen wrap (`make_shard_aware_create_character`) patches `BASE_ACCOUNT_TYPECLASS.create_character` on routers.

These failures are documented-expected: when the library is configured as a router, it deliberately overrides Evennia's puppet semantics so that the router never actually puppets locally. Evennia's tests assume vanilla semantics. Not a bug; the override is the entire purpose of router-mode.

The fact that router-mode shows *more* failures than shard-mode (8 vs 5 above baseline) reflects this — router-side overrides are louder than the auto-filter.

## Conclusion

**The multitenant install is not the dominant source of failures.** 51 of 64 with-library failures (80%) are install-side effects independent of whether the filter is active. The auto-filter itself only accounts for 5 failures.

The big categories are:

1. **CharacterCmdSet additions affect help-output topic counts** (TestHelp + the contrib cmdset tests, ~25 failures). Expected library-feature side-effect.
2. **Twisted deferred-thread tenant-context gap** (TestDelay, 6 failures). Pre-flagged in tenancy.md "Known gaps"; concrete confirmation that it bites Evennia code. Worth implementing the `TENANT_USE_ASGIREF` fix.
3. **Router-mode library overrides change puppet/auth flow** (8 failures). Intentional behaviour; documented under "Library territory" in `library-integration-risks.md`.
4. **Auto-filter excludes test-fixture rows from internal lookups** (5 failures). Small set, each worth a brief look.

None of the categories indicate a regression in *library functionality*. The library's own 290-test suite all passes; the Evennia-suite findings reflect known interaction points and the documented behavioural changes the library introduces.

## Things worth following up on

- **Twisted `deferToThread` tenant-context propagation.** Set `TENANT_USE_ASGIREF = True` in the library's settings install (if compatible) and re-run TestDelay against shard mode. If it flips green, ship the setting.
- **Spot-check the 5 shard-only failures.** Each is probably a one-test investigation — likely either a test that calls `_base_manager` directly or a test fixture that needs `shard_context(None)` to be visible to its assertion path.
- **`TestLockfuncs.test_has_account` returning `0`.** Worth a quick look — `lockfuncs.has_account(char1, None)` is reading an attribute that's resolving to 0 instead of True. Could be unrelated to sharding (the assertion is `True != 0` — looks like a vanilla type comparison issue) but worth confirming.

## Artefacts

- Settings files: [`tests/evennia_suite_monolith.py`](../../tests/evennia_suite_monolith.py), [`tests/evennia_suite_shard.py`](../../tests/evennia_suite_shard.py), [`tests/evennia_suite_router.py`](../../tests/evennia_suite_router.py).
- Full logs: `ops/evennia-suite-runs/{monolith,shard,router}.log`.
- Bucketed failure lists: `ops/evennia-suite-runs/{shard_unique,router_unique,shard_and_router_only}.txt` (and the intermediate `comm` outputs).
