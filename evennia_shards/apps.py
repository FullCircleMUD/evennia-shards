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
        from evennia.objects.models import ObjectDB

        if not any(f.name == "shard_id" for f in ObjectDB._meta.get_fields()):
            ObjectDB.add_to_class(
                "shard_id",
                models.CharField(max_length=64, null=True, blank=True, db_index=True),
            )
