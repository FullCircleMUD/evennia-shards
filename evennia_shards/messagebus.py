# SPDX-License-Identifier: BSD-3-Clause
"""Cross-shard message bus primitives.

Low-level CRUD over the `Message` table. Higher-level pieces — the
polling cycle, dispatch, undeliverable_reply lifecycle, and consumer-
overrideable handler hook — build on these. See
DESIGN/cross-shard-message-bus.md for the full design.
"""

def send_message(
    kind: str,
    payload: dict,
    to_shard: str,
    from_shard: str | None = None,
):
    """Insert a message row addressed to `to_shard`.

    If `from_shard` is omitted, defaults to the current shard's `SHARD_ID`.
    Returns the created `Message` instance.
    """
    from .config import get_shard_id
    from .models import Message

    if from_shard is None:
        from_shard = get_shard_id()
    return Message.objects.create(
        kind=kind,
        payload=payload,
        to_shard=to_shard,
        from_shard=from_shard,
    )
