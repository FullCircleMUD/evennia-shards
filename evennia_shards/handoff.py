# SPDX-License-Identifier: BSD-3-Clause
"""Cross-shard handoff primitives.

The library's cross-shard handoff mechanism — operations that move a
row's identity (``shard_id``, location) from one shard process to
another, evict it from the source's idmapper, and redirect any
puppeting sessions to the destination shard.

Houses two public primitives:

- :func:`cross_shard_character_move` — composes the full handoff for a
  character and its inventory (recursive): validate, atomic DB writes
  (inside :func:`shard_writes_allowed_for`), idmapper eviction,
  per-session ticket+redirect.
- :func:`_redirect_to_character_shard` — the per-session redirect
  used by the move primitive, the ``ic`` command, and the
  ``at_post_login`` override.

The chokepoint-bypass primitive that the move composes with lives in
:mod:`evennia_shards.isolation`.
"""

from collections import namedtuple

from django.db import transaction
from evennia.utils import logger

from .config import get_shard_id, get_shard_url
from .errors import ShardIsolationError
from .isolation import shard_writes_allowed_for
from .tickets import create_ticket


def _collect_all_contents(root_pk):
    """Return pks of every descendant of *root_pk* (breadth-first, exclusive).

    Uses ``values_list`` queries to avoid ``from_db`` / instance
    construction — safe to call for objects on any shard.

    Evennia prevents location loops, so cycle detection is unnecessary.
    """
    from evennia.objects.models import ObjectDB

    all_pks = []
    parents = [root_pk]
    while parents:
        children = list(
            ObjectDB.objects.filter(db_location_id__in=parents)
            .values_list("pk", flat=True)
        )
        if not children:
            break
        all_pks.extend(children)
        parents = children
    return all_pks


def _evict_pks_from_idmapper(pks):
    """Evict a list of pks from the ObjectDB idmapper cache.

    Uses ``flush_from_cache(force=True)`` on instances already in the
    cache (Evennia's API), with a direct dict pop as fallback for pks
    not currently cached.  Both are equivalent today — ``force=True``
    skips ``at_idmapper_flush`` and is just a dict pop — but using the
    method is more future-proof.
    """
    from evennia.objects.models import ObjectDB

    cache = ObjectDB.__dbclass__.__instance_cache__
    for pk in pks:
        cached = cache.get(pk)
        if cached is not None:
            cached.flush_from_cache(force=True)
        else:
            cache.pop(pk, None)


MoveResult = namedtuple(
    "MoveResult",
    ["objects_moved", "sessions_redirected", "failures"],
)
"""Outcome of a :func:`cross_shard_character_move` call.

- ``objects_moved`` (int): number of rows whose ``shard_id`` was
  updated to the new shard (character + inventory contents).
- ``sessions_redirected`` (int): number of puppeting sessions for
  which a ticket was created and a ``shard_redirect`` OOB was sent.
- ``failures`` (list[tuple]): per-session failure entries —
  ``(session, exception)`` pairs for redirects that raised. The move
  itself committed; these are post-commit recoverable failures
  (network OOB couldn't be sent, etc.). Player can reconnect.
"""


def cross_shard_character_move(obj, target_shard, target_location_pk):
    """Move ``obj`` and its inventory to ``target_shard``, into the room at ``target_location_pk``.

    Moves the character and all recursive contents (inventory items,
    bags-within-bags, etc.).  Contents' ``db_location_id`` is unchanged
    — parent pk doesn't change across shards.  Contents with
    ``shard_id == "*"`` (globals) are left alone.

    Steps:

    1. Validate ``target_shard`` is in ``SHARD_URLS``.
    2. Validate ``target_location_pk`` exists and is on
       ``target_shard`` (or is the global ``"*"`` sentinel).
    3. Snapshot the sessions currently puppeting ``obj`` (with their
       accounts) before anything changes — after the session detach
       (step 6) the puppet references will be cleared.
    3b. Collect all descendant pks (recursive inventory) via
       ``_collect_all_contents``.
    4. Atomic DB writes + idmapper eviction inside one
       ``transaction.atomic()`` block (with ``shard_writes_allowed_for``
       lifting the chokepoints for ``obj``): update ``obj.shard_id``
       and ``obj.db_location_id``, save, evict from this process's
       idmapper.
    5. Bulk-update contents' ``shard_id`` to ``target_shard`` and evict
       them from the idmapper.  Single ``qs.update`` — no bypass needed
       because contents have ``shard_id == current_shard``.
    6. Pre-emptive session detach — still inside the
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
       chokepoint error, no zombie session.
    7. For each snapshotted session: create a ticket and send
       ``shard_redirect`` OOB. Per-session failures are captured in
       the returned :class:`MoveResult` and do not roll back the move.

    Args:
        obj: an ``ObjectDB`` instance (typically a Character) on this
            shard, to be moved to ``target_shard``.
        target_shard: the destination shard's ``SHARD_ID``. Must be a
            key in ``SHARD_URLS``.
        target_location_pk: pk of the destination row on the target
            shard (typically a room, but the primitive does not enforce
            that — see "Target typeclass not validated" below). Must
            exist; must have ``shard_id == target_shard`` or
            ``shard_id == "*"``.

    Target typeclass not validated. The primitive checks only that the
    target row exists and is on the target shard. It does not check
    that the target is a Room (vs. a Character, Item, Exit, etc.).
    Considered and rejected: encoding "valid move targets" as a
    library-level rule would put a game concept into the primitive,
    against load-bearing principle 3 (the library does not own game
    concepts). Some games legitimately move characters into vehicles,
    mounts, or containers; the choice of valid destinations is the
    consumer's. Consumer-side typeclass code (e.g. a ``CrossShardExit``
    or a teleport command) should validate the target's typeclass
    before calling this primitive.

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
            f"cross_shard_character_move: target_shard {target_shard!r} is not "
            f"configured (not present in SHARD_URLS)"
        ) from exc

    # 2. Validate target location exists and is on the target shard.
    target_rows = list(
        ObjectDB.objects.filter(pk=target_location_pk)
        .values_list("shard_id", flat=True)[:1]
    )
    if not target_rows:
        raise ShardIsolationError(
            f"cross_shard_character_move: target_location_pk {target_location_pk!r} "
            f"does not exist"
        )
    location_shard = target_rows[0]
    if location_shard != target_shard and location_shard != "*":
        raise ShardIsolationError(
            f"cross_shard_character_move: target_location_pk {target_location_pk!r} "
            f"is on shard {location_shard!r}, not {target_shard!r}"
        )

    # 3. Snapshot sessions before anything changes. After the
    # session detach (step 6) the puppet references will be cleared,
    # and we need the (session, account) pairs for redirect in step 7.
    sessions_to_redirect = [
        (session, session.account) for session in obj.sessions.all()
    ]

    # 3b. Collect all descendant pks (recursive inventory).
    content_pks = _collect_all_contents(obj.pk)

    # 4. Atomic DB writes + idmapper eviction + pre-emptive session detach.
    #
    # shard_writes_allowed_for wraps the whole block — both the
    # atomic DB update (steps 4-5) AND the session detach (step 6).
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

                # 5. Bulk-update contents' shard_id and evict from
                # idmapper.  No bypass needed — contents have
                # shard_id == current_shard, so the custom
                # QuerySet.update chokepoint allows the write.
                # Filter by current shard to skip globals ("*"),
                # NULLs, and any foreign strays.
                if content_pks:
                    current_shard = get_shard_id()
                    contents_updated = ObjectDB.objects.filter(
                        pk__in=content_pks, shard_id=current_shard,
                    ).update(shard_id=target_shard)
                    _evict_pks_from_idmapper(content_pks)
                else:
                    contents_updated = 0

            # 6. Pre-emptive session detach — inside bypass, outside
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
                    f"cross_shard_character_move: puppeted tag removal "
                    f"failed for obj pk={obj.pk}: {exc}"
                )
    except Exception:
        # Defensive eviction — idmapper pops aren't rolled back by
        # transaction.atomic, so purge stale entries on failure too.
        try:
            obj.flush_from_cache(force=True)
        except Exception:
            pass
        try:
            _evict_pks_from_idmapper(content_pks)
        except Exception:
            pass
        raise

    # 7. Per-session redirect. Reached only if the bypass block
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
                f"cross_shard_character_move: redirect failed for session "
                f"{session!r} on obj pk={obj.pk}: {exc}"
            )
            failures.append((session, exc))

    return MoveResult(
        objects_moved=1 + contents_updated,
        sessions_redirected=redirected,
        failures=failures,
    )


def _redirect_to_character_shard(account, session, character) -> str:
    """Set ``_last_puppet``, create a ticket, send ``shard_redirect`` OOB.

    Pure mechanism shared between every router-side entry point that
    needs to redirect a session to a character's owning shard:

    - ``ShardAwareCmdIC`` (manual ``ic <char>``)
    - ``shard_aware_at_post_login`` (login-time auto-puppet)
    - ``cross_shard_character_move`` (programmatic handoff)

    The caller is responsible for validating ``character.shard_id``
    before calling — this helper assumes a usable shard id (not
    ``None``, not the ``"*"`` sentinel, and resolvable via
    ``get_shard_url``).

    The OOB payload is a WebSocket URL with the ticket as a query
    parameter. The client closes its current WebSocket and opens a
    new one to this URL; the destination shard's ``onOpen`` validates
    the ticket and auto-logs-in the session.

    Returns the redirect URL (the WebSocket URL with ticket).
    """
    shard_id = character.shard_id

    # Set _last_puppet so the destination shard's auto-puppet picks up
    # the correct character after ticket auth.
    account.db._last_puppet = character

    # Clear the OOC-menu marker. The player is going IC; on the next
    # connection (refresh, reconnect, fresh login) the router's
    # at_post_login should not suppress AUTO_PUPPET on this account.
    # Set on the router by protocols.py priority #2 ticket auth;
    # cleared here at every IC entry point (manual @ic,
    # login-time auto-redirect, and cross_shard_character_move).
    # Router-side calls hit the router's own AttributeHandler — the
    # symmetric clean case. Shard-side calls (cross_shard move) write
    # locally on the shard's cache; the router will see stale True
    # until its cache evicts naturally, but the worst case is the
    # player landing at OOC menu after a forced move and typing @ic
    # to proceed. Acceptable degradation; not worth a cross-process
    # invalidation primitive.
    account.db._shards_at_ooc_menu = False

    token = create_ticket(
        account.id, character.id, shard_id, client_ip=session.address,
    )
    url = f"{get_shard_url(shard_id)}?ticket={token}"
    session.msg(shard_redirect=[[url], {}])

    logger.log_sec(
        f"Shard redirect: (Caller: {account}, Target: {character}, "
        f"Shard: {shard_id}, IP: {session.address})."
    )
    return url
