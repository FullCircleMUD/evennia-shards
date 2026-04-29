# SPDX-License-Identifier: BSD-3-Clause
"""Exceptions raised by the shard-isolation chokepoints."""


class ShardIsolationError(Exception):
    """Raised when a process attempts an operation on a row owned by another shard."""
