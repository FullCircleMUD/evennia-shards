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

from evennia.utils import logger

from .config import get_role, get_shard_id


OWNING_SHARD_ATTR = "owning_shard"
"""Attribute name a consumer sets to declare which shard owns a script.

Deliberately not ``shard_id`` — that is the ``ObjectDB`` column the tenancy
layer owns, and conflating the two invites confusion about which mechanism is
in play.
"""

OWNING_ROLES_ATTR = "owning_roles"
"""Attribute name a consumer sets to declare which *roles* may run a script.

A list, because the common case is more than one — work that belongs on every
shard but not the router, say. Use this when the script has no single owner;
use :data:`OWNING_SHARD_ATTR` when exactly one shard owns it.

The two are mutually exclusive. A script declaring both is a contradiction —
"only shard0" and "any shard" cannot both be true — so the guards treat it as
misconfigured and refuse to run it anywhere, loudly. Failing closed is the
safe direction: a script that does not run is visible in the consumer's own
status tooling, whereas one running in the wrong place is the failure this
module exists to prevent.
"""


def _read_attr(script, name):
    """Read one Attribute, treating any failure as "declares nothing".

    An Attribute read can fail on a half-initialised row — during creation,
    before the pk exists. That must not break script creation, so it degrades
    to the unconfined answer.
    """
    try:
        return script.attributes.get(name, default=None)
    except Exception:
        return None


def get_owning_shard(script):
    """Return the shard that owns `script`, or ``None`` if it declares none."""
    return _read_attr(script, OWNING_SHARD_ATTR)


def get_owning_roles(script):
    """Return the roles allowed to run `script`, or ``None`` if unrestricted.

    Normalised to a list so a consumer may declare a single role as a bare
    string without it being read character by character.
    """
    roles = _read_attr(script, OWNING_ROLES_ATTR)
    if roles is None:
        return None
    if isinstance(roles, str):
        return [roles]
    return list(roles)


def is_foreign_script(script) -> tuple:
    """Return ``(blocked, declared, current)`` for `script` on this process.

    A script may declare its home in one of two ways, never both:

    - ``owning_shard`` — exactly one shard owns it. Compared against this
      process's ``SHARD_ID``.
    - ``owning_roles`` — a list of roles that may run it. Compared against
      this process's role. Use for work belonging to every shard but not the
      router, which no single owner can express.

    Declaring neither leaves the script unconfined, which is what keeps
    scripts that genuinely run everywhere working untouched.
    """
    owner = get_owning_shard(script)
    roles = get_owning_roles(script)

    if owner is not None and roles:
        # Contradictory declaration: "only shard0" and "any shard" cannot
        # both hold. Fail closed rather than silently honouring one — see
        # OWNING_ROLES_ATTR.
        logger.log_err(
            f"evennia-shards: script {getattr(script, 'key', '?')!r} declares "
            f"both {OWNING_SHARD_ATTR}={owner!r} and {OWNING_ROLES_ATTR}="
            f"{roles!r}. They are mutually exclusive; refusing to run it "
            f"anywhere until one is removed."
        )
        return True, (owner, roles), None

    if owner is not None:
        current = get_shard_id()
        return owner != current, owner, current

    if roles:
        current = get_role()
        return current not in roles, roles, current

    return False, None, None


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
        blocked, _declared, _current = is_foreign_script(self)
        if blocked:
            return
        return original_start(self, *args, **kwargs)

    @functools.wraps(original_unpause)
    def _guarded_unpause_task(self, *args, **kwargs):
        blocked, _declared, _current = is_foreign_script(self)
        if blocked:
            # Return before reading or clearing _paused_time — the marker
            # must survive for the owning process. This is also what stops
            # the first process to boot sweeping up every marked row
            # regardless of role: Evennia's boot walk calls this method
            # directly, and it is role-blind.
            return
        return original_unpause(self, *args, **kwargs)

    ScriptBase._start_task = _guarded_start_task
    ScriptBase._unpause_task = _guarded_unpause_task
    ScriptBase._evennia_shards_confinement_installed = True
