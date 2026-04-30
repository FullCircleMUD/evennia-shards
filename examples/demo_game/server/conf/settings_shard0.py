"""
Shard-mode settings for the demo game (shard0).

Usage:
    evennia start --settings settings_shard0.py

Imports the base settings, then pins SHARDS_ROLE and SHARD_ID for
shard mode and adds evennia_shards to INSTALLED_APPS.
"""

from server.conf.settings import *  # noqa: F401, F403

SHARDS_ROLE = "shard"
SHARD_ID = "shard0"
INSTALLED_APPS = list(INSTALLED_APPS) + ["evennia_shards"]
