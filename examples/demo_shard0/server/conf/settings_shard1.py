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

from evennia_shards import ROLE_SHARD

SHARDS_ROLE = ROLE_SHARD
# SHARD_ID is consumer-chosen — descriptive names like "overworld" or
# "underdark" are fine. Only SHARDS_ROLE comes from the library enum.
SHARD_ID = "shard1"

# Shards must auto-puppet — ticket auth logs the player in and Evennia's
# at_post_login reads _last_puppet to puppet the correct character.
# The router sets _last_puppet to the chosen character before redirecting,
# so it may not be the literal last-puppeted character.
AUTO_PUPPET_ON_LOGIN = True

# Replace with the PK of this shard's landing room.
DEFAULT_HOME = "#3"
START_LOCATION = "#3"

# Localhost multi-instance testing: offset ports by 20 from router
# (shard0 uses +10, shard1 uses +20, etc.).
WEBSERVER_PORTS = [(4021, 4025)]
WEBSOCKET_CLIENT_PORT = 4022
AMP_PORT = 4026

# Shards exist to host player sessions, nothing else. They never
# render the webclient page, never serve static assets, never run
# the website. evennia_shards.portal_services registers the
# webclient WebSocket independently when WEBSERVER_ENABLED=False, so
# disabling the webserver here drops the unused HTTP stack
# (reverse-proxy, AJAX webclient, Django views, WEB_PLUGINS_MODULE
# hook chain) without affecting WebSocket sessions.
WEBSERVER_ENABLED = False
