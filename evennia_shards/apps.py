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

        # Hybrid auto-stamp: any save on ObjectDB (or a subclass) sets
        # shard_id to the current process's shard *only if* shard_id is
        # currently None. Explicit values (e.g. set during a cross-shard
        # handoff) are respected. As a side effect, legacy NULL rows get
        # lazily backfilled on their next save — useful for monolith-to-
        # shard migration, but the explicit RunPython backfill remains the
        # primary mechanism for legacy rows that may never save again.
        #
        # Connected without a sender filter because Evennia's typeclass
        # system uses concrete Django subclasses of ObjectDB (Room,
        # Character, Exit, consumer-defined typeclasses) that share the
        # ObjectDB table. Django dispatches pre_save with `sender =
        # type(instance)`, which is the subclass — so a sender=ObjectDB
        # filter never matches the saves we care about. The handler does
        # the isinstance check in-line.
        pre_save.connect(
            _stamp_shard_id_if_unset,
            dispatch_uid="evennia_shards.stamp_shard_id",
        )


def _stamp_shard_id_if_unset(sender, instance, **kwargs):
    from evennia.objects.models import ObjectDB

    if not isinstance(instance, ObjectDB):
        return

    if instance.shard_id is None:
        from evennia_shards import get_shard_id

        instance.shard_id = get_shard_id()
