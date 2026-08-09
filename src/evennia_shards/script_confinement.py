# SPDX-License-Identifier: BSD-3-Clause
"""Confine a shard-owned persistent script to the process that owns it.

``ScriptDB`` carries no ``shard_id`` and is not tenant-scoped, so a persistent
script is a single row visible to every process, and each process attaches its
own ``LoopingCall`` to it. For a script whose work belongs to one shard, that
means it can tick on a process that does not own it — and on the unscoped
router, rows it creates land with no shard stamp.

The library cannot know which scripts are shard-specific, so ownership is
declared by the consumer as data: an ``owning_shard`` Attribute naming the
shard the script belongs to. Scripts without it are untouched, which is what
keeps global scripts and every non-shard-specific script working as before.

Enforcement wraps two methods on ``ScriptDB``, because neither covers the
other:

- ``_unpause_task`` is what the boot walk calls
  (``update_scripts_after_server_start``), and it attaches the ``LoopingCall``
  directly rather than going through ``_start_task``. Returning early here
  leaves ``_paused_time`` untouched, so the owning process still finds its
  pause marker whenever it boots — boot order stops mattering.
- ``_start_task`` covers everything else: explicit ``start()``, the autostart
  on creation, and ``unpause()``. It calls ``_unpause_task`` internally but
  then starts the loop directly if that didn't, so guarding ``_unpause_task``
  alone is bypassed. The guard sits at the top, before the ``db_is_active``
  write, so a non-owning process never touches the shared row.

Installed from ``EvenniaShardsConfig.ready()`` alongside the ObjectDB tenancy
install, and idempotent via a marker attribute in the same style.
"""

import functools

from .config import get_shard_id


OWNING_SHARD_ATTR = "owning_shard"
"""Attribute name a consumer sets to declare which shard owns a script.

Deliberately not ``shard_id`` — that is the ``ObjectDB`` column the tenancy
layer owns, and conflating the two invites confusion about which mechanism is
in play.
"""


def get_owning_shard(script):
    """Return the shard that owns `script`, or ``None`` if it declares none.

    A script with no ``owning_shard`` Attribute is not shard-specific and is
    never confined.
    """
    try:
        return script.attributes.get(OWNING_SHARD_ATTR, default=None)
    except Exception:
        # An Attribute read can fail on a half-initialised row (during
        # creation, before the pk exists). Treat that as "declares nothing"
        # rather than letting the guard break script creation.
        return None


def is_foreign_script(script) -> tuple:
    """Return ``(blocked, owner, current)`` for `script` on this process.

    ``blocked`` is True only when the script declares an owning shard and it
    is not the shard this process is running as.
    """
    owner = get_owning_shard(script)
    current = get_shard_id()
    if owner is None:
        return False, None, current
    return owner != current, owner, current


def install_script_confinement() -> None:
    """Late-bind the ownership guards onto ``ScriptBase``. Idempotent.

    ``ScriptBase`` rather than ``ScriptDB``: the model itself carries no task
    machinery — ``_start_task`` / ``_unpause_task`` are defined one level up,
    on the typeclass base every script inherits from.
    """
    from evennia.scripts.scripts import ScriptBase

    if getattr(ScriptBase, "_evennia_shards_confinement_installed", False):
        return

    original_start = ScriptBase._start_task
    original_unpause = ScriptBase._unpause_task

    @functools.wraps(original_start)
    def _guarded_start_task(self, *args, **kwargs):
        blocked, _owner, _current = is_foreign_script(self)
        if blocked:
            return
        return original_start(self, *args, **kwargs)

    @functools.wraps(original_unpause)
    def _guarded_unpause_task(self, *args, **kwargs):
        blocked, _owner, _current = is_foreign_script(self)
        if blocked:
            # Return before reading or clearing _paused_time — the marker
            # must survive for the owning process.
            return
        return original_unpause(self, *args, **kwargs)

    ScriptBase._start_task = _guarded_start_task
    ScriptBase._unpause_task = _guarded_unpause_task
    ScriptBase._evennia_shards_confinement_installed = True
