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

# Router WebSocket URL (used by shards for OOC redirect).
# The library does WebSocket-level redirects: when a player crosses a
# shard boundary, the JS in the webclient closes the current WebSocket
# and opens a new one to this URL with ?ticket=TOKEN appended.
# In production, set via environment variable.
ROUTER_URL = "ws://localhost:4002/"

# Map of shard IDs to their WebSocket URLs.
# Used by get_shard_url() to build IC redirect URLs (same shape as
# ROUTER_URL above). Shard IDs are flexible — name them to match
# your game world. In production, set these via environment variables.
SHARD_URLS = {
    "shard0": "ws://localhost:4012/",
    "shard1": "ws://localhost:4022/",
}

# Telnet disabled for all sharded instances — ticket-based auth is
# websocket-only. Wiring telnet into the ticket system is future work.
TELNET_ENABLED = False
