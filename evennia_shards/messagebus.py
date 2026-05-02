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


class MessageHandler:
    """Base class for cross-shard message handlers.

    The polling cycle calls `handle(message)` for each polled message.
    Return truthy to mark the message processed (it will be deleted);
    return falsy to defer (the message stays for the next poll cycle).

    Library-shipped kinds get handled in this base class:

    - `ping` — diagnostic round-trip. Replies with a `ping_received`
      message addressed to the original sender, echoing the payload.
    - `ping_received` — silently consumed. (Useful for inspection: poll
      the inbox before the next cycle to observe replies; the consumer
      can override this method to surface them.)

    Consumers extend by subclassing:

        class MyHandler(MessageHandler):
            def handle(self, message):
                if super().handle(message):
                    return True
                if message.kind == "tell":
                    deliver_tell(message.payload)
                    return True
                return False
    """

    def handle(self, message) -> bool:
        if message.kind == "ping":
            return self._handle_ping(message)
        if message.kind == "ping_received":
            return self._handle_ping_received(message)
        if message.kind == "undeliverable_reply":
            return self._handle_undeliverable_reply(message)
        return False

    def _handle_ping(self, message) -> bool:
        from .config import get_shard_id

        current = get_shard_id()
        # If the ping has no usable return address, consume it silently;
        # the ping is "received" but unreplied.
        if message.from_shard is None or message.from_shard == current:
            return True
        send_message(
            kind="ping_received",
            payload={"original_pk": message.pk, "echo": message.payload},
            to_shard=message.from_shard,
            from_shard=current,
        )
        return True

    def _handle_ping_received(self, message) -> bool:
        # Diagnostic reply — consumed silently in the base. Consumers
        # who want to observe ping replies can override this method.
        return True

    def _handle_undeliverable_reply(self, message) -> bool:
        # Notification that an earlier outbound message we sent timed
        # out without being processed. Consumed silently in the base;
        # consumers who care about delivery failure can override to
        # surface it (UI notification, retry logic, metrics, etc.).
        return True


def process_inbox(handler: MessageHandler | None = None) -> int:
    """Run one polling cycle: poll, dispatch, delete on success or timeout.

    Lifecycle per message:
    - handler returns truthy -> success, delete (counted as processed)
    - handler returns falsy AND age <= lifespan -> defer for next cycle
    - handler returns falsy AND age > lifespan -> insert
      `undeliverable_reply` to original `from_shard` (if there is one
      and it isn't us), then delete the original

    A handler that raises is logged and the message is treated as falsy
    for the rest of the cycle — it can still be deferred or timed out
    on this pass.

    Returns the count of messages that were successfully processed by
    the handler (timed-out messages are counted separately in logs but
    not in the return value).

    Pure function — testable without the reactor; the polling loop wraps
    this in a Twisted `LoopingCall`.
    """
    import logging

    from django.utils import timezone

    from .config import get_message_timeout, get_shard_id

    log = logging.getLogger(__name__)

    if handler is None:
        handler = MessageHandler()

    processed = 0
    now = timezone.now()
    for msg in poll_messages():
        try:
            handled = bool(handler.handle(msg))
        except Exception:
            log.exception(
                "MessageHandler raised on pk=%s kind=%r; treating as defer",
                msg.pk,
                msg.kind,
            )
            handled = False

        if handled:
            delete_message(msg)
            processed += 1
            continue

        lifespan = get_message_timeout(msg.kind)
        age = (now - msg.created_at).total_seconds()
        if age <= lifespan:
            continue

        # Aged out. Reply with undeliverable to the original sender, if
        # there is a valid one, then drop the original.
        current = get_shard_id()
        if msg.from_shard and msg.from_shard != current:
            send_message(
                kind="undeliverable_reply",
                payload={
                    "original_kind": msg.kind,
                    "original_payload": msg.payload,
                    "reason": "timeout",
                },
                to_shard=msg.from_shard,
                from_shard=current,
            )
        else:
            log.warning(
                "Message pk=%s kind=%r timed out with no valid from_shard "
                "(%r); dropping without reply",
                msg.pk,
                msg.kind,
                msg.from_shard,
            )
        delete_message(msg)
    return processed


def start_message_bus(
    handler: MessageHandler | None = None,
    interval: float = 0.5,
):
    """Start the cross-shard message bus polling loop.

    Registers a Twisted `LoopingCall` that runs `process_inbox(handler)`
    every `interval` seconds. Call once from the consumer's
    `at_server_start()` hook. Returns the `LoopingCall` so the consumer
    can stop it if needed.

    If `handler` is omitted, the library's base `MessageHandler` is used
    (which currently does nothing — every message defers and eventually
    times out). Consumers wanting message dispatch must pass a subclass.
    """
    from twisted.internet.task import LoopingCall

    loop = LoopingCall(process_inbox, handler)
    loop.start(interval, now=False)
    return loop
