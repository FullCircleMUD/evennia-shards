"""evennia-shards: optional split deployment and sharding for Evennia."""

from .config import get_message_timeout, get_role, get_shard_id
from .errors import MessageBusError, ShardIsolationError, TicketError
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
    "get_role",
    "get_shard_id",
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
    "ShardIsolationError",
    "MessageBusError",
    "TicketError",
    "__version__",
]
