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
import sys

# ── macOS only: use a bundled, non-Apple SQLite build ────────────────
#
# macOS ships /usr/lib/libsqlite3.dylib, which drives sqlite3_initialize()
# through libdispatch. libdispatch does not survive fork(), so once any
# SQLite connection has been opened, a daemonizing (forking) start deadlocks
# on the child's first SQLite call — silently, with no error or timeout.
# `evennia start` forks on Unix; `--nodaemon` and Windows do not, which is
# why this only bites daemonized starts on macOS.
#
# sqlean.py ships its own statically-linked SQLite, so Apple's library is
# never loaded. The swap must happen before anything imports sqlite3.
#
# Scoped to darwin so Linux (CI, production) keeps the stdlib module and
# this whole block is dead code there. Also a no-op if sqlean isn't
# installed, so a Mac without it still runs — just not daemonized.
if sys.platform == "darwin":
    try:
        import sqlean
        import sqlean.dbapi2

        # sqlean's DBAPI predates a couple of things Django 6's sqlite3
        # backend expects. Its Connection is an immutable C type, so the
        # additions go on a subclass installed via connect(factory=...).
        class _ShardsConnection(sqlean.dbapi2.Connection):
            def getlimit(self, category):
                # Django uses this only to size bulk_create batches.
                # 999 is SQLite's conservative historical default.
                return 999

        _sqlean_connect = sqlean.dbapi2.connect

        def _connect(*args, **kwargs):
            kwargs.setdefault("factory", _ShardsConnection)
            return _sqlean_connect(*args, **kwargs)

        sqlean.dbapi2.connect = _connect
        sqlean.connect = _connect
        sqlean.SQLITE_LIMIT_VARIABLE_NUMBER = 9
        sqlean.dbapi2.SQLITE_LIMIT_VARIABLE_NUMBER = 9

        sys.modules["sqlite3"] = sqlean
        sys.modules["sqlite3.dbapi2"] = sqlean.dbapi2
    except ImportError:
        pass

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
