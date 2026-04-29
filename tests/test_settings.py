# SPDX-License-Identifier: BSD-3-Clause
"""Minimal Django settings for evennia-shards unit tests.

Imports Evennia's defaults, adds the library to INSTALLED_APPS, pins shard mode,
and uses an in-memory sqlite test database. No gamedir required — BaseEvenniaTestCase
forces all gamedir-shaped settings (CMDSET_*, BASE_*_TYPECLASS, ...) to
evennia.game_template fallbacks at test runtime.
"""
import os
import tempfile

from evennia.settings_default import *  # noqa: F401, F403

# Evennia path bits — point at safe scratch locations so settings_default's
# path-derived defaults resolve without needing a real gamedir.
GAME_DIR = tempfile.gettempdir()
LOG_DIR = os.path.join(tempfile.gettempdir(), "evennia_shards_test_logs")
os.makedirs(LOG_DIR, exist_ok=True)

# Library under test
INSTALLED_APPS = list(INSTALLED_APPS) + ["evennia_shards"]

# In-memory test database
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    },
}

# Sharding config — pinned for tests
SHARDS_ROLE = "shard"
SHARD_ID = "shard0"

# Required Django bits
SECRET_KEY = "test-only-secret"
TEST_ENVIRONMENT = True
ROOT_URLCONF = "tests.urls"
