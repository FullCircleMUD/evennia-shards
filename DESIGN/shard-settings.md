# Shard settings

How the library's two configuration items (`SHARDS_ROLE` and `SHARD_ID`) are declared, read, and defaulted.

## The two settings

| Setting | Type | Default | Meaning |
|---|---|---|---|
| `SHARDS_ROLE` | `str` | `"monolith"` | One of `"monolith"`, `"router"`, `"shard"`. Selects which role this Evennia process plays. |
| `SHARD_ID` | `str \| None` | `None` | Identifier for this shard. Meaningful only when `SHARDS_ROLE == "shard"`. |

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

Code that needs the current role or shard id — library code *or* consumer game code — should call the accessors rather than reading `settings.SHARDS_ROLE` directly:

```python
from evennia_shards import get_role, get_shard_id
role = get_role()
shard = get_shard_id()
```

A direct `settings.SHARDS_ROLE` read raises `AttributeError` whenever the consumer hasn't declared the setting — i.e. every monolith consumer. The accessors apply the documented defaults and are the single source of truth for fallback values, so any future change to a default lands in one place.

The primary caller is library code (it reads the role to decide what to register at boot). Consumers can also call these — for instance, an admin command that prints the deployment mode — and should, rather than rolling their own `getattr` reads.

## What this design doesn't address

- **Validation.** Nothing checks that `SHARDS_ROLE` is one of the three valid strings, or that `SHARD_ID` is set when role is `"shard"`. Validation will land with whatever code first depends on it. Pre-building it now would be forward-design.
- **Other settings.** This document covers only `SHARDS_ROLE` and `SHARD_ID`. Future settings (router URL, shard registry, ticket secret, etc.) will follow the same pattern but aren't designed yet.
