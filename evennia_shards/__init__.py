"""evennia-shards: optional split deployment and sharding for Evennia."""

from .config import get_role, get_shard_id

__version__ = "0.0.1"

__all__ = ["get_role", "get_shard_id", "__version__"]
