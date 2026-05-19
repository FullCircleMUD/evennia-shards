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

from evennia.commands.cmdhandler import InterruptCommand
from evennia.commands.default.building import CmdTeleport

from .search import shard_aware_global_search


class ShardAwareCmdTeleport(CmdTeleport):
    """Shard-aware override of Evennia's ``@teleport`` / ``@tel``.

    Installed via module-attribute swap on
    ``evennia.commands.default.building.CmdTeleport`` (see
    ``apps.py``'s ``_shards_wrapped_init``). The standard
    ``CharacterCmdSet`` references ``building.CmdTeleport`` via module
    attribute at cmdset-rebuild time, so the swap is picked up
    transparently.

    Design shape:

    - ``parse()`` mirrors vanilla 1:1, just substituting the three
      ``caller.search(global_search=True)`` calls with
      :func:`shard_aware_global_search` from the helper module. The
      result is stashed onto ``self`` in the same slots vanilla uses
      (``self.obj_to_teleport`` / ``self.destination``) — with the
      instance present when the match is local, ``None`` when foreign.
      Extra fields (``self.obj_pk`` / ``self.obj_shard`` /
      ``self.dest_pk`` / ``self.dest_shard``) carry the cross-shard
      routing data ``func`` needs when delegation isn't safe.

    - ``func()`` dispatches into three branches:
        1. ``/tonone`` — if obj is local, delegate to vanilla; if
           foreign, refuse with the cross_shard_move pointer.
        2. Both obj and destination are local instances — delegate to
           vanilla. All vanilla complexity (lock checks, equality
           checks, ``/loc`` / ``/intoexit`` / ``/quiet`` modifiers,
           the move itself) stays in vanilla; we don't reimplement it.
        3. At least one of obj / destination is cross-shard — for now
           refuse with a "not yet implemented" message. Subsequent
           commits implement these branches one at a time.

    Per-branch implementation strategy lives in DESIGN/ once the
    branches are filled in; the scaffold here just proves the dispatch
    structure is sound.
    """

    def parse(self):
        # Arg split via CmdTeleport's parent (MuxCommand, in the
        # default Evennia stack). Skips vanilla CmdTeleport.parse's
        # body, which would do the unsafe caller.search(global_search=True)
        # calls and trip the from_db chokepoint on cross-shard matches.
        # The lhs/rhs splitting (via rhs_split = ("=", " to ")) lives in
        # MuxCommand.parse, not in Command.parse — so we have to reach
        # the immediate parent, not the bare base.
        super(CmdTeleport, self).parse()

        # Vanilla's default state (slots that func() reads).
        self.obj_to_teleport = self.caller
        self.destination = None

        # Extra routing fields (None when not applicable / not found).
        self.obj_pk = None
        self.obj_shard = None
        self.dest_pk = None
        self.dest_shard = None

        # Mirror vanilla's three-call structure with shard-aware lookups.
        if self.rhs:
            obj_result = shard_aware_global_search(self.caller, self.lhs)
            if obj_result.state == "not_found":
                self.msg("Did not find object to teleport.")
                raise InterruptCommand
            if obj_result.state == "multiple":
                # TODO: render full disambiguation prompt like vanilla.
                # For the scaffold, refuse and prompt for dbref.
                self.msg(
                    f"Multiple matches for {self.lhs!r}; specify by dbref."
                )
                raise InterruptCommand
            # state == "found"
            self.obj_to_teleport = obj_result.obj  # None if cross-shard
            self.obj_pk = obj_result.pk
            self.obj_shard = obj_result.shard_id

            dest_result = shard_aware_global_search(self.caller, self.rhs)
            if dest_result.state == "found":
                self.destination = dest_result.obj  # None if cross-shard
                self.dest_pk = dest_result.pk
                self.dest_shard = dest_result.shard_id
            # If destination not found, vanilla func() handles the
            # "Destination not found." message via its existing
            # `if not destination: ...` guard.

        elif self.lhs:
            dest_result = shard_aware_global_search(self.caller, self.lhs)
            if dest_result.state == "found":
                self.destination = dest_result.obj  # None if cross-shard
                self.dest_pk = dest_result.pk
                self.dest_shard = dest_result.shard_id

    def func(self):
        # Branch 1 — /tonone. Vanilla's body handles obj at /tonone time;
        # we only need to guard against a foreign obj (which we can't
        # None-ify from this process).
        if "tonone" in self.switches:
            if self.obj_to_teleport is None:
                # Foreign obj — vanilla would crash on the next
                # attribute access. Refuse with the pointer to the
                # cross-shard primitive.
                self.caller.msg(
                    f"|rCross-shard /tonone not yet implemented.|n "
                    f"Object {self.lhs!r} is on shard "
                    f"{self.obj_shard!r}; switch shards first with "
                    f"|wcross_shard_move|n."
                )
                return
            super().func()
            return

        # Branch 2 — both targets local. Vanilla handles everything:
        # lock checks, equality checks, /loc, /intoexit, /quiet, the
        # actual move, the announce messages, the failure paths.
        if self.obj_to_teleport is not None and self.destination is not None:
            super().func()
            return

        # Branch 3 — at least one target is cross-shard. Each sub-case
        # gets its own implementation in a subsequent commit; for now,
        # surface a clear "not yet implemented" so the dispatch
        # structure can be smoke-tested.

        if self.obj_to_teleport is None:
            # Foreign obj. Under our current scope this is refused —
            # the admin should switch shards before trying to move
            # something local to the destination shard.
            self.caller.msg(
                f"|rCross-shard teleport of a foreign object not "
                f"yet implemented.|n Object {self.lhs!r} is on shard "
                f"{self.obj_shard!r}; switch shards first with "
                f"|wcross_shard_move|n."
            )
            return

        # obj is local, destination is cross-shard. This is the
        # "I want to teleport (myself or a local object) to a room on
        # another shard" path — wraps cross_shard_character_move.
        # Stub for now.
        self.caller.msg(
            f"|rCross-shard teleport to foreign destination not yet "
            f"implemented.|n Destination is on shard "
            f"{self.dest_shard!r} (pk={self.dest_pk!r}). "
            f"Use |wcross_shard_move|n directly for now."
        )
