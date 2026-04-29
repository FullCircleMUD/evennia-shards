# SPDX-License-Identifier: BSD-3-Clause
"""Models defined by evennia-shards.

Currently just `Message` — the cross-shard message bus row. See
DESIGN/cross-shard-message-bus.md for the full design.
"""

from django.db import models


class Message(models.Model):
    """A cross-shard bus message: one row addressed to one recipient shard.

    Senders insert; recipients poll-process-delete. Transient communication,
    not persistent storage — see DESIGN/cross-shard-message-bus.md.
    """

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    to_shard = models.CharField(max_length=64, db_index=True)
    from_shard = models.CharField(max_length=64, null=True, blank=True)
    kind = models.CharField(max_length=64)
    payload = models.JSONField(default=dict)

    class Meta:
        app_label = "evennia_shards"
        indexes = [models.Index(fields=["to_shard", "created_at"])]
