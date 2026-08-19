# SPDX-License-Identifier: BSD-3-Clause
"""evennia-shards: optional split deployment and sharding for Evennia."""

from django.core.exceptions import ImproperlyConfigured

# This import chain touches Django settings at module load time (via
# evennia.utils.logger, pulled in by .handoff). That's fine in real use —
# Evennia always configures settings before importing any INSTALLED_APPS
# entry — but it means this package can't be bare-imported (`import
# evennia_shards`) outside a configured Evennia gamedir. Turn Django's
# generic "settings are not configured" error into one that says what to
# actually do instead of leaving a reader to guess.
try:
    from .config import (
        ROLE_MONOLITH,
        ROLE_ROUTER,
        ROLE_SHARD,
        get_message_timeout,
        get_role,
        get_router_shard_id,
        get_router_url,
        get_shard_id,
        get_shard_url,
        get_ticket_bind_ip,
    )
    from .errors import MessageBusError, TicketError, UnstampedInsertError
    from .handoff import MoveResult, cross_shard_move
    from .messagebus import (
        MessageHandler,
        delete_message,
        poll_messages,
        process_inbox,
        send_message,
        start_message_bus,
    )
    from .messaging import send_cross_shard_message, send_cross_shard_room_message
    from .search import ShardSearchResult, shard_aware_global_search
    from .tenancy import (
        GLOBAL_SHARD_ID,
        Shard,
        allow_unstamped_insert,
        clear_shard_context,
        preserve_tenant_context,
        set_current_shard,
        shard_context,
        unstamped_insert_allowed,
    )
    from .script_confinement import (
        OWNING_ROLES_ATTR,
        OWNING_SHARD_ATTR,
        get_owning_roles,
        get_owning_shard,
    )
    from .tickets import create_ticket, delete_ticket, get_ticket
except ImproperlyConfigured as e:
    raise ImproperlyConfigured(
        "evennia_shards cannot be imported standalone — it's an Evennia "
        "app, not a general-purpose library. Add it to INSTALLED_APPS in "
        "an Evennia gamedir's settings.py and start the game via `evennia "
        "start` (or run its own test suite via runtests.py, which "
        "bootstraps Django first). See "
        "https://github.com/FullCircleMUD/evennia-shards#install for "
        "setup instructions."
    ) from e

__version__ = "0.1.2"

__all__ = [
    "ROLE_MONOLITH",
    "ROLE_ROUTER",
    "ROLE_SHARD",
    "get_role",
    "get_shard_id",
    "get_shard_url",
    "get_router_shard_id",
    "get_router_url",
    "get_message_timeout",
    "get_ticket_bind_ip",
    "send_message",
    "poll_messages",
    "delete_message",
    "MessageHandler",
    "process_inbox",
    "start_message_bus",
    "send_cross_shard_message",
    "send_cross_shard_room_message",
    "create_ticket",
    "get_ticket",
    "delete_ticket",
    "shard_aware_global_search",
    "ShardSearchResult",
    "cross_shard_move",
    "MoveResult",
    "MessageBusError",
    "TicketError",
    "UnstampedInsertError",
    # Multitenant tenancy primitives (replaced shard_writes_allowed_for
    # / ShardIsolationError from the chokepoint era).
    "GLOBAL_SHARD_ID",
    "Shard",
    "set_current_shard",
    "clear_shard_context",
    "shard_context",
    "preserve_tenant_context",
    # Unstamped-INSERT guard: NULL-shard rows are refused, and this is
    # the one sanctioned way through. See tenancy.allow_unstamped_insert.
    "allow_unstamped_insert",
    "unstamped_insert_allowed",
    # Shard-owned scripts: consumers declare ownership by setting the
    # OWNING_SHARD_ATTR Attribute; the library confines the script to that
    # process. See script_confinement.py.
    "OWNING_SHARD_ATTR",
    "OWNING_ROLES_ATTR",
    "get_owning_shard",
    "get_owning_roles",
    "__version__",
]
