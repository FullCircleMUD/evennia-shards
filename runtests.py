# SPDX-License-Identifier: BSD-3-Clause
"""Test runner for evennia-shards.

Runs the library's unit tests against tests/test_settings.py — no gamedir
required. Invoke from the library root:

    python runtests.py
"""
import os
import sys

import django

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.test_settings")
    django.setup()

    from django.conf import settings
    from django.test.utils import get_runner

    runner = get_runner(settings)()
    failures = runner.run_tests(["evennia_shards"])
    sys.exit(bool(failures))
