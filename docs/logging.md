# Logging

How the library emits durable log output, separate from operator-facing messaging and from Evennia's generic `server.log`.

The library writes its own log file, `shards.log`, co-located with Evennia's standard logs (the `LOG_DIR` configured by the consumer's gamedir). All library code that needs to record a durable event routes through a single helper, `shard_log`, which wraps Evennia's built-in `evennia.utils.logger.log_file()`. Outside an Evennia engine (tests, any future CLI), the helper is a silent no-op.

This mirrors the shims in the sibling libraries [`evennia-world-builder`](../../evennia-world-builder/docs/logging.md) and [`evennia-mob-spawner`](../../evennia-mob-spawner/docs/logging.md). The rationale below is the same; only the two divergences are specific to this library.

## Why a dedicated log file

Two distinct output channels that should not be confused:

1. **Operator-facing messages.** `@tel` failures, admin command output, redirect notices. Ephemeral, addressed to the human triggering the command — they belong on the operator's terminal, not in a log file.
2. **Durable forensic records.** Refused NULL-shard inserts, cross-shard move failures, message-bus drops and timeouts, chargen stamping failures, script-confinement contradictions, at_post_login override warnings. These need to survive the operator session: read later, to answer "why did that mob never spawn" or "what happened to that character during the handoff."

Without a dedicated file, the only durable record is whatever Twisted dumps into `server.log` when an exception escapes — a thin slice of what's worth recording, mixed with everything else Evennia is logging. A dedicated `shards.log` lets an operator tail one file and grep its history without sifting Evennia noise.

## One mechanism, no exceptions

Every durable record goes through `shard_log`. Library code does not call `evennia.utils.logger` directly, and does not use Python's `logging` module at all.

Two mechanisms would mean no single file holds the library's story, and that records reaching Python's `logging` hierarchy can be silently rerouted or filtered by a consumer's logging configuration without the library knowing. The one place `logger` is touched directly is inside the shim itself.

The trade is that a consumer cannot route library events into their own `logging` hierarchy. Both sibling libraries treat that as the point rather than a loss. A bridge could be added later if someone asks for one.

## Why `evennia.utils.logger.log_file`

Evennia's `log_file(msg, filename="shards.log")` already handles every concern a custom logger would have to solve:

- Writes into `settings.LOG_DIR` — same directory as `server.log` and `portal.log` — without the library hard-coding a path.
- Thread-safe via Evennia's interruptable thread pool, so worker threads can call it without locking concerns.
- No dependency on Python's `logging` module hierarchy, so it can't be silently rerouted by a consumer's logging config.
- Already a documented Evennia surface; consumers reading `shards.log` find it next to logs they already know.

The library does not implement its own file rotation, level filtering, or destination dispatch. If those become real needs later, Evennia's logging surface is the place to extend, not this library.

## Filename

Hardcoded to `shards.log`. Not configurable.

**Why hardcoded.** A configurable filename is a footgun for very little gain: two operators tailing different files because one consumer renamed it, scripts and runbooks bit-rotting when the name drifts, and the library having to validate the consumer's choice. The library is one of many things logging into `LOG_DIR`; owning a fixed name in that namespace is a smaller surface than exposing yet another setting.

## Line format

Every line emitted by `shard_log` has the shape:

```
<ISO-8601 timestamp> [<LEVEL>] <message>
```

Example: `2026-08-18T14:22:01+00:00 [ERROR] refusing NULL-shard INSERT: DefaultObject key='a kobold' typeclass='typeclasses.mobs.Mob' — no shard context set in this thread`.

**Why a timestamp.** Evennia's `log_file` does not prepend one, and a forensic log without per-line time context is hard to correlate with other logs or with operator memory of when something happened. ISO-8601 sorts lexically and parses unambiguously.

**Why a level prefix.** Severity becomes filterable with plain `grep`, without committing the library to Python's `logging` module. Levels are deliberately small: `INFO`, `WARN`, `ERROR`. No `DEBUG` (the library has no chatty inner loops worth logging at that volume) and no `CRITICAL`.

## Two divergences from the sibling shims

Both exist for a use site this library actually has; neither is speculative.

### `trace=True`

Appends the active exception's traceback to the message. Evennia's `log_file` has no equivalent of `logging.exception()`, so consolidating `messagebus.py`'s handler-failure path onto the shim would otherwise have lost its stack trace.

Call it from inside an `except` block. Outside one, `traceback.format_exc()` returns the literal string `"NoneType: None"`; the helper detects that and appends nothing rather than writing noise.

### `security=True`

Additionally emits the message via `logger.log_sec`, into Evennia's security log.

Shard and router redirects are account- and IP-bearing audit records. Evennia's security log is a separate surface, often separately monitored and retained, and is where a security review looks. Moving those records out of it to consolidate would have been a real loss, so the two redirect sites in `handoff.py` write to both.

## Non-Evennia behaviour

When the library is imported outside a running Evennia engine — tests, future CLI tooling — `evennia.utils.logger` may not be importable. `shard_log` detects this and becomes a silent no-op.

**Why silent and not a fallback file.** Tests and CLI paths don't want stray log files in CWD or CI workspaces, and any caller in that context already has its own output channel. Silent no-op is the smallest, least-surprising behaviour. Detection is by `ImportError`, evaluated lazily on first call.

**Testing consequence.** Because the helper is a no-op under the test runner, tests cannot assert on log output by capturing a logger. They patch `shard_log` in the module under test instead — the helper is bound at import time via `from .log import shard_log`, so the patch targets the *importing* module's namespace, not `evennia_shards.log`. `_capture_shard_log` in `tests.py` does this.

## What the library logs

Wired in deliberately, not en masse. The discipline is "log what an operator would want to read later," not "log everything."

- **Refused NULL-shard inserts** — both INSERT paths (`save`, `bulk_create`) log at `ERROR` *before* raising `UnstampedInsertError`. The log is what makes the guard diagnosable rather than merely safe: a caller with a broad `except` (mob-spawner's per-rule tick is exactly one) swallows the exception, and without the log line the blocked object simply never appears with nothing anywhere saying why.
- **Tenancy install** — one `INFO` line per process at startup naming the role and shard id, and confirming the guard is armed. During a play-test this is how an operator confirms the layer is live in a given process rather than inferring it from an absence of errors.
- **Cross-shard move failures** — the move failure itself with a traceback, plus each defensive cache-eviction failure. A failed eviction leaves a stale idmapper entry behind, which is the seed of an object that outlives the row it mirrors; that must not be swallowed.
- **Cross-shard teleport failures** — with traceback. The caller gets a message, but it scrolls off and leaves no record of a half-completed move.
- **Message-bus drops** — missing target object/account/room, handler exceptions (with traceback), and timeouts with no valid `from_shard`.
- **Chargen stamping failures** — a new character left unstamped because its start location has no usable `shard_id`, or has no location at all.
- **Script-confinement contradictions** — a script declaring both an owning shard and owning roles, which is refused everywhere.
- **`at_post_login` overrides** — a consumer account class shadowing the library's patch without calling `super()`.

### Deliberately not logged

`_read_attr` in `script_confinement.py` swallows any Attribute-read failure and degrades to "declares nothing". That failure is expected on a half-initialised row during script creation, so logging it would be routine noise rather than signal. [TBD — needs discussion: whether a confinement decision taken on a *failed* read, as opposed to a genuinely absent Attribute, is worth distinguishing and logging.]

## Consumer impact

None beyond what Evennia already requires. The consumer's gamedir already has `LOG_DIR` configured. No new setting, no new install step. `shards.log` appears on first emission alongside the existing logs.

## Out of scope

- **Log rotation.** Deferred to Evennia / the operator's deployment infrastructure. The library does not own retention.
- **Structured / JSON logging.** The line format is human-readable.
- **Per-call-site level configuration.** No `SHARDS_LOG_LEVEL` setting, no filtering at emit time. If volume becomes a real problem, that's a signal to log less, not to filter at runtime.
- **Routing to Python's `logging` module.** The library does not register loggers under its package namespace; see "One mechanism, no exceptions".
