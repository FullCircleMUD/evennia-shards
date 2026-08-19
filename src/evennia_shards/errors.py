# SPDX-License-Identifier: BSD-3-Clause
"""Exceptions raised by evennia-shards."""


class MessageBusError(Exception):
    """Raised on misuse of the cross-shard message bus."""


class TicketError(Exception):
    """Raised when a ticket token is invalid, expired, or already consumed."""


class UnstampedInsertError(Exception):
    """Raised when a tenant-tagged row would be INSERTed with no shard stamp.

    A ``shard_id=NULL`` row belongs to no shard: it is invisible to every
    shard's auto-filter and is not a valid IC destination. The guard in
    ``tenancy._tenant_aware_save`` refuses the write rather than let one
    be created silently. See :func:`evennia_shards.allow_unstamped_insert`
    for the one sanctioned way through.
    """
