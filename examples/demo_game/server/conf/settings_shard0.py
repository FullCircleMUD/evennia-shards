"""
Shard-mode settings for the demo game (shard0).

Usage:
    evennia start --settings settings_shard0.py

Imports common shard config, then pins SHARDS_ROLE and SHARD_ID for
shard mode.

Cascade:
    settings_shard0.py (this file)
        -> settings_common_shard_config.py (SHARD_URLS, INSTALLED_APPS)
            -> settings.py (base Evennia config)
"""

from server.conf.settings_common_shard_config import *  # noqa: F401, F403

SHARDS_ROLE = "shard"
SHARD_ID = "shard0"

# Point this shard's home/start room at its own Limbo.
# Replace with the actual PK of the room created for this shard.
DEFAULT_HOME = "#2"
START_LOCATION = "#2"

# Localhost multi-instance testing: offset ports so shard0 doesn't
# collide with the router (which keeps default ports).
WEBSERVER_PORTS = [(4011, 4015)]
WEBSOCKET_CLIENT_PORT = 4012
AMP_PORT = 4016
