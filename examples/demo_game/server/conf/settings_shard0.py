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
