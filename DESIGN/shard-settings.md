# Shard settings

How the library's configuration items are declared, read, and defaulted.

## Settings

| Setting | Type | Default | Meaning |
|---|---|---|---|
| `SHARDS_ROLE` | `str` | `"monolith"` | One of `"monolith"`, `"router"`, `"shard"`. Selects which role this Evennia process plays. |
| `SHARD_ID` | `str \| None` | `None` | Identifier for this shard. Meaningful only when `SHARDS_ROLE == "shard"`. |
| `SHARD_URLS` | `dict \| None` | `None` | Maps shard IDs to webclient base URLs. Required for any sharded deployment. |

## How they flow

The library does **not** ship a settings module. It does not write to Django's settings registry, mutate `INSTALLED_APPS`, or modify the consumer's `settings.py`. The flow is one-directional:

1. **Consumer declares** (or doesn't declare) the settings in their `server/conf/settings.py`:
   ```python
   from evennia.settings_default import *
   # ...
   SHARDS_ROLE = "router"   # only when not monolith
   SHARD_ID = "world-east"  # only when role is "shard"
   ```
2. **Django loads** that file as the canonical settings module (per Evennia's launcher pointing `DJANGO_SETTINGS_MODULE` at `server.conf.settings`).
3. **Library code reads** through accessor functions that apply defaults:
   ```python
   from evennia_shards import get_role, get_shard_id
   role = get_role()         # "monolith" if undeclared, else what consumer set
   shard = get_shard_id()    # None if undeclared, else what consumer set
   ```

The accessors live in [`evennia_shards/config.py`](../evennia_shards/config.py) and use `getattr(settings, "...", default)`. The defaults are baked into the library's read code, not into a settings file.

## Why this shape

- **Monolith consumers configure nothing.** No required declarations, no library-provided settings module to inherit from. The default behaviour (do nothing) is genuinely the default — declaring it would be redundant.
- **Non-monolith consumers add at most two lines.** Just `SHARDS_ROLE` (and `SHARD_ID` for shard role) in their existing `settings.py`. No import changes, no app registrations.
- **The library is not a Django app.** It exposes Python functions, not models/admin/signals/URLs. Adding it to `INSTALLED_APPS` is unnecessary today. If a future feature needs Django-level integration, that's the trigger to revisit — not now.
- **`getattr` defaults centralise the contract.** Library code always reads through the accessors, so the fallback value is defined in exactly one place. Adding a new setting later means adding one accessor; consumers automatically get the new default without changes.

## Reading the settings

Code that needs shard configuration — library code *or* consumer game code — should call the accessors rather than reading `settings.*` directly:

```python
from evennia_shards import get_role, get_shard_id, get_shard_url
role = get_role()                  # "monolith" if undeclared
shard = get_shard_id()             # None if undeclared
url = get_shard_url("shard0")      # ValueError if SHARD_URLS not configured
                                   # KeyError if shard_id not in the dict
```

A direct `settings.SHARDS_ROLE` read raises `AttributeError` whenever the consumer hasn't declared the setting — i.e. every monolith consumer. The accessors apply the documented defaults and are the single source of truth for fallback values, so any future change to a default lands in one place.

The primary caller is library code (it reads the role to decide what to register at boot). Consumers can also call these — for instance, an admin command that prints the deployment mode — and should, rather than rolling their own `getattr` reads.

## Row-level `shard_id` and the global sentinel

The library also adds a `shard_id` column to `ObjectDB` (and likely other partitioned models in future) that tags each row with its owning shard. Most rows hold a specific shard identifier (e.g. `"shard0"`); the sentinel value `"*"` denotes a row owned by *all* shards — used for system-wide entities like global scripts that must run on every shard process. Global rows are instantiated independently on each shard, so any mutable per-instance state does not coordinate across shards without explicit cross-shard messaging.

## `SHARD_URLS` and redirect routing

`SHARD_URLS` is a dict mapping every shard ID (including the router) to its webclient base URL. The IC/OOC redirect flow uses it to build the target URL when sending a player to a different instance.

```python
SHARD_URLS = {
    "router": "http://router.example.com",
    "shard0": "http://shard0.example.com",
    "shard1": "http://shard1.example.com:5001",
}
```

The library reads this via `get_shard_url(shard_id)`, which raises `ValueError` if the setting is absent and `KeyError` if the shard ID is not in the dict. In production, URLs are typically set via environment variables. For local development, all instances share the same settings file with localhost URLs on different ports.

The routing decision itself comes from the character's game state: `character.location or character.home` → room's `shard_id` → `get_shard_url(shard_id)`. Returning players go back to where they were; new characters land in their home room.

## Consumer settings cascade

The library does not prescribe a settings layout, but the demo game uses a three-level cascade that separates per-instance config from shared shard config:

```
settings_router.py  ─┐
settings_shard0.py  ─┤── imports ── settings_common_shard_config.py ── imports ── settings.py
settings_shard1.py  ─┘
```

- **`settings.py`** — base Evennia config (`SERVERNAME`, etc.), loads `secret_settings.py`
- **`settings_common_shard_config.py`** — settings shared across all sharded instances: `SHARD_URLS`, `INSTALLED_APPS += ["evennia_shards"]`
- **`settings_<role>.py`** — per-instance: `SHARDS_ROLE`, `SHARD_ID`, port overrides

Each instance starts with `evennia start --settings settings_router.py` (or `settings_shard0.py`, etc.). The cascade keeps the URL map in one place while allowing each instance to set its own role and ports.

## What this design doesn't address

- **Validation.** Nothing checks that `SHARDS_ROLE` is one of the three valid strings, or that `SHARD_ID` is set when role is `"shard"`. Validation will land with whatever code first depends on it. Pre-building it now would be forward-design.
