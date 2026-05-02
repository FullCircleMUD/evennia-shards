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

    # Evennia uses lazy ``evennia.Command``/``CmdSet`` exports that are
    # populated by ``evennia._init()``. Real-runtime entry points (server.py,
    # portal.py, evennia_launcher) call this AFTER ``django.setup()`` has
    # already triggered our app's ``ready()`` — so anything in ``ready()``
    # that transitively imports ``evennia.commands.default.cmdset_character``
    # would hit ``class CmdEvMenuNode(None):`` via ``evmenu`` and explode.
    # Library code that needs lazy exports defers import via ``evennia._init``
    # wrapping; the test runner just calls ``_init()`` explicitly here so the
    # deferred work has somewhere to run.
    import evennia
    evennia._init()

    from django.conf import settings
    from django.test.utils import get_runner

    runner = get_runner(settings)()
    failures = runner.run_tests(["evennia_shards"])
    sys.exit(bool(failures))
