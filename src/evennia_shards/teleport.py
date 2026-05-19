# SPDX-License-Identifier: BSD-3-Clause
"""Shard-aware override of Evennia's ``@teleport`` / ``@tel``.

This module is imported only *after* ``evennia._init()`` has run — see
``apps.py``'s ``_shards_wrapped_init``. The reason is the import chain:
``evennia.commands.default.building`` pulls in ``prototypes/menus``
which pulls in ``evmenu``, and ``evmenu`` defines a class that
subclasses ``evennia.Command`` at module load time. ``evennia.Command``
is a lazy export populated by ``evennia._init()``; before that runs,
the name resolves to ``None``, and the class definition raises
``TypeError: NoneType takes no arguments``. The library's ``apps.py``
defers any code that depends on this chain into a wrapper around
``evennia._init`` so the imports happen at the right moment.

Keeping the class in its own module lets ``commands.py`` (which is
imported at ``AppConfig.ready()`` time, before ``evennia._init``)
stay free of the bomb-triggering ``building`` import.
"""

from evennia.commands.default.building import CmdTeleport


class ShardAwareCmdTeleport(CmdTeleport):
    """Shard-aware override of Evennia's ``@teleport`` / ``@tel``.

    WIP — skeleton only. Installed via module-attribute swap on
    ``evennia.commands.default.building.CmdTeleport`` (see
    ``apps.py``'s ``_shards_wrapped_init``). The standard
    ``CharacterCmdSet`` references ``building.CmdTeleport`` via module
    attribute at cmdset-rebuild time, so the swap is picked up
    transparently.

    The eventual job: detect which of vanilla's execution paths the
    operator invoked (self-move, move-arbitrary-object, /tonone,
    /intoexit, /loc) and route each through shard-safe lookups —
    using the library's ``cross_shard_character_move`` primitive
    when the target / object lives on a different shard, falling
    through to vanilla's body when everything is local.

    The current implementation is a hello-world stub that proves the
    module-attribute swap is wired correctly. ``parse()`` is overridden
    to a no-op so vanilla's ``caller.search(global_search=True)`` calls
    (three of them) never fire — those would trip the ``from_db``
    chokepoint on any cross-shard match before we get a chance to route
    to a sharded fallback. ``func()`` just announces itself.

    Branch-routing scaffold and per-branch logic land in subsequent
    commits.
    """

    def parse(self):
        # Intentionally skipping the vanilla parse body. The full
        # shard-aware parse will be reintroduced piece by piece as
        # each execution-path branch lands. For the hello-world stub,
        # we just need the skeleton to load without raising.
        return

    def func(self):
        self.caller.msg(
            "|cShardAwareCmdTeleport|n: hello world from the library override. "
            "Branch-routing scaffold not yet implemented."
        )
