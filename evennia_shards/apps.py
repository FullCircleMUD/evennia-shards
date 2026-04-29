"""Django AppConfig for evennia-shards.

Only loaded when the consumer adds `evennia_shards` to `INSTALLED_APPS`,
which by convention they only do in non-monolith roles.
"""

from django.apps import AppConfig


class EvenniaShardsConfig(AppConfig):
    name = "evennia_shards"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Late-bind shard_id onto Evennia's ObjectDB so the ORM is aware
        # of the column the migration adds. Idempotent — guards against
        # double-installation in dev reload scenarios.
        from django.db import models
        from django.db.models.signals import pre_delete, pre_save
        from evennia.objects.models import ObjectDB

        if not any(f.name == "shard_id" for f in ObjectDB._meta.get_fields()):
            ObjectDB.add_to_class(
                "shard_id",
                models.CharField(max_length=64, null=True, blank=True, db_index=True),
            )

        # pre_save chokepoint: combines auto-stamp (set shard_id to the
        # current shard for new rows where shard_id is None) and write
        # protection (raise ShardIsolationError if a save would persist
        # to a row owned by another shard). The "*" sentinel denotes
        # rows owned by all shards and is allowed.
        #
        # Connected without a sender filter because Evennia's typeclass
        # system uses concrete Django subclasses of ObjectDB (Room,
        # Character, Exit, consumer-defined typeclasses) that share the
        # ObjectDB table. Django dispatches pre_save with `sender =
        # type(instance)`, which is the subclass — so a sender=ObjectDB
        # filter never matches the saves we care about. The handler does
        # the isinstance check in-line.
        pre_save.connect(
            _pre_save_chokepoint,
            dispatch_uid="evennia_shards.pre_save_chokepoint",
        )

        # pre_delete chokepoint: refuse to delete a row owned by another
        # shard. Covers both instance.delete() and qs.delete() — Django
        # fires pre_delete per affected row even on bulk queryset deletes
        # (it has to, for cascade handling). Connected without a sender
        # filter for the same reason as pre_save.
        pre_delete.connect(
            _pre_delete_chokepoint,
            dispatch_uid="evennia_shards.pre_delete_chokepoint",
        )

        # from_db chokepoint: refuse to construct an instance from a row
        # whose shard_id is owned by another shard. Covers all read paths
        # that produce a Model instance — queryset iteration, raw() queries,
        # and select_related (the three Django call sites that go through
        # Model.from_db). Inherited automatically by typeclass subclasses
        # (Room, Character, ...) since they look up from_db via Python's
        # MRO and ObjectDB is the patched class.
        if not getattr(ObjectDB, "_evennia_shards_from_db_patched", False):
            original_from_db = ObjectDB.from_db.__func__

            def _shard_aware_from_db(cls, db, field_names, values):
                field_names_list = list(field_names)
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


def _pre_save_chokepoint(sender, instance, **kwargs):
    from evennia.objects.models import ObjectDB

    if not isinstance(instance, ObjectDB):
        return

    from evennia_shards import get_shard_id
    from evennia_shards.errors import ShardIsolationError

    current = get_shard_id()

    if instance.shard_id is None:
        instance.shard_id = current
        return

    if instance.shard_id == current or instance.shard_id == "*":
        return

    raise ShardIsolationError(
        f"pre_save refused: shard {current!r} cannot persist "
        f"{type(instance).__name__} pk={instance.pk!r} owned by shard "
        f"{instance.shard_id!r}"
    )


def _pre_delete_chokepoint(sender, instance, **kwargs):
    from evennia.objects.models import ObjectDB

    if not isinstance(instance, ObjectDB):
        return

    from evennia_shards import get_shard_id
    from evennia_shards.errors import ShardIsolationError

    current = get_shard_id()

    if instance.shard_id is None or instance.shard_id == "*":
        return

    if instance.shard_id == current:
        return

    raise ShardIsolationError(
        f"pre_delete refused: shard {current!r} cannot delete "
        f"{type(instance).__name__} pk={instance.pk!r} owned by shard "
        f"{instance.shard_id!r}"
    )
