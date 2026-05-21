# SPDX-License-Identifier: BSD-3-Clause
"""Settings for running Evennia's own test suite under router mode.

Library is active with ``SHARDS_ROLE=router``. The tenancy install
runs but ``bootstrap_tenant_context()`` calls ``clear_shard_context()``,
so the auto-filter is inactive (no tenant set → no WHERE clause
injected). The ``shard_id`` column, ``__setattr__`` wrap, and patched
manager methods are still in place; this run isolates their effect
when the filter is off.
"""
from .test_settings import *  # noqa: F401, F403

SHARDS_ROLE = "router"
SHARD_ID = "router"
