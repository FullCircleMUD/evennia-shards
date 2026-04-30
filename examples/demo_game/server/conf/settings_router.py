"""
Router-mode settings for the demo game.

Usage:
    evennia start --settings settings_router.py

Imports the base settings, then pins SHARDS_ROLE and SHARD_ID for
router mode and adds evennia_shards to INSTALLED_APPS.
"""

from server.conf.settings import *  # noqa: F401, F403

SHARDS_ROLE = "router"
SHARD_ID = "router"
INSTALLED_APPS = list(INSTALLED_APPS) + ["evennia_shards"]
