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

  demo_shard1/      Symlinked instance for a second shard (not active
                    by default — add "shard1" to SHARD_URLS when ready).
```

### What's symlinked vs real

Each symlinked instance (`demo_router`, `demo_shard1`) contains:

- **Symlinks** to `demo_shard0/`: `commands/`, `typeclasses/`, `web/`,
  `world/`, `server/conf/`, `server/.static/`, `.gitignore`
- **Real (instance-specific)**: `server/`, `server/__init__.py`,
  `server/logs/`

This means PID files and logs are per-instance (no collisions), while
game code and settings are shared (edit once, applies everywhere).

### Shared database

All instances share the same SQLite database at
`demo_shard0/server/evennia.db3`. This is configured in
`settings_common_shard_config.py` using `os.path.realpath(__file__)`
to resolve symlinks back to the real conf directory.

## Quick start

### 1. First-time setup

From the `demo_shard0` directory (shard0 must boot first so Limbo #2
gets stamped with `shard_id="shard0"`):

```bash
cd examples/demo_shard0
evennia migrate --settings settings_shard0
evennia start --settings settings_shard0
```

This will prompt you to create a superuser, run initial setup, and
start shard0 on its ports (web 4011, websocket 4012).

### 2. Start the router

In a separate terminal:

```bash
cd examples/demo_router
evennia start --settings settings_router
```

The router starts on default ports (web 4001, websocket 4002).

### 3. Connect

- **Router webclient**: http://localhost:4001
- **Shard0 webclient**: http://localhost:4011

### Stopping

From each instance's directory:

```bash
evennia stop --settings settings_router    # from demo_router/
evennia stop --settings settings_shard0    # from demo_shard0/
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
