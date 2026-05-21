# SPDX-License-Identifier: BSD-3-Clause
"""Settings for running Evennia's own test suite as a monolith baseline.

Library is NOT in INSTALLED_APPS (the project's convention is that
monolith-mode consumers don't add it). Any failures here belong to
the Evennia-suite + our settings choices (e.g. ROOT_URLCONF), not to
the library. Diff against ``evennia_suite_shard`` to isolate
multitenant-specific behaviour.
"""
from .test_settings import *  # noqa: F401, F403

# Monolith baseline: library dormant, no app registration, no patches.
INSTALLED_APPS = [app for app in INSTALLED_APPS if app != "evennia_shards"]
SHARDS_ROLE = "monolith"
