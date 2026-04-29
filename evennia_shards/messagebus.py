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

    Raises `MessageBusError` if `to_shard == from_shard` — the bus is for
    cross-shard messaging; same-shard sends are almost always a bug or a
    misconfigured `SHARD_ID`. For deferred work on the same shard, use
    Twisted's `reactor.callLater` or call the function directly.
    """
    from .config import get_shard_id
    from .errors import MessageBusError
    from .models import Message

    if from_shard is None:
        from_shard = get_shard_id()
    if to_shard == from_shard:
        raise MessageBusError(
            f"send_message refused: to_shard ({to_shard!r}) equals from_shard "
            f"({from_shard!r}). The bus is for cross-shard messaging only."
        )
    return Message.objects.create(
        kind=kind,
        payload=payload,
        to_shard=to_shard,
        from_shard=from_shard,
    )


def poll_messages(shard_id: str | None = None):
    """Return all `Message` rows addressed to `shard_id`, oldest first.

    If `shard_id` is omitted, defaults to the current shard's `SHARD_ID`.
    Returns a `QuerySet` — the caller composes further filtering, slicing,
    iteration, or counting. Does not delete or mutate; use `delete_message`
    after a row has been processed.
    """
    from .config import get_shard_id
    from .models import Message

    if shard_id is None:
        shard_id = get_shard_id()
    return Message.objects.filter(to_shard=shard_id).order_by("created_at")


def delete_message(message) -> None:
    """Delete a processed message row.

    Thin wrapper over `message.delete()` so the API surface is consistent
    (send / poll / delete) and so future side-effects (metrics, idempotency
    bookkeeping) can land here without consumer changes.
    """
    message.delete()
