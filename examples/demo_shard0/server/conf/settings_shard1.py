"""
Shard-mode settings for the demo game (shard1).

Usage:
    evennia start --settings settings_shard1.py

Imports common shard config, then pins SHARDS_ROLE and SHARD_ID for
shard mode.

Cascade:
    settings_shard1.py (this file)
        -> settings_common_shard_config.py (SHARD_URLS, INSTALLED_APPS)
            -> settings.py (base Evennia config)
"""

from server.conf.settings_common_shard_config import *  # noqa: F401, F403

SHARDS_ROLE = "shard"
SHARD_ID = "shard1"

# Replace with the PK of this shard's landing room.
DEFAULT_HOME = "#3"
START_LOCATION = "#3"

# Localhost multi-instance testing: offset ports by 20 from router
# (shard0 uses +10, shard1 uses +20, etc.).
WEBSERVER_PORTS = [(4021, 4025)]
WEBSOCKET_CLIENT_PORT = 4022
AMP_PORT = 4026
