"""evennia-shards: optional split deployment and sharding for Evennia."""

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
)
from .errors import MessageBusError, ShardIsolationError, TicketError
from .handoff import MoveResult, cross_shard_move_to
from .isolation import shard_writes_allowed_for
from .messagebus import (
    MessageHandler,
    delete_message,
    poll_messages,
    process_inbox,
    send_message,
    start_message_bus,
)
from .tickets import create_ticket, delete_ticket, get_ticket

__version__ = "0.0.1"

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
    "send_message",
    "poll_messages",
    "delete_message",
    "MessageHandler",
    "process_inbox",
    "start_message_bus",
    "create_ticket",
    "get_ticket",
    "delete_ticket",
    "shard_writes_allowed_for",
    "cross_shard_move_to",
    "MoveResult",
    "ShardIsolationError",
    "MessageBusError",
    "TicketError",
    "__version__",
]
