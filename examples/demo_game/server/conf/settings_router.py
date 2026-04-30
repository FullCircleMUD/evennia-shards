"""
Router-mode settings for the demo game.

Usage:
    evennia start --settings settings_router.py

Imports common shard config, then pins SHARDS_ROLE and SHARD_ID for
router mode.

Cascade:
    settings_router.py (this file)
        -> settings_common_shard_config.py (SHARD_URLS, INSTALLED_APPS)
            -> settings.py (base Evennia config)
"""

from server.conf.settings_common_shard_config import *  # noqa: F401, F403

SHARDS_ROLE = "router"
SHARD_ID = "router"
