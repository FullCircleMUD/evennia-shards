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

# AUTO_PUPPET_ON_LOGIN is deliberately not set here — the router inherits
# the consumer's setting from settings.py. The router intercepts the
# auto-puppet flow and converts it to a ticket redirect to the correct
# shard, so both True and False work correctly.

# Localhost multi-instance testing: router uses default Evennia ports.
WEBSERVER_PORTS = [(4001, 4005)]
WEBSOCKET_CLIENT_PORT = 4002
AMP_PORT = 4006
