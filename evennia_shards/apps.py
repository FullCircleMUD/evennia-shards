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
        from django.db.models.signals import pre_save
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
