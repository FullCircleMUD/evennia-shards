# SPDX-License-Identifier: BSD-3-Clause
"""Create the cross-shard message bus table.

Standard CreateModel migration. The Message model is defined in
evennia_shards/models.py and is library-internal — no cross-app
dependency beyond the prior migration in this app.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("evennia_shards", "0001_add_shard_id_to_objectdb"),
    ]

    operations = [
        migrations.CreateModel(
            name="Message",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("to_shard", models.CharField(max_length=64, db_index=True)),
                ("from_shard", models.CharField(blank=True, max_length=64, null=True)),
                ("kind", models.CharField(max_length=64)),
                ("payload", models.JSONField(default=dict)),
            ],
            options={
                "indexes": [models.Index(fields=["to_shard", "created_at"], name="evennia_sha_to_shar_idx")],
            },
        ),
    ]
