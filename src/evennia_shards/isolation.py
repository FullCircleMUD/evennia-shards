# SPDX-License-Identifier: BSD-3-Clause
"""Shard isolation mechanism: chokepoints + bypass.

Two halves of the same data-integrity mechanism:

- **Chokepoints** — four hook points where the library raises if a save,
  delete, read, or bulk-update would cross a shard ownership boundary.
  Catches accidental cross-shard writes from bugs as loud, traceable
  failures. See ``DESIGN/shard-isolation.md`` for the architectural framing.

- **Bypass** — :func:`shard_writes_allowed_for`, a context manager that
  temporarily lifts the chokepoints for specific objects. The opt-in
  exception for genuine cross-shard operations (handoff, recovery, data
  migration). Caller takes responsibility for the integrity of writes
  made inside the bypass.

The bypass tracks each authorised object two ways:

- ``id(instance)`` — checked by ``pre_save`` and ``pre_delete``, which
  receive the model instance. Works for both saved and unsaved rows
  (a freshly-created object has no pk yet but does have an id).
- ``(model, pk)`` — checked by ``from_db`` (which gets ``cls`` and the
  raw row ``values``) and by ``QuerySet.update`` (which queries the
  affected rows by pk via values_list). Pks are added to this set on
  bypass entry for any object whose pk is set.

Both sets are thread-local and accessed by the chokepoints via
``_bypass_id_set()`` / ``_bypass_pk_set()``. No cross-module state.
"""

import threading
from contextlib import contextmanager


# ── Bypass primitive ──────────────────────────────────────────────────


_bypass_state = threading.local()


def _bypass_id_set():
    """Thread-local set of ``id(instance)`` values currently bypassed.

    Checked by ``pre_save`` and ``pre_delete`` (instance-receiving
    chokepoints).
    """
    if not hasattr(_bypass_state, "ids"):
        _bypass_state.ids = set()
    return _bypass_state.ids


def _bypass_pk_set():
    """Thread-local set of ``(concrete_model, pk)`` tuples currently bypassed.

    Checked by ``from_db`` and ``QuerySet.update`` (which don't get a
    model instance, so pk-keyed identity is what's available).

    Keyed by the *concrete* model class — Evennia's typeclass system uses
    Django proxy models that share the underlying table (e.g. ``Room``,
    ``Character``, ``DefaultObject`` all proxy ``ObjectDB``). The bypass
    has to match across the proxy/concrete boundary because the caller
    typically holds a typeclass instance (proxy) while ``from_db`` is
    called with the concrete model class. Normalising both sides via
    ``_meta.concrete_model`` makes them match.
    """
    if not hasattr(_bypass_state, "pks"):
        _bypass_state.pks = set()
    return _bypass_state.pks


def _concrete_model(model_or_instance):
    """Return the concrete (non-proxy) model class for a model or instance."""
    cls = model_or_instance if isinstance(model_or_instance, type) else type(model_or_instance)
    return cls._meta.concrete_model


@contextmanager
def shard_writes_allowed_for(*objs):
    """Lift the shard-isolation chokepoints for ``objs`` inside this block.

    Use this **only** for genuine cross-shard operations — ownership
    handoff, data migrations, recovery tooling. Inside the block, all
    four chokepoints (pre_save, pre_delete, from_db, QuerySet.update)
    skip enforcement for the listed objects. Caller takes responsibility
    for the integrity of the resulting writes.

    Scoped to the ``with`` block; entries added by this call are removed
    on exit (success or exception). Nesting is safe — only the entries
    added by this call are removed, so an outer bypass keeps its
    objects authorised when an inner one exits.

    Example::

        with shard_writes_allowed_for(character):
            character.shard_id = "shard1"
            character.location_id = remote_room_pk
            character.save()
    """
    ids = _bypass_id_set()
    pks = _bypass_pk_set()

    new_ids = []
    new_pks = []
    for obj in objs:
        oid = id(obj)
        if oid not in ids:
            ids.add(oid)
            new_ids.append(oid)
        if getattr(obj, "pk", None) is not None:
            key = (_concrete_model(obj), obj.pk)
            if key not in pks:
                pks.add(key)
                new_pks.append(key)

    try:
        yield
    finally:
        ids.difference_update(new_ids)
        pks.difference_update(new_pks)


def _bypassed_pks_for_model(model):
    """Return a set of pks currently bypassed for ``model``.

    Used by ``QuerySet.update`` to exclude bypassed rows from its
    foreign-shard scan. Bypass keys are stored under the concrete model
    class (see ``_bypass_pk_set``), so we normalise the queryset's model
    the same way before comparison.
    """
    target = _concrete_model(model)
    return {pk for (m, pk) in _bypass_pk_set() if m is target}


# ── Chokepoint signal handlers ────────────────────────────────────────


def _pre_save_chokepoint(sender, instance, **kwargs):
    """pre_save: combine auto-stamp on None with refusal of remote saves.

    Connected without a ``sender`` filter because Evennia's typeclass
    system uses concrete subclasses of ObjectDB (Room, Character, Exit,
    consumer-defined typeclasses) that share the ObjectDB table. Django
    dispatches pre_save with ``sender = type(instance)``, which is the
    subclass — a ``sender=ObjectDB`` filter would never match. The
    handler does the isinstance check inline.
    """
    from evennia.objects.models import ObjectDB

    if not isinstance(instance, ObjectDB):
        return

    # Bypass: caller has explicitly authorised this write.
    if id(instance) in _bypass_id_set():
        return

    from evennia_shards import get_shard_id
    from evennia_shards.config import ROLE_ROUTER, get_role

    current = get_shard_id()

    # Auto-stamp: new rows with no shard_id get the current shard's ID.
    if instance.shard_id is None:
        instance.shard_id = current
        return

    # Router is exempt — it creates/modifies objects across all shards
    # (chargen, chardelete, setting _last_puppet, etc.).
    if get_role() == ROLE_ROUTER:
        return

    if instance.shard_id == current or instance.shard_id == "*":
        return

    from evennia_shards.errors import ShardIsolationError

    raise ShardIsolationError(
        f"pre_save refused: shard {current!r} cannot persist "
        f"{type(instance).__name__} pk={instance.pk!r} owned by shard "
        f"{instance.shard_id!r}"
    )


def _pre_delete_chokepoint(sender, instance, **kwargs):
    """pre_delete: refuse to delete a row owned by another shard.

    Covers both ``instance.delete()`` and ``qs.delete()`` — Django fires
    pre_delete per affected row even on bulk queryset deletes (it has
    to, for cascade handling). Connected without a sender filter for
    the same reason as pre_save.
    """
    from evennia.objects.models import ObjectDB

    if not isinstance(instance, ObjectDB):
        return

    # Bypass: caller has explicitly authorised this delete.
    if id(instance) in _bypass_id_set():
        return

    from evennia_shards import get_shard_id
    from evennia_shards.config import ROLE_ROUTER, get_role

    # Router is exempt — chardelete and other OOC operations span shards.
    if get_role() == ROLE_ROUTER:
        return

    current = get_shard_id()

    if instance.shard_id is None or instance.shard_id == "*":
        return

    if instance.shard_id == current:
        return

    from evennia_shards.errors import ShardIsolationError

    raise ShardIsolationError(
        f"pre_delete refused: shard {current!r} cannot delete "
        f"{type(instance).__name__} pk={instance.pk!r} owned by shard "
        f"{instance.shard_id!r}"
    )


# ── Install ────────────────────────────────────────────────────────────


def install_chokepoints():
    """Install all four chokepoints on Evennia's ObjectDB.

    Called from ``EvenniaShardsConfig.ready()``. Idempotent for the
    ``from_db`` and ``QuerySet.update`` patches via marker attributes;
    the signal handlers use Django's ``dispatch_uid`` to dedupe.
    """
    from django.db import models
    from django.db.models.signals import pre_delete, pre_save
    from evennia.objects.models import ObjectDB

    # Late-bind shard_id onto Evennia's ObjectDB so the ORM is aware
    # of the column the migration adds. Idempotent — guards against
    # double-installation in dev reload scenarios.
    if not any(f.name == "shard_id" for f in ObjectDB._meta.get_fields()):
        ObjectDB.add_to_class(
            "shard_id",
            models.CharField(max_length=64, null=True, blank=True, db_index=True),
        )

    pre_save.connect(
        _pre_save_chokepoint,
        dispatch_uid="evennia_shards.pre_save_chokepoint",
    )

    pre_delete.connect(
        _pre_delete_chokepoint,
        dispatch_uid="evennia_shards.pre_delete_chokepoint",
    )

    # from_db chokepoint: refuse to construct an instance from a row
    # whose shard_id is owned by another shard. Covers all read paths
    # that produce a Model instance — queryset iteration, raw() queries,
    # and select_related (the three Django call sites that go through
    # Model.from_db). Inherited automatically by typeclass subclasses
    # via Python MRO. Idempotent via marker attribute.
    if not getattr(ObjectDB, "_evennia_shards_from_db_patched", False):
        original_from_db = ObjectDB.from_db.__func__

        def _shard_aware_from_db(cls, db, field_names, values):
            from evennia_shards.config import ROLE_ROUTER, get_role

            if get_role() == ROLE_ROUTER:
                return original_from_db(cls, db, field_names, values)

            field_names_list = list(field_names)

            # Bypass: if this (concrete_model, pk) is in the bypass set,
            # allow the construction regardless of the row's shard_id.
            # Normalise via _concrete_model so a bypass entry stored
            # under (ObjectDB, pk) matches a from_db call where cls is
            # a proxy class (DefaultObject, etc.).
            if "id" in field_names_list:
                pk_idx = field_names_list.index("id")
                pk = values[pk_idx]
                if pk is not None and (_concrete_model(cls), pk) in _bypass_pk_set():
                    return original_from_db(cls, db, field_names, values)

            if "shard_id" in field_names_list:
                idx = field_names_list.index("shard_id")
                row_shard = values[idx]
                if row_shard is not None and row_shard != "*":
                    from evennia_shards import get_shard_id
                    from evennia_shards.errors import ShardIsolationError

                    current = get_shard_id()
                    if row_shard != current:
                        raise ShardIsolationError(
                            f"from_db refused: shard {current!r} cannot "
                            f"instantiate {cls.__name__} with shard_id={row_shard!r}"
                        )
            return original_from_db(cls, db, field_names, values)

        ObjectDB.from_db = classmethod(_shard_aware_from_db)
        ObjectDB._evennia_shards_from_db_patched = True

    # QuerySet.update chokepoint: refuse a bulk update if the queryset
    # would touch any row whose shard_id is neither current nor "*".
    # This is the one write operation Django does not fire signals for,
    # so it needs an explicit override on the QuerySet's update method.
    # Idempotent via a marker on the QuerySet class.
    QuerySetClass = type(ObjectDB.objects.get_queryset())
    if not getattr(QuerySetClass, "_evennia_shards_update_patched", False):
        original_update = QuerySetClass.update

        def _shard_aware_update(self, **kwargs):
            # Only enforce for ObjectDB-derived models in case this
            # queryset class is shared with other Evennia models.
            if not (isinstance(self.model, type) and issubclass(self.model, ObjectDB)):
                return original_update(self, **kwargs)

            from evennia_shards.config import ROLE_ROUTER, get_role

            # Router is exempt from isolation checks.
            if get_role() == ROLE_ROUTER:
                return original_update(self, **kwargs)

            from evennia_shards import get_shard_id
            from evennia_shards.errors import ShardIsolationError

            current = get_shard_id()

            # Find non-owned, non-global, non-NULL shard_ids in the
            # queryset's scope. values_list bypasses from_db (per
            # design — see shard-isolation.md), so this SELECT can
            # read remote rows without raising.
            #
            # Bypass: exclude rows whose pks are in the bypass set.
            # If the entire foreign set is bypassed, the update is
            # allowed; the caller has taken responsibility.
            bypassed_pks = _bypassed_pks_for_model(self.model)

            foreign_qs = (
                self.exclude(shard_id__isnull=True)
                .exclude(shard_id="*")
                .exclude(shard_id=current)
            )
            if bypassed_pks:
                foreign_qs = foreign_qs.exclude(pk__in=bypassed_pks)

            foreign = list(
                foreign_qs.values_list("shard_id", flat=True).distinct()[:5]
            )
            if foreign:
                raise ShardIsolationError(
                    f"qs.update refused: shard {current!r} would touch "
                    f"{self.model.__name__} rows owned by {sorted(set(foreign))!r}"
                )
            return original_update(self, **kwargs)

        QuerySetClass.update = _shard_aware_update
        QuerySetClass._evennia_shards_update_patched = True
