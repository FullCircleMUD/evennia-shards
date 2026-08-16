# Shard settings

How the library's configuration items are declared, read, and defaulted.

## Settings

| Setting | Type | Default | Meaning |
|---|---|---|---|
| `SHARDS_ROLE` | `str` | `ROLE_MONOLITH` | One of `ROLE_MONOLITH`, `ROLE_ROUTER`, `ROLE_SHARD` (string constants exported by the library: `"monolith"`, `"router"`, `"shard"`). Selects which role this Evennia process plays. |
| `SHARD_ID` | `str \| None` | `None` | Identifier for this shard. Consumer-chosen — descriptive names like `"overworld"` or `"underdark"` are fine. Required when role is `ROLE_SHARD`. For `ROLE_ROUTER`, must equal `get_router_shard_id()` (library mandate). |
| `ROUTER_URL` | `str \| None` | `None` | **WebSocket** URL for the router (e.g. `"ws://router.example.com/"`, `"wss://..."`). Used by shards for OOC redirect: the player's webclient closes its current WebSocket and opens a new one to this URL with `?ticket=TOKEN` appended. |
| `ROUTER_SHARD_ID` | `str` | `"router"` | The router's shard ID. Library mandate — not consumer-configurable. The router's `SHARD_ID` must be `"router"`. |
| `SHARD_URLS` | `dict \| None` | `None` | Maps shard IDs to **WebSocket** URLs (e.g. `"ws://shard0.example.com/"`). Used by the router for IC redirect: same connection-level swap as `ROUTER_URL`, in the other direction. Shard IDs are flexible — name them to match your game world. |
| `SHARDS_TICKET_BIND_IP` | `bool` | `True` | Whether a ticket records the address it was issued to, so the receiving shard refuses a connection from anywhere else. Set `False` when the shard cannot observe the player's real address — see below. |

### Turning ticket IP binding off

Set `SHARDS_TICKET_BIND_IP = False` when the address a shard observes is not the player's.

The check is defence in depth: tickets are already single-use and short-lived, so binding
only adds protection against replay from a second address. What it costs, when the address
is wrong, is every redirect — the shard compares the proxy's address against the one the
router recorded, they never match, and the player is bounced back with
`Ticket rejected: IP mismatch`.

The library resolves the real address from `x-forwarded-for`, but only when the immediate
peer appears in Evennia's `UPSTREAM_IPS`. That setting is an exact-match list, so it cannot
express a proxy whose address varies within a range — which is the normal case on a hosted
platform, where the peer is drawn from a private range like `100.64.0.0/10`.

So: keep it on behind a proxy you can enumerate, or none at all. Turn it off where you
cannot name every address the connection might arrive from.

The setting is read in `create_ticket`, not at the callsites, so callers keep passing
`session.address` and nothing else needs to know. With no address stored, the shard's
comparison is skipped — it is guarded by `if data["client_ip"]`, so a null address is what
makes the ticket acceptable from any origin.

### One Evennia setting you must also change

```python
TIME_IGNORE_DOWNTIMES = True
```

Not a library setting, but leaving it at Evennia's default of `False` is **unsafe in any
multi-process deployment**. That default derives game time from an uptime accumulator held
in a per-process module global and written back to one shared row every 60 seconds — safe
for the single Server process Evennia assumes, and a last-write-wins race between N
processes that never re-read it. Game time can end up moving backwards.

The wall-clock branch derives from the OS clock and a database constant written once, so
every process agrees with no coordination. The trade is that game time advances while the
server is down.

Full reasoning, what it costs, how to verify it, and the single-writer design to build if
you genuinely need downtime excluded: **[game-time.md](game-time.md)**.

## How they flow

The library does **not** ship a settings module. It does not write to Django's settings registry, mutate `INSTALLED_APPS`, or modify the consumer's `settings.py`. The flow is one-directional:

1. **Consumer declares** (or doesn't declare) the settings in their `server/conf/settings.py`:
   ```python
   from evennia.settings_default import *
   from evennia_shards import ROLE_ROUTER, ROLE_SHARD, get_router_shard_id
   # ...
   # Router instance:
   SHARDS_ROLE = ROLE_ROUTER
   SHARD_ID = get_router_shard_id()  # mandated to equal the role string

   # Or shard instance:
   SHARDS_ROLE = ROLE_SHARD
   SHARD_ID = "world-east"           # consumer's choice
   ```
   The `ROLE_*` constants are the single source of truth for the role enum — using them rather than bare literals means a future change to the strings is a one-line edit in `config.py`.
2. **Django loads** that file as the canonical settings module (per Evennia's launcher pointing `DJANGO_SETTINGS_MODULE` at `server.conf.settings`).
3. **Library code reads** through accessor functions that apply defaults:
   ```python
   from evennia_shards import get_role, get_shard_id
   role = get_role()         # "monolith" if undeclared, else what consumer set
   shard = get_shard_id()    # None if undeclared, else what consumer set
   ```

The accessors live in [`evennia_shards/config.py`](../src/evennia_shards/config.py) and use `getattr(settings, "...", default)`. The defaults are baked into the library's read code, not into a settings file.

## Why this shape

- **Monolith consumers configure nothing.** No required declarations, no library-provided settings module to inherit from. The default behaviour (do nothing) is genuinely the default — declaring it would be redundant.
- **Non-monolith consumers register the app and set the role.** `SHARDS_ROLE` (and `SHARD_ID` for the shard role) in their existing `settings.py`, plus `INSTALLED_APPS += ["evennia_shards"]` — registering the app is what loads the library's models and runs its `AppConfig.ready()`.
- **The library is a Django app.** It ships models (`Message`, `Ticket`) with migrations and an `AppConfig` whose `ready()` installs the library's runtime integration in non-monolith roles — tenant isolation, the WebSocket protocol override and redirect middleware, the Portal-services plugin, and the shard-aware account/command patches. `ready()` is gated on the role, so in monolith it no-ops and registration is unnecessary.
- **`getattr` defaults centralise the contract.** Library code always reads through the accessors, so the fallback value is defined in exactly one place. Adding a new setting later means adding one accessor; consumers automatically get the new default without changes.

## Reading the settings

Code that needs shard configuration — library code *or* consumer game code — should call the accessors rather than reading `settings.*` directly:

```python
from evennia_shards import get_role, get_shard_id, get_shard_url, get_router_url, get_router_shard_id
role = get_role()                  # "monolith" if undeclared
shard = get_shard_id()             # None if undeclared
url = get_shard_url("overworld")   # ValueError if SHARD_URLS not configured
                                   # KeyError if shard_id not in the dict
router = get_router_url()          # ValueError if ROUTER_URL not configured
router_id = get_router_shard_id()  # always "router" — library mandate
```

A direct `settings.SHARDS_ROLE` read raises `AttributeError` whenever the consumer hasn't declared the setting — i.e. every monolith consumer. The accessors apply the documented defaults and are the single source of truth for fallback values, so any future change to a default lands in one place.

The primary caller is library code (it reads the role to decide what to register at boot). Consumers can also call these — for instance, an admin command that prints the deployment mode — and should, rather than rolling their own `getattr` reads.

## Row-level `shard_id` and the global sentinel

The library adds a `shard_id` column to `ObjectDB`, tagging each row with its owning shard. Most rows hold a specific shard identifier (e.g. `"shard0"`); the sentinel value `"*"` denotes a row visible from *every* shard. A `"*"` row is instantiated independently in each shard process, so mutable per-instance state on it does not coordinate across shards without explicit cross-shard messaging.

Tenancy is installed on `ObjectDB` and nothing else — see [tenancy.md](tenancy.md). No other model carries a `shard_id` column, including `ScriptDB`; see below for what that means for global scripts.

## Global scripts run one instance per process

`ScriptDB` is not tenant-scoped and carries no `shard_id` column, so a persistent global script is a **single row visible to every process**. What is per-process is the *running instance*: a script's `ndb` is in-memory, so each Server process attaches its own Twisted `LoopingCall` to that shared row and ticks independently of the others.

The consequence for consumers: in an N-shard deployment a global script's tick fires **N times per interval**, once in each process. Whether that is correct depends entirely on what the script does.

**Safe per-process** — scripts that hold no persistent state (counters on `ndb`, never `db`) and act only on process-local data. The canonical shape is walking `SESSION_HANDLER` and mutating the puppets connected to *this* process: `SessionHandler` is a plain in-memory `dict` per Server process and `get_puppet()` is an attribute read, so a process structurally cannot see another shard's sessions. Such a script needs no shard scoping, because it issues no query to scope.

**Not safe per-process** — scripts that query the world, aggregate, or produce side-effects that must happen once. Under the auto-filter each process silently sees only its own shard's rows, so the script does a partial job *and* emits its side-effect once per process. Note the failure mode is quiet: before django-multitenant an unscoped query raised, now it just returns a subset. "Starts with no errors" is not evidence such a script is correct.

**Confined to where it belongs** — a script that should not run on every process can declare where it does belong, and the library keeps it there. One shard via `owning_shard`, or a set of roles via `owning_roles` when no single shard owns it. A single Attribute either way; no base class to inherit and no runtime API to call. See [script-confinement.md](script-confinement.md).

Declaring roles is what a consumer's own role table cannot achieve alone. Such a table gates what each process *creates*; Evennia's boot walk attaches a `LoopingCall` to any active row still carrying a pause marker and knows nothing about roles, so the first process to boot picks up every marked script in the cluster. Role gating is therefore only half-enforced at boot without confinement — honoured for what a process claims, silently exceeded for what it sweeps up.

That mechanism answers *where may this script run*. It is **not** a "run exactly once across the cluster" mechanism, and the library provides none: a consumer needing a singleton that belongs to no particular shard or role must still gate it themselves — by nominating one, or by their own election.

> The per-process behaviour above was established by reading the boot path (`run_init_hooks` calls `at_server_start()` before `evennia.GLOBAL_SCRIPTS.start()`, so `ndb._task` is empty when a consumer's boot hook runs) and the script sources. Confinement is confirmed on a live three-process deployment across repeated restarts in varying boot orders — see [script-confinement.md](script-confinement.md#verified-behaviour).

## URL settings and redirect routing

The router and shards have separate URL settings, reflecting their different roles in the redirect flow:

- **`ROUTER_URL`** — single string, the router's WebSocket URL. Shards use `get_router_url()` to build OOC redirect URLs (sending players back to the router).
- **`SHARD_URLS`** — dict mapping shard IDs to WebSocket URLs. The router uses `get_shard_url(shard_id)` to build IC redirect URLs (sending players to a shard).

```python
ROUTER_URL = "ws://router.example.com/"
SHARD_URLS = {
    "overworld": "ws://overworld.example.com/",
    "dungeons": "ws://dungeons.example.com/",
    "pvp_arena": "wss://pvp.example.com/",
}
```

These are **WebSocket URLs** (`ws://` or `wss://`), not HTTP URLs — the library does *connection-level* redirects: when a player crosses a shard boundary, the JS in the loaded webclient page closes its current WebSocket and opens a new one to the configured target URL with `?ticket=TOKEN` appended. The page itself is not reloaded; UI state, scrollback, plugins all persist across the transition.

This is the same pattern that telnet/SSH/MUD-client redirect would use (close the connection, open a new one to the target host:port with auth) — the WebSocket URL is just one expression of it. The HTTP URL of a shard or router is not the library's concern; whether the consumer also serves an HTTP webclient is a deployment decision.

Shard IDs are flexible — name them to match your game world. Each shard instance's `SHARD_ID` must match a key in `SHARD_URLS`. In production, URLs are typically set via environment variables.

The IC routing decision comes from the character's game state: `character.location or character.home` → room's `shard_id` → `get_shard_url(shard_id)`. Returning players go back to where they were; new characters land in their start location.

## Per-shard home room

Each shard needs its own home room — the fallback location for characters and the spawn point for new ones. Evennia already has `DEFAULT_HOME` and `START_LOCATION` (both default to `"#2"`, the Limbo room created during initial setup). Shards override these in their per-instance settings file to point at a shard-specific room.

```python
# settings_shard0.py
DEFAULT_HOME = "#2"       # or whatever PK the shard's home room has
START_LOCATION = "#2"
```

The library does **not** create these rooms or manage their PKs. The consumer creates them however suits their deployment — initial setup hook, build commands, migration script — and records the PK in settings. This works for both greenfield (new game) and brownfield (existing game adding shards) deployments.

A shard only needs to know its **own** home room. The IC routing flow (`character.location or character.home` → room's `shard_id` → `get_shard_url()`) sends players to the right shard URL; the destination shard places the character based on the character's own location/home already stored in the DB.

## Consumer settings cascade

The library does not prescribe a settings layout, but the demo game uses a three-level cascade that separates per-instance config from shared shard config:

```
settings_router.py  ─┐
settings_shard0.py  ─┤── imports ── settings_common_shard_config.py ── imports ── settings.py
settings_shard1.py  ─┘
```

- **`settings.py`** — base Evennia config (`SERVERNAME`, etc.), loads `secret_settings.py`
- **`settings_common_shard_config.py`** — settings shared across all sharded instances: `ROUTER_URL`, `SHARD_URLS`, `INSTALLED_APPS += ["evennia_shards"]`, `TELNET_ENABLED = False`
- **`settings_<role>.py`** — per-instance: `SHARDS_ROLE`, `SHARD_ID`, `DEFAULT_HOME`/`START_LOCATION`, port overrides (`WEBSERVER_PORTS`, `WEBSOCKET_CLIENT_PORT`, `AMP_PORT`)

Each instance starts with `evennia start --settings settings_router.py` (or `settings_shard0.py`, etc.). The cascade keeps the URL map in one place while allowing each instance to set its own role and ports.

## Telnet

Telnet is disabled for all sharded instances (`TELNET_ENABLED = False` in the common config). The ticket-based auth flow is websocket-only — telnet has no mechanism to carry a ticket token (no URL, no query parameters). Wiring telnet into the ticket system is future work.

## HTTP webserver: router-only by default

The library assumes exactly one HTTP webserver in the deployment, hosting the webclient page, the website, and the static-asset pipeline. By default it's the router:

| Setting | Router | Shard |
|---|---|---|
| `WEBSERVER_ENABLED` | `True` | `False` |

Shards exist to host player sessions, not to serve web pages. Disabling the webserver on shards drops the entire HTTP stack on those processes (reverse-proxy, AJAX webclient, Django views, `WEB_PLUGINS_MODULE` hook chain) — they listen on the AMP port and the WebSocket port, nothing else.

This requires a small library workaround because Evennia 6.0.0 bundles the WebSocket registration inside `register_webserver` (see [deployment-topology.md](deployment-topology.md#a-note-on-evennias-coupling)). The library's Portal-services plugin (`evennia_shards/portal_services.py`) registers the WebSocket independently when `WEBSERVER_ENABLED = False`. Auto-installed by `AppConfig.ready()`; no consumer wiring required.

Consumers running their website on a separate service (Next.js, static site host, separate Django, etc.) can flip `WEBSERVER_ENABLED = False` on the router as well — the same plugin keeps the WebSocket running with no HTTP serving on the Evennia process at all.

## Localhost multi-instance ports

Each Evennia instance binds several ports. When running multiple instances on localhost for testing, each needs its own set to avoid collisions. The demo game offsets shard ports by 10 from the router's defaults:

| Port | Router | Shard0 |
|---|---|---|
| `WEBSERVER_PORTS` | `(4001, 4005)` | `(4011, 4015)` |
| `WEBSOCKET_CLIENT_PORT` | `4002` | `4012` |
| `AMP_PORT` | `4006` | `4016` |

Additional shards increment by 10 again (4021/4022/4026, etc.). `AMP_PORT` is Evennia's internal Portal↔Server IPC — not player-facing, but still needs a unique port per instance. In production (separate hosts), port offsets are unnecessary.

## Localhost multi-instance game directories

The recipe for running multiple roles locally differs by OS (Windows runs all roles from one gamedir; Unix needs symlinked view gamedirs to avoid PID-file collisions). See [deployment-topology.md § Local development](deployment-topology.md#local-development) for the full explanation and the canonical recipes.

## What this design doesn't address

- **Validation.** Nothing checks that `SHARDS_ROLE` is one of the three valid strings, or that `SHARD_ID` is set when role is `"shard"`. Validation will land with whatever code first depends on it. Pre-building it now would be forward-design.
