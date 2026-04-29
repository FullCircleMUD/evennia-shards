"""evennia-shards: optional split deployment and sharding for Evennia."""

from .config import get_message_timeout, get_role, get_shard_id
from .errors import ShardIsolationError
from .messagebus import send_message

__version__ = "0.0.1"

__all__ = [
    "get_role",
    "get_shard_id",
    "get_message_timeout",
    "send_message",
    "ShardIsolationError",
    "__version__",
]
