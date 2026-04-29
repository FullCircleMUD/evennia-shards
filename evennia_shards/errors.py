# SPDX-License-Identifier: BSD-3-Clause
"""Exceptions raised by evennia-shards."""


class ShardIsolationError(Exception):
    """Raised when a process attempts an operation on a row owned by another shard."""


class MessageBusError(Exception):
    """Raised on misuse of the cross-shard message bus."""
