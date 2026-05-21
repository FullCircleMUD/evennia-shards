# SPDX-License-Identifier: BSD-3-Clause
"""Settings for running Evennia's own test suite under shard mode.

Library is active with ``SHARDS_ROLE=shard, SHARD_ID=shard0``. The full
multitenant install runs: ``shard_id`` column added to ObjectDB,
``ObjectDB.objects`` carries the auto-filter ``WHERE shard_id IN
('shard0', '*')``, save/_do_update/__setattr__ wrapped, manager
patched. This is the interesting run — failures here that don't
appear in ``evennia_suite_monolith`` are multitenant-specific.
"""
from .test_settings import *  # noqa: F401, F403

# test_settings.py already pins SHARDS_ROLE="shard", SHARD_ID="shard0"
# and has evennia_shards in INSTALLED_APPS. Re-declared here for
# clarity at the call site.
SHARDS_ROLE = "shard"
SHARD_ID = "shard0"
