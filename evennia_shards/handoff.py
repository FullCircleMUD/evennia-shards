# SPDX-License-Identifier: BSD-3-Clause
"""Cross-shard handoff primitives.

The library's cross-shard handoff mechanism — operations that move a
row's identity (``shard_id``, location) from one shard process to
another, evict it from the source's idmapper, and redirect any
puppeting sessions to the destination shard.

Houses two public primitives:

- :func:`cross_shard_move_to` — composes the full handoff for an
  object (single-object scope in the current spike): validate, atomic
  DB writes (inside :func:`shard_writes_allowed_for`), idmapper
  eviction, per-session ticket+redirect.
- :func:`_redirect_to_character_shard` — the per-session redirect
  used by the move primitive, the ``ic`` command, and the
  ``at_post_login`` override.

The chokepoint-bypass primitive that the move composes with lives in
:mod:`evennia_shards.isolation`.
"""

from collections import namedtuple

from django.db import transaction
from evennia.utils import logger

from .config import get_shard_url
from .errors import ShardIsolationError
from .isolation import shard_writes_allowed_for
from .tickets import create_ticket


MoveResult = namedtuple(
    "MoveResult",
    ["objects_moved", "sessions_redirected", "failures"],
)
"""Outcome of a :func:`cross_shard_move_to` call.

- ``objects_moved`` (int): number of rows whose ``shard_id`` was
  updated to the new shard. Spike 1: always 1 on success.
- ``sessions_redirected`` (int): number of puppeting sessions for
  which a ticket was created and a ``shard_redirect`` OOB was sent.
- ``failures`` (list[tuple]): per-session failure entries —
  ``(session, exception)`` pairs for redirects that raised. The move
  itself committed; these are post-commit recoverable failures
  (network OOB couldn't be sent, etc.). Player can reconnect.
"""


def cross_shard_move_to(obj, target_shard, target_location_pk):
    """Move ``obj`` to ``target_shard``, into the room at ``target_location_pk``.

    Spike 1 scope: single object, **no recursion through
    ``obj.contents``**. Caller is responsible for ensuring ``obj`` has
    no contents (or is comfortable with contents being orphaned in
    source-shard rows). Recursion lands as a separate spike.

    Steps:

    1. Validate ``target_shard`` is in ``SHARD_URLS``.
    2. Validate ``target_location_pk`` exists and is on
       ``target_shard`` (or is the global ``"*"`` sentinel).
    3. Snapshot the sessions currently puppeting ``obj`` (with their
       accounts) before anything changes — after the session detach
       (step 5) the puppet references will be cleared.
    4. Atomic DB writes + idmapper eviction inside one
       ``transaction.atomic()`` block (with ``shard_writes_allowed_for``
       lifting the chokepoints for ``obj``): update ``obj.shard_id``
       and ``obj.db_location_id``, save, evict from this process's
       idmapper. The eviction is inside the atomic block so a failure
       there rolls back the DB write too. On any exception, a defensive
       second eviction runs in the ``except`` branch — the in-memory
       ``obj.shard_id`` was mutated before save, so even with a
       rolled-back DB the cached instance is stale and shouldn't be
       served from the idmapper afterwards.
    5. Pre-emptive session detach — still inside the
       ``shard_writes_allowed_for`` bypass but outside the atomic
       block. Clears ``session.puppet`` and ``session.puid`` for
       each snapshotted session, and removes the ``"puppeted"`` tag
       from ``obj``.  We cannot call Evennia's full
       ``unpuppet_object()`` because its ``at_post_unpuppet`` hook
       dereferences ``obj.location`` — a FK to the room on
       ``target_shard`` — which triggers ``from_db`` on a foreign
       row not in the bypass set.  The minimal detach is sufficient:
       the disconnect handler's ``obj = session.puppet; if obj:``
       guard finds ``None`` and returns immediately — no save, no
       chokepoint error, no zombie session.  Without this detach,
       the asynchronous disconnect handler (WebSocket close →
       ``portal_disconnect`` → ``at_disconnect`` →
       ``unpuppet_object``) runs outside any bypass and creates a
       zombie session whose stale ``shard_id`` poisons the next
       ``portal_connect`` via ``disconnect_duplicate_sessions``.
    6. For each snapshotted session: create a ticket and send
       ``shard_redirect`` OOB. Per-session failures are captured in
       the returned :class:`MoveResult` and do not roll back the move.

    Args:
        obj: an ``ObjectDB`` instance (typically a Character) on this
            shard, to be moved to ``target_shard``.
        target_shard: the destination shard's ``SHARD_ID``. Must be a
            key in ``SHARD_URLS``.
        target_location_pk: pk of the destination room (or container)
            on the target shard. Must exist; must have ``shard_id ==
            target_shard`` or ``shard_id == "*"``.

    Returns:
        :class:`MoveResult` with counts and per-session failures.

    Raises:
        ShardIsolationError: if ``target_shard`` isn't configured, if
            ``target_location_pk`` doesn't exist, or if it's on a
            shard other than ``target_shard`` (and not global).
    """
    from evennia.objects.models import ObjectDB

    # 1. Validate target shard.
    try:
        get_shard_url(target_shard)
    except (KeyError, ValueError) as exc:
        raise ShardIsolationError(
            f"cross_shard_move_to: target_shard {target_shard!r} is not "
            f"configured (not present in SHARD_URLS)"
        ) from exc

    # 2. Validate target location exists and is on the target shard.
    target_rows = list(
        ObjectDB.objects.filter(pk=target_location_pk)
        .values_list("shard_id", flat=True)[:1]
    )
    if not target_rows:
        raise ShardIsolationError(
            f"cross_shard_move_to: target_location_pk {target_location_pk!r} "
            f"does not exist"
        )
    location_shard = target_rows[0]
    if location_shard != target_shard and location_shard != "*":
        raise ShardIsolationError(
            f"cross_shard_move_to: target_location_pk {target_location_pk!r} "
            f"is on shard {location_shard!r}, not {target_shard!r}"
        )

    # 3. Snapshot sessions before anything changes. After the
    # session detach (step 5) the puppet references will be cleared,
    # and we need the (session, account) pairs for redirect in step 6.
    sessions_to_redirect = [
        (session, session.account) for session in obj.sessions.all()
    ]

    # 4. Atomic DB writes + idmapper eviction + pre-emptive session detach.
    #
    # shard_writes_allowed_for wraps the whole block — both the
    # atomic DB update (step 4) AND the session detach (step 5).
    # The bypass is keyed on id(obj), which stays valid
    # because flush_from_cache only evicts from the idmapper dict;
    # the Python object and its identity are unchanged.
    #
    # transaction.atomic ensures the row update is all-or-nothing.
    # flush_from_cache lives inside the atomic block so an eviction
    # failure (vanishingly rare — it's a dict pop) rolls back the
    # DB write too.
    #
    # On any exception inside the block, the except branch evicts
    # again defensively. The in-memory obj.shard_id was mutated to
    # the new shard before save() ran, so even with a rolled-back DB
    # the cached instance is stale relative to the row; eviction
    # ensures any future idmapper access reloads from the
    # (rolled-back) DB rather than serving the stale Python object.
    try:
        with shard_writes_allowed_for(obj):
            with transaction.atomic():
                obj.shard_id = target_shard
                obj.db_location_id = target_location_pk
                # Suppress Evennia's post-save contents-cache update
                # for this save. Without this,
                # at_db_location_postsave fires after save and
                # dereferences self.db_location, which loads the
                # target room — and the target row lives on
                # target_shard, so from_db refuses (correctly: the
                # move's source process should not be instantiating a
                # remote row). Same flag Evennia itself uses for
                # analogous location-change paths (see
                # ObjectDB.location setter in
                # evennia/objects/models.py).
                obj._safe_contents_update = True
                try:
                    obj.save()
                finally:
                    # Remove the flag whether save succeeded or
                    # raised, so nothing downstream sees it lingering.
                    try:
                        del obj._safe_contents_update
                    except AttributeError:
                        pass
                obj.flush_from_cache(force=True)

            # 5. Pre-emptive session detach — inside bypass, outside
            # atomic.
            #
            # We cannot call Evennia's full unpuppet_object() here
            # because its hooks (at_post_unpuppet) dereference
            # obj.location — a FK to the room, which now lives on
            # target_shard.  That dereference triggers from_db on
            # the room row, and the room is NOT in the bypass set,
            # so from_db refuses.
            #
            # Instead we do the minimum needed to prevent the
            # asynchronous disconnect handler from creating a zombie:
            #
            # a) session.puppet = None — the disconnect handler's
            #    unpuppet_object does ``obj = session.puppet; if
            #    obj:`` — with puppet cleared it returns immediately.
            #    No save, no chokepoint error, no zombie.
            #
            # b) session.puid = None — mirrors Evennia's own
            #    unpuppet_object cleanup; prevents stale puid from
            #    confusing session accounting.
            #
            # c) obj.tags.remove("puppeted") — prevents Evennia's
            #    server_maintenance periodic task from trying to
            #    from_db a now-foreign row via
            #    get_by_tag("puppeted").  Tag operations hit the Tag
            #    model, not ObjectDB, so from_db is not triggered.
            #
            # We intentionally skip the DB-level cleanup (clearing
            # db_sessid, db_account) and the unpuppet hooks
            # (at_pre_unpuppet, at_post_unpuppet).  The destination
            # shard's puppet_object will overwrite db_sessid and
            # db_account when the player arrives, so stale values
            # in those fields are harmless.
            for session, _account in sessions_to_redirect:
                session.puppet = None
                session.puid = None
            try:
                obj.tags.remove("puppeted", category="account")
            except Exception as exc:
                logger.log_warn(
                    f"cross_shard_move_to: puppeted tag removal "
                    f"failed for obj pk={obj.pk}: {exc}"
                )
    except Exception:
        try:
            obj.flush_from_cache(force=True)
        except Exception:
            pass
        raise

    # 6. Per-session redirect. Reached only if the bypass block
    # above completed cleanly — any exception there re-raises and
    # skips this block (the control flow is the guarantee; no flag
    # needed). Uses the pre-snapshotted (session, account) pairs
    # because obj.sessions is now empty after the session detach.
    # Per-session failures are captured but don't roll back the move,
    # and the player can ticket-auth on next reconnect regardless.
    redirected = 0
    failures = []
    for session, account in sessions_to_redirect:
        try:
            _redirect_to_character_shard(account, session, obj)
            redirected += 1
        except Exception as exc:
            logger.log_warn(
                f"cross_shard_move_to: redirect failed for session "
                f"{session!r} on obj pk={obj.pk}: {exc}"
            )
            failures.append((session, exc))

    return MoveResult(
        objects_moved=1,
        sessions_redirected=redirected,
        failures=failures,
    )


def _redirect_to_character_shard(account, session, character) -> str:
    """Set ``_last_puppet``, create a ticket, send ``shard_redirect`` OOB.

    Pure mechanism shared between every router-side entry point that
    needs to redirect a session to a character's owning shard:

    - ``ShardAwareCmdIC`` (manual ``ic <char>``)
    - ``shard_aware_at_post_login`` (login-time auto-puppet)
    - upcoming ``cross_shard_move_to`` (programmatic handoff)

    The caller is responsible for validating ``character.shard_id``
    before calling — this helper assumes a usable shard id (not
    ``None``, not the ``"*"`` sentinel, and resolvable via
    ``get_shard_url``).

    Returns the redirect URL.
    """
    shard_id = character.shard_id

    # Set _last_puppet so the destination shard's auto-puppet picks up
    # the correct character after ticket auth.
    account.db._last_puppet = character

    token = create_ticket(
        account.id, character.id, shard_id, client_ip=session.address,
    )
    url = f"{get_shard_url(shard_id)}/webclient?ticket={token}"
    session.msg(shard_redirect=[[url], {}])

    logger.log_sec(
        f"Shard redirect: (Caller: {account}, Target: {character}, "
        f"Shard: {shard_id}, IP: {session.address})."
    )
    return url
