# SPDX-License-Identifier: BSD-3-Clause
"""Shard-aware Evennia hook overrides.

These replace Evennia hook methods when the library is active in a role
that needs them. Injected by ``AppConfig.ready()`` via monkey-patch on
the relevant Evennia class.

Currently houses one override:

- ``shard_aware_at_post_login`` — replaces ``DefaultAccount.at_post_login``
  on routers, intercepting Evennia's auto-puppet step and converting it
  to a ticket+redirect to the character's owning shard. See
  DESIGN/library-integration-risks.md for what to diff on Evennia upgrade.
"""

from evennia.utils import logger

from .handoff import _redirect_to_character_shard
from .config import get_shard_url


def _is_redirectable_character(character) -> bool:
    """Return True iff ``character`` is usable as a redirect target.

    A character is redirectable when it's set, has a real ``shard_id``
    (not ``None`` and not the global ``"*"`` sentinel), and that shard
    has a configured URL in ``SHARD_URLS``.
    """
    if character is None:
        return False
    # Router process may hold a stale row in the idmapper if another
    # process moved this character (e.g. cross_shard_move_to updates
    # shard_id and db_location_id together). Refresh from DB before
    # reading so redirect decisions use the live values.
    character.refresh_from_db()
    shard_id = getattr(character, "shard_id", None)
    if not shard_id or shard_id == "*":
        return False
    try:
        get_shard_url(shard_id)
    except (KeyError, ValueError):
        return False
    return True


def shard_aware_at_post_login(self, session=None, **kwargs):
    """Library override of ``DefaultAccount.at_post_login`` on routers.

    Reproduced from Evennia 6.0.0 ``DefaultAccount.at_post_login``. The
    prelude (protocol-flag load, ``logged_in`` OOB, connect-channel msg)
    runs verbatim. The original ``if AUTO_PUPPET_ON_LOGIN`` branch is
    replaced with redirect-or-fallback logic; the ``else`` branch (OOC
    character-select menu) is reproduced for the fallback path.

    Three outcomes:

    - ``_last_puppet`` is set with a usable ``shard_id`` → create a
      ticket and send ``shard_redirect``; the player's browser navigates
      to that shard.
    - ``_last_puppet`` is set but its ``shard_id`` is ``None``, ``"*"``,
      or not in ``SHARD_URLS`` → log a warning and render the OOC menu.
      Login does not fail.
    - ``_last_puppet`` is ``None`` → render the OOC menu silently
      (normal first login).

    See DESIGN/library-integration-risks.md for what to diff on Evennia
    upgrade.
    """
    # ── Reproduced Evennia DefaultAccount.at_post_login prelude ───────
    # Based on Evennia 6.0.0. Diff against upstream on upgrade.
    protocol_flags = self.attributes.get("_saved_protocol_flags", {})
    if session and protocol_flags:
        session.update_flags(**protocol_flags)

    if session:
        session.msg(logged_in={})

    self._send_to_connect_channel(f"|G{self.key} connected|n")
    # ── End reproduced prelude ────────────────────────────────────────

    # Honor the consumer's AUTO_PUPPET_ON_LOGIN setting. When the
    # consumer has disabled auto-puppet, vanilla Evennia's else-branch
    # always renders the OOC menu — none of the library's redirect
    # logic should apply. Short-circuit here so the override stays
    # vanilla-aligned for that case.
    from django.conf import settings as _django_settings
    if not getattr(_django_settings, "AUTO_PUPPET_ON_LOGIN", True):
        self.msg(self.at_look(target=self.characters, session=session), session=session)
        return

    # OOC-return signal: a session whose URL carried ?ticket= was, by
    # construction, the target of a library-issued redirect to this
    # process. On the router that is the OOC-return case from a shard;
    # we render the OOC menu and skip the auto-puppet decision so we
    # don't bounce the player straight back to the shard they just
    # left (which would be an infinite loop). Flag is set in
    # ShardWebSocketClient.onOpen() based on URL presence and stored
    # in protocol_flags so it survives the Portal→Server AMP sync.
    # Leaves Evennia's _last_puppet semantics untouched.
    if (
        session is not None
        and session.protocol_flags.get("SHARDS_TICKET_AUTHED", False)
    ):
        self.msg(self.at_look(target=self.characters, session=session), session=session)
        return

    last_puppet = self.db._last_puppet

    if _is_redirectable_character(last_puppet):
        _redirect_to_character_shard(self, session, last_puppet)
        return

    if last_puppet is not None:
        logger.log_warn(
            f"at_post_login on router: account {self} has _last_puppet="
            f"{last_puppet} but its shard_id="
            f"{getattr(last_puppet, 'shard_id', None)!r} is unusable — "
            "falling back to OOC menu"
        )

    # OOC menu (reproduced from Evennia at_post_login else-branch).
    self.msg(self.at_look(target=self.characters, session=session), session=session)
