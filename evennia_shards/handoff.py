# SPDX-License-Identifier: BSD-3-Clause
"""Cross-shard handoff primitives.

This module is the home for the library's cross-shard handoff
mechanism — the operations that move a logical session (and, in
future spikes, an object's row identity and idmapper presence)
from one shard process to another.

Currently houses the session-redirect helper used by all
router-side entry points that need to "send the player's WebSocket
session to a different shard." Future Phase 2 work — the
``cross_shard_move_to`` primitive and the ``shard_writes_allowed_for``
chokepoint bypass — will land alongside it here.
"""

from evennia.utils import logger

from .config import get_shard_url
from .tickets import create_ticket


def _redirect_to_character_shard(account, session, character) -> str:
    """Set ``_last_puppet``, create a ticket, send ``shard_redirect`` OOB.

    Pure mechanism shared between every router-side entry point that
    needs to redirect a session to a character's owning shard:

    - ``ShardAwareCmdIC`` (manual ``ic <char>``)
    - ``shard_aware_at_post_login`` (login-time auto-puppet)
    - upcoming ``cross_shard_move_to`` (programmatic handoff)

    The caller is responsible for validating ``character.shard_id``
    before calling — this helper assumes a usable shard id (not
    ``None``, not the ``"*"`` sentinel, and resolvable via
    ``get_shard_url``).

    Returns the redirect URL.
    """
    shard_id = character.shard_id

    # Set _last_puppet so the destination shard's auto-puppet picks up
    # the correct character after ticket auth.
    account.db._last_puppet = character

    token = create_ticket(
        account.id, character.id, shard_id, client_ip=session.address,
    )
    url = f"{get_shard_url(shard_id)}/webclient?ticket={token}"
    session.msg(shard_redirect=[[url], {}])

    logger.log_sec(
        f"Shard redirect: (Caller: {account}, Target: {character}, "
        f"Shard: {shard_id}, IP: {session.address})."
    )
    return url
