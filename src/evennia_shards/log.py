# SPDX-License-Identifier: BSD-3-Clause
"""Logging shim for evennia-shards.

See docs/logging.md for the design rationale. One public helper:

    shard_log(message, level="INFO", trace=False, security=False)

routes a line into ``shards.log`` (co-located with Evennia's other logs
under ``settings.LOG_DIR``) via Evennia's built-in
``evennia.utils.logger.log_file``. The filename is hardcoded and the
format is fixed:

    <ISO-8601 timestamp> [<LEVEL>] <message>

Outside an Evennia engine (tests, any caller where Evennia is not
bootstrapped), ``shard_log`` is a silent no-op: the import of
``evennia.utils.logger`` is lazy and an ``ImportError`` is swallowed.
The library deliberately does not fall back to stderr or a local file
in that case.

Two parameters diverge from the sibling shims in ``evennia-world-builder``
and ``evennia-mob-spawner``, each for a use site this library actually
has:

- ``trace=True`` appends the current exception traceback. Evennia's
  ``log_file`` has no equivalent of ``logging.exception()``, so without
  this the message-bus handler-failure path would lose its stack trace.
- ``security=True`` additionally emits via ``logger.log_sec``. Shard and
  router redirects are account/IP-bearing audit records; they belong in
  the library's own log *and* in Evennia's security log, which is the
  surface a security review reads. Dual-write rather than move.
"""
import traceback
from datetime import datetime, timezone

_LOG_FILENAME = "shards.log"
_VALID_LEVELS = ("INFO", "WARN", "ERROR")


def shard_log(
    message: str,
    level: str = "INFO",
    trace: bool = False,
    security: bool = False,
) -> None:
    """Emit one line to ``shards.log``.

    ``level`` is coerced to ``INFO`` if not one of ``INFO``/``WARN``/
    ``ERROR``. A log call must never raise into the caller, so unknown
    levels degrade gracefully rather than rejecting.

    ``trace`` appends the active exception's traceback — call it from
    inside an ``except`` block, where ``format_exc()`` has something to
    report. Outside one it is a no-op, not an error.

    ``security`` additionally routes the message to Evennia's security
    log via ``logger.log_sec``, for records an operator would expect to
    find there as well as here.
    """
    try:
        from evennia.utils import logger
    except ImportError:
        return

    if level not in _VALID_LEVELS:
        level = "INFO"

    if trace:
        formatted = traceback.format_exc()
        # format_exc() returns the string "NoneType: None\n" when no
        # exception is being handled. Appending that would be noise.
        if formatted and not formatted.startswith("NoneType: None"):
            message = f"{message}\n{formatted.rstrip()}"

    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    logger.log_file(
        f"{timestamp} [{level}] {message}", filename=_LOG_FILENAME
    )

    if security:
        logger.log_sec(message)
