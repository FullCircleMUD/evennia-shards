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

# For single-shard, Limbo #2 (created by Evennia's initial_setup) serves
# as the shard's home room — no extra room creation needed. The pre_save
# chokepoint auto-stamps it with SHARD_ID when initial_setup runs.
# For multi-shard, replace with the PK of this shard's landing room.
DEFAULT_HOME = "#2"
START_LOCATION = "#2"

# Localhost multi-instance testing: offset ports so shard0 doesn't
# collide with the router (which keeps default ports).
WEBSERVER_PORTS = [(4011, 4015)]
WEBSOCKET_CLIENT_PORT = 4012
AMP_PORT = 4016
