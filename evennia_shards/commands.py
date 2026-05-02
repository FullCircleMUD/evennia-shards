# SPDX-License-Identifier: BSD-3-Clause
"""Shard-aware command overrides.

These replace Evennia's built-in commands when the library is active
(non-monolith). Injected by ``AppConfig.ready()`` via monkey-patch on
the ``evennia.commands.default.account`` module — the AccountCmdSet
picks up our versions on cmdset rebuild.
"""

from evennia.commands.default.account import CmdIC, CmdOOC
from evennia.utils import logger, search, utils

from .config import ROLE_SHARD, get_role, get_router_shard_id, get_router_url
from .handoff import _redirect_to_character_shard
from .tickets import create_ticket


class ShardAwareCmdIC(CmdIC):
    """Shard-aware override of Evennia's ``ic`` command.

    - **Router**: resolves the character, sets ``_last_puppet``, creates
      a ticket, and redirects the client to the character's shard.
    - **Shard**: tells the player to return to the router.
    - **Monolith**: never injected (original ``CmdIC`` stays).
    """

    def func(self):
        role = get_role()

        if role == ROLE_SHARD:
            self.msg("Leave this character before trying to enter another one.")
            return

        # --- Router path: resolve character, then ticket + redirect ---

        new_character = self._resolve_character()
        if new_character is None:
            return  # error messages already sent

        shard_id = new_character.shard_id
        if not shard_id or shard_id == "*":
            self.msg("That character has no shard assignment.")
            return

        _redirect_to_character_shard(self.account, self.session, new_character)
        self.msg(f"Redirecting to {shard_id}...")

    def _resolve_character(self):
        """Resolve the target character from command args.

        Replicates the resolution logic from ``CmdIC.func()`` without
        calling ``puppet_object()``.  Returns the character or ``None``
        (with error messages already sent to the caller).
        """
        account = self.account
        session = self.session

        character_candidates = []

        if not self.args:
            character_candidates = (
                [account.db._last_puppet] if account.db._last_puppet else []
            )
            if not character_candidates:
                self.msg("Usage: ic <character>")
                return None
        else:
            if playables := account.characters:
                character_candidates.extend(
                    utils.make_iter(
                        account.search(
                            self.args,
                            candidates=playables,
                            search_object=True,
                            quiet=True,
                        )
                    )
                )

            if account.locks.check_lockstring(account, "perm(Builder)"):
                if session.puppet:
                    character_candidates = [
                        char
                        for char in session.puppet.search(self.args, quiet=True)
                        if char.access(account, "puppet")
                    ]
                if not character_candidates:
                    character_candidates.extend(
                        [
                            char
                            for char in search.object_search(self.args)
                            if char.access(account, "puppet")
                        ]
                    )

        if not character_candidates:
            self.msg("That is not a valid character choice.")
            return None
        if len(character_candidates) > 1:
            self.msg(
                "Multiple targets with the same name:\n %s"
                % ", ".join(
                    "%s(#%s)" % (obj.key, obj.id) for obj in character_candidates
                )
            )
            return None

        return character_candidates[0]


class ShardAwareCmdOOC(CmdOOC):
    """Shard-aware override of Evennia's ``ooc`` command.

    - **Shard**: creates a ticket and redirects the client to the router.
      Always redirects — even if no puppet (error state), because a
      player should never be OOC on a shard.
    - **Router**: never injected (original ``CmdOOC`` stays).
    - **Monolith**: never injected (original ``CmdOOC`` stays).

    No explicit ``unpuppet_object()`` call is needed here. The redirect
    triggers a full page navigation (``window.location.href``), which
    closes the WebSocket connection. Evennia's disconnect handler
    (``sessionhandler.disconnect()`` → ``account.unpuppet_object()``)
    automatically releases the character on the shard when the
    connection drops.
    """

    def func(self):
        account = self.account
        session = self.session

        # Resolve the best character_id for the ticket.
        old_char = account.get_puppet(session)
        if old_char:
            character_id = old_char.id
        elif account.db._last_puppet:
            character_id = account.db._last_puppet.id
        else:
            # Truly broken state — no puppet and no _last_puppet.
            # Log a warning and use 0 as a sentinel; the router
            # won't use character_id for OOC tickets anyway.
            character_id = 0
            logger.log_warn(
                f"OOC redirect with no puppet and no _last_puppet "
                f"(Account: {account}, IP: {session.address})."
            )

        token = create_ticket(
            account.id, character_id, get_router_shard_id(),
            client_ip=session.address,
        )
        url = f"{get_router_url()}/webclient?ticket={token}"
        session.msg(shard_redirect=[[url], {}])
        self.msg("Redirecting to router...")

        logger.log_sec(
            f"OOC redirect: (Caller: {account}, Character: {character_id}, "
            f"IP: {session.address})."
        )
