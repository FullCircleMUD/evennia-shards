# Game time under sharding

Evennia's game clock is safe with one Server process and unsafe with several. A sharded
consumer must set `TIME_IGNORE_DOWNTIMES = True`, or build a single-writer replacement.
This document explains why, what the setting costs, and what the replacement would involve.

## The two modes

`evennia.utils.gametime.gametime()` has one branch, selected by `settings.TIME_IGNORE_DOWNTIMES`:

```python
if IGNORE_DOWNTIMES:
    gtime = epoch + (time.time() - server_epoch()) * TIMEFACTOR   # wall clock
else:
    gtime = epoch + (runtime() - GAME_TIME_OFFSET) * TIMEFACTOR   # accumulated uptime
```

Evennia's default is `False` — the uptime branch.

## Why the uptime branch breaks under sharding

`runtime()` returns:

```python
SERVER_RUNTIME + time.time() - SERVER_RUNTIME_LAST_UPDATED
```

`SERVER_RUNTIME` is a **module-level global** — one per Python process. Evennia's
`server_maintenance` loop, a `LoopingCall` on a 60-second cadence, maintains it:

- On its first tick after a start or reload, it loads the value from
  `ServerConfig["runtime"]`.
- On every tick thereafter, it adds the elapsed interval **to the in-memory global**.
- On every tick, it writes that global back to `ServerConfig["runtime"]`.

Between ticks, `runtime()` interpolates with local wall time, so the value reads as
continuous rather than stepped.

That design assumes exactly one Server process. A sharded deployment runs several, and each
one:

- holds its **own** `SERVER_RUNTIME`, in its own memory,
- accumulates independently from whenever *it* started,
- overwrites the **same** `ServerConfig["runtime"]` row every 60 seconds,
- and never re-reads that row after startup, so never notices being overwritten.

The result is N unco-ordinated writers on one key, last write wins. A shard that started an
hour after its siblings carries a total 3600s lower and stamps it over theirs every minute,
so the stored value flaps between different totals continuously.

**The consequential failure is that game time can move backwards.** A process that reloads
reads whatever was last written, which may be lower than the total it had accumulated. Any
consumer logic deriving a calendar from game time — day/night phase, seasons, day counters
— will see time reverse. Day/night surviving that is plausible; a season running backwards
is not.

It also means processes simply *disagree*: two shards can be in different seasons at the
same moment.

## Why the wall-clock branch is immune

```python
gtime = game_epoch() + (time.time() - server_epoch()) * TIMEFACTOR
```

Every term is one of three things:

- `TIMEFACTOR` — a settings constant.
- `game_epoch()` — `settings.TIME_GAME_EPOCH`, or `server_epoch()` when that is `None`.
- `server_epoch()` — read from `ServerConfig["server_epoch"]`, written **once** by
  `initial_setup.py` on the first ever start against a fresh database, and cached in a
  module global thereafter.
- `time.time()` — the OS clock, which every process on a host already agrees on, and which
  NTP keeps agreed across hosts.

No accumulator, no writer, nothing persisted per tick. Every process computes an identical
value with no coordination, no round trip, and no cold-start case. `runtime()` is not called
at all, so the contended row becomes irrelevant to the clock. Processes still write it every
60 seconds; nothing reads it.

It is also more durable across restarts than the accumulator, which loses up to 60 seconds
of accrual on an unclean shutdown. There is nothing to lose when there is nothing stored.

## What the setting costs

**Game time advances while the server is down.** That is the entire trade, and it is what
the setting's name says.

Players cannot observe it. Game time passes while a player is logged off either way, and a
restart disconnects them, so they are inside the gap regardless. A player returning after an
hour away finds an hour has passed — which is the intuitive result. The uptime branch is the
one that produces the surprise, where an hour away during an outage yields no elapsed time
at all.

The systems that *would* notice are any holding a game-time deadline — a quest expiring at
game-time X, a timed effect measured in game hours. A forward jump expires those in bulk.
Audit for them before adopting the setting on a game that has been running.

## Verification

The scheme rests on one database row. Confirm `ServerConfig["server_epoch"]` exists:

```sql
SELECT db_key, db_value FROM server_serverconfig WHERE db_key = 'server_epoch';
```

If it is missing, `server_epoch()` falls back to `time.time() - runtime()` — dragging the
contended accumulator back in through the epoch, and defeating the whole arrangement.
`initial_setup.py` writes it on first start, so on a normally created database it will be
present.

## If a consumer genuinely needs downtime excluded

The library does not provide this. The uptime accumulator has to be made single-writer:

1. **Router publishes an anchor** on its own timer:
   `ServerConfig["shard_clock"] = (runtime(), time.time())`. The timestamp is required
   because `ServerConfig` carries only `db_key` and `db_value` — no modified column — so a
   reader cannot otherwise tell how stale a value is.
2. **Shards are barred from writing `runtime`.** The narrowest seam is a guard on
   `ServerConfigManager.conf` refusing that key on the shard role; it returns `None` on the
   write path, so a guard returning `None` is indistinguishable from a successful write.
   Wrapping `server_maintenance` instead would mean reproducing a method body that also
   flushes the idmapper and recycles database connections.
3. **Shards override `runtime()`** to extrapolate from the anchor:
   `value + (time.time() - written_at)`. Extrapolating from the anchor rather than from the
   moment of reading is what makes read latency irrelevant — a value read 59 seconds late
   yields the same answer as one read immediately, so no fast polling or message-bus
   delivery is needed.
4. **A staleness cap** stops shards extrapolating once the anchor stops being refreshed, so
   the clock freezes when the router stops. That is the correct definition of downtime for
   this architecture: the router is the entry point, so nobody can be playing without it.

Reads must bypass caching. `ServerConfig` is a `WeakSharedMemoryModel` with its own instance
cache, so use `values_list` rather than the normal accessor.

That is a module, two Evennia couplings to re-diff on every upgrade, and a tuning constant —
weighed against what downtime actually costs in a given deployment.
