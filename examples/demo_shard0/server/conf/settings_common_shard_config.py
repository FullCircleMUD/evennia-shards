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

import os

from server.conf.settings import *  # noqa: F401, F403

# Shared database: all instances (router, shard0, shard1, ...) use the same
# DB file in demo_shard0/server/. os.path.realpath resolves symlinks so this
# works regardless of which game directory we're running from.
_CONF_DIR = os.path.dirname(os.path.realpath(__file__))
DATABASES["default"]["NAME"] = os.path.join(_CONF_DIR, "..", "evennia.db3")

# Add evennia_shards to all sharded instances.
INSTALLED_APPS = list(INSTALLED_APPS) + ["evennia_shards"]

# Router webclient base URL (used by shards for OOC redirect).
# In production, set via environment variable.
ROUTER_URL = "http://localhost:4001"

# Map of shard IDs to their webclient base URLs.
# Used by get_shard_url() to build IC redirect URLs.
# Shard IDs are flexible — name them to match your game world.
# In production, set these via environment variables.
SHARD_URLS = {
    "shard0": "http://localhost:4011",
    "shard1": "http://localhost:4021",
}

# Telnet disabled for all sharded instances — ticket-based auth is
# websocket-only. Wiring telnet into the ticket system is future work.
TELNET_ENABLED = False
