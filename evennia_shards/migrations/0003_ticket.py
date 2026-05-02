# SPDX-License-Identifier: BSD-3-Clause
"""Create the single-use auth ticket table.

Standard CreateModel migration. The Ticket model is defined in
evennia_shards/models.py. Token is the primary key for fast indexed
lookup on the hot path of incoming shard connections.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("evennia_shards", "0002_message"),
    ]

    operations = [
        migrations.CreateModel(
            name="Ticket",
            fields=[
                ("token", models.CharField(max_length=64, primary_key=True, serialize=False)),
                ("account_id", models.IntegerField()),
                ("character_id", models.IntegerField()),
                ("to_shard", models.CharField(max_length=64, db_index=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "app_label": "evennia_shards",
            },
        ),
    ]
