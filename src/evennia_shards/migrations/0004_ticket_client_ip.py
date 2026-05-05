# SPDX-License-Identifier: BSD-3-Clause
"""Add client_ip to Ticket for IP-pinned token validation."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("evennia_shards", "0003_ticket"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="client_ip",
            field=models.GenericIPAddressField(null=True, blank=True),
        ),
    ]
