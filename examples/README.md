# Demo game — multi-instance localhost setup

This directory contains a demo Evennia game configured for multi-instance
testing of the evennia-shards library.

## Directory structure

```
examples/
  demo_shard0/      The "real" game directory. Contains all game code
                    (commands, typeclasses, web, world), all settings
                    files, and the shared SQLite database.

  demo_router/      Symlinked instance for the router role. Links to
                    demo_shard0's code and settings, but has its own
                    server/ directory for PID files and logs.

  demo_shard1/      Symlinked instance for a second shard. Same
                    pattern as demo_router. Active in SHARD_URLS.
```

### What's symlinked vs real

Each symlinked instance (`demo_router`, `demo_shard1`) contains:

- **Symlinks** to `demo_shard0/`: `commands/`, `typeclasses/`, `web/`,
  `world/`, `server/conf/`, `server/.static/`, `.gitignore`
- **Real (instance-specific)**: `server/`, `server/__init__.py`,
  `server/logs/`

This means PID files and logs are per-instance (no collisions), while
game code and settings are shared (edit once, applies everywhere).

### Windows note (symlink caveat)

The symlinked layout has not been verified on Windows. Symlinks may or
may not work end-to-end depending on Windows version, `git config
core.symlinks`, Developer Mode being enabled, and how Evennia's
`os.path.realpath`-based settings resolution behaves with the link
type git materialises. With `core.symlinks=true` and the right
permissions on Windows 10/11, the layout *probably* works — but we
haven't tested it.

A guaranteed-working fallback if symlinks aren't co-operating: instead
of relying on them, **copy the contents of `demo_shard0`** into
`demo_router` and `demo_shard1`. Keep the per-instance
`server/__init__.py` and `logs/` in place so PID files and logs stay
separate. The settings files already exist for each role inside
`demo_shard0/server/conf/` (`settings_router.py`, `settings_shard0.py`,
`settings_shard1.py`); a copied tree gives each instance its own
physical copy of those files to point at via `--settings`.

The trade-off with the copy approach is that "edit once, applies
everywhere" no longer holds — changes to game code under `demo_shard0`
need to be propagated by hand to the copied instances. Acceptable for
smoke testing; for ongoing multi-instance development a Docker-based
setup or a small sync helper would be worth the time.

### Shared database

All instances share the same SQLite database at
`demo_shard0/server/evennia.db3`. This is configured in
`settings_common_shard_config.py` using `os.path.realpath(__file__)`
to resolve symlinks back to the real conf directory.

## Quick start

### 1. Migrate and start shard0 first

Boot order matters. The first instance to run `evennia migrate` and
`evennia start` is the one whose initial-setup runs — which is what
creates the bootstrap rows (`#1` superuser character, `#2` Limbo).
Those rows get auto-stamped by the `pre_save` chokepoint with
whatever shard the booting process is, so **whichever shard runs
first owns Limbo**.

Chargen happens on the **router** (it's an OOC operation), so the
**router's** `START_LOCATION` is what determines where new characters
spawn. The library's chargen wrapper looks up that row's `shard_id`
and stamps the new character to match. Per-shard `START_LOCATION`
settings are not consulted in the sharded path — change the router's
`START_LOCATION` (in `settings_router.py`) to point at a room on the
shard you want new players to spawn on.

We boot `shard0` first so Limbo (`#2`) lands on `shard0`, and the
router's default `START_LOCATION = "#2"` resolves to a `shard0`-owned
row — meaning new players land on `shard0`. Point the router's
`START_LOCATION` at a room on a different shard to change where
chargen spawns to.

```bash
cd examples/demo_shard0
evennia migrate --settings settings_shard0
evennia start --settings settings_shard0
```

The migrate step prompts for superuser creation and runs initial
setup. shard0 then listens on web 4011 / websocket 4012.

### 2. Start shard1

The DB is shared, so shard1 only needs `evennia start` — no
separate migration. In a new terminal:

```bash
cd examples/demo_shard1
evennia start --settings settings_shard1
```

shard1 listens on web 4021 / websocket 4022.

### 3. Start the router

In a third terminal:

```bash
cd examples/demo_router
evennia start --settings settings_router
```

The router listens on web 4001 / websocket 4002.

### 4. Connect

- **Router webclient**: http://localhost:4001 — log in here. After `@ic`
  you're redirected to the character's owning shard.
- **Shard0 webclient**: http://localhost:4011 (direct shard access; for
  smoke testing only, normal play goes through the router).
- **Shard1 webclient**: http://localhost:4021 (same).

### Stopping

From each instance's directory:

```bash
evennia stop --settings settings_router    # from demo_router/
evennia stop --settings settings_shard0    # from demo_shard0/
evennia stop --settings settings_shard1    # from demo_shard1/
```

## Port assignments

| Port               | Router | Shard0 | Shard1 |
|--------------------|--------|--------|--------|
| `WEBSERVER_PORTS`  | 4001   | 4011   | 4021   |
| `WEBSOCKET_CLIENT` | 4002   | 4012   | 4022   |
| `AMP_PORT`         | 4006   | 4016   | 4026   |

Telnet is disabled for all sharded instances (ticket auth is
websocket-only).

## Settings cascade

All per-instance settings files import from a shared middle layer:

```
settings_router.py  ─┐
settings_shard0.py  ─┤── settings_common_shard_config.py ── settings.py
settings_shard1.py  ─┘
```

- `settings.py` — base Evennia config
- `settings_common_shard_config.py` — shared DB path, `SHARD_URLS`,
  `ROUTER_URL`, `INSTALLED_APPS`, `TELNET_ENABLED = False`
- `settings_<role>.py` — per-instance: `SHARDS_ROLE`, `SHARD_ID`,
  `DEFAULT_HOME`, port overrides

## Recreating the symlinked directories

If you need to recreate `demo_router` or `demo_shard1` from scratch:

```bash
cd examples

# demo_router
mkdir -p demo_router/server/logs
touch demo_router/server/__init__.py
ln -s ../demo_shard0/commands demo_router/commands
ln -s ../demo_shard0/typeclasses demo_router/typeclasses
ln -s ../demo_shard0/web demo_router/web
ln -s ../demo_shard0/world demo_router/world
ln -s ../../demo_shard0/server/conf demo_router/server/conf
ln -s ../../demo_shard0/server/.static demo_router/server/.static
ln -s ../demo_shard0/.gitignore demo_router/.gitignore

# demo_shard1 (same pattern)
mkdir -p demo_shard1/server/logs
touch demo_shard1/server/__init__.py
ln -s ../demo_shard0/commands demo_shard1/commands
ln -s ../demo_shard0/typeclasses demo_shard1/typeclasses
ln -s ../demo_shard0/web demo_shard1/web
ln -s ../demo_shard0/world demo_shard1/world
ln -s ../../demo_shard0/server/conf demo_shard1/server/conf
ln -s ../../demo_shard0/server/.static demo_shard1/server/.static
ln -s ../demo_shard0/.gitignore demo_shard1/.gitignore
```
