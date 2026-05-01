# SPDX-License-Identifier: BSD-3-Clause
"""Shard-aware command overrides.

These replace Evennia's built-in commands when the library is active
(non-monolith). Injected by ``AppConfig.ready()`` via monkey-patch on
the ``evennia.commands.default.account`` module — the AccountCmdSet
picks up our versions on cmdset rebuild.
"""

from evennia.commands.default.account import CmdIC
from evennia.utils import logger, search, utils

from .config import get_role, get_shard_url
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

        if role == "shard":
            self.msg("Return to the router to select a character.")
            return

        # --- Router path: resolve character, then ticket + redirect ---

        new_character = self._resolve_character()
        if new_character is None:
            return  # error messages already sent

        shard_id = new_character.shard_id
        if not shard_id or shard_id == "*":
            self.msg("That character has no shard assignment.")
            return

        account = self.account
        session = self.session

        # Set _last_puppet so the shard's auto-puppet picks up the
        # correct character after ticket auth.
        account.db._last_puppet = new_character

        token = create_ticket(
            account.id, new_character.id, shard_id, client_ip=session.address
        )
        url = f"{get_shard_url(shard_id)}/webclient?ticket={token}"
        session.msg(shard_redirect=[[url], {}])
        self.msg(f"Redirecting to {shard_id}...")

        logger.log_sec(
            f"Shard redirect: (Caller: {account}, Target: {new_character}, "
            f"Shard: {shard_id}, IP: {session.address})."
        )

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
