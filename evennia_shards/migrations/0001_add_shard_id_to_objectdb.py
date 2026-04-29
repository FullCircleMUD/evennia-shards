"""Add shard_id column to evennia.objects.ObjectDB.

Cross-app migration: lives in evennia_shards but modifies the schema of
the `objects` app (Evennia's ObjectDB table). Uses RunSQL because Django's
AddField references models in the migration's own app, not another's;
RunSQL does the schema change directly. The corresponding Python field
binding happens in EvenniaShardsConfig.ready().

Dependency anchor: Evennia's `objects` app at version 0013, the latest at
the time this migration was written. May need updating if Evennia adds
later migrations that materially affect ObjectDB.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("objects", "0013_defaultobject_alter_objectdb_id_defaultcharacter_and_more"),
    ]

    operations = [
        migrations.RunSQL(
            sql="ALTER TABLE objects_objectdb ADD COLUMN shard_id VARCHAR(64) NULL;",
            reverse_sql="ALTER TABLE objects_objectdb DROP COLUMN shard_id;",
        ),
    ]
