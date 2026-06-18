# SPDX-License-Identifier: BSD-3-Clause
"""Ticket primitives for shard authentication.

Three CRUD operations on the Ticket table:
- create_ticket: router inserts a ticket when sending a player to a shard
- get_ticket: shard looks up a ticket by token on incoming connection
- delete_ticket: shard deletes the ticket after successful auth

See docs/ticket-auth-flow.md for the full flow.
"""

import uuid


def create_ticket(account_id, character_id, to_shard, client_ip=None):
    """Create a single-use auth ticket for a shard connection.

    Generates a unique token, inserts a Ticket row, and returns the token.
    Called by the router when a player goes IC.

    If ``client_ip`` is provided, the receiving shard will reject
    connections from a different IP (token-theft protection).

    Usage::

        token = create_ticket(account.id, character.id, "shard0",
                              client_ip=session.address)
        # then redirect client to ws://shard:port/websocket?ticket=<token>
    """
    from .models import Ticket

    token = uuid.uuid4().hex
    Ticket.objects.create(
        token=token,
        account_id=account_id,
        character_id=character_id,
        to_shard=to_shard,
        client_ip=client_ip,
    )
    return token


def get_ticket(token, shard_id=None):
    """Look up a ticket by token.

    Returns (True, data_dict) if found and addressed to this shard,
    (False, None) otherwise. Does not delete — caller consumes via
    delete_ticket after successful auth.

    Defaults shard_id to the current SHARD_ID if not provided.
    """
    from .config import get_shard_id
    from .models import Ticket

    if shard_id is None:
        shard_id = get_shard_id()

    try:
        ticket = Ticket.objects.get(token=token)
    except Ticket.DoesNotExist:
        return False, None

    if ticket.to_shard != shard_id:
        return False, None

    return True, {
        "account_id": ticket.account_id,
        "character_id": ticket.character_id,
        "to_shard": ticket.to_shard,
        "client_ip": ticket.client_ip,
    }


def delete_ticket(token):
    """Delete a ticket by token.

    Called after successful auth to consume the ticket (single-use).
    Silent no-op if the token does not exist.
    """
    from .models import Ticket

    Ticket.objects.filter(token=token).delete()
