"""
Common shard configuration shared by all sharded instances.

This file contains settings that apply to the entire sharded game system
(not specific to any one shard or the router). Role-specific settings
files import from here.

Cascade:
    settings_router.py / settings_shard0.py
        -> settings_common_shard_config.py (this file)
            -> settings.py
                -> secret_settings.py
"""

from server.conf.settings import *  # noqa: F401, F403

# Add evennia_shards to all sharded instances.
INSTALLED_APPS = list(INSTALLED_APPS) + ["evennia_shards"]

# Map of shard IDs to their webclient base URLs.
# Used by get_shard_url() to build redirect URLs.
# In production, set these via environment variables.
SHARD_URLS = {
    "router": "http://localhost:4001",
    "shard0": "http://localhost:4001",
}
