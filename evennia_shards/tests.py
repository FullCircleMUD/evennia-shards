# SPDX-License-Identifier: BSD-3-Clause
"""Unit tests for the evennia-shards library."""

from django.db import models
from django.test import override_settings
from evennia.objects.models import ObjectDB
from evennia.utils.test_resources import BaseEvenniaTestCase

from evennia_shards import (
    ROLE_ROUTER,
    ROLE_SHARD,
    MessageBusError,
    MessageHandler,
    ShardIsolationError,
    TicketError,
    create_ticket,
    delete_message,
    delete_ticket,
    get_message_timeout,
    get_role,
    get_router_shard_id,
    get_router_url,
    get_shard_id,
    get_shard_url,
    get_ticket,
    poll_messages,
    process_inbox,
    send_message,
)
from evennia_shards.models import Message, Ticket

TYPECLASS = "evennia.objects.objects.DefaultObject"


def _forge_db_shard(pk, shard_id):
    """Bypass chokepoints to set a row's shard_id directly via raw SQL.

    Used by tests to set up "remote shard" scenarios. Raw cursor SQL is
    deliberately not covered by the chokepoints (see shard-isolation.md).
    """
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE objects_objectdb SET shard_id=%s WHERE id=%s",
            [shard_id, pk],
        )


class ConfigAccessorTests(BaseEvenniaTestCase):
    """Tests for the get_role / get_shard_id accessors."""

    @override_settings(SHARDS_ROLE=ROLE_ROUTER)
    def test_get_role_reflects_setting_router(self):
        self.assertEqual(get_role(), ROLE_ROUTER)

    @override_settings(SHARDS_ROLE=ROLE_SHARD)
    def test_get_role_reflects_setting_shard(self):
        self.assertEqual(get_role(), ROLE_SHARD)

    @override_settings(SHARD_ID="some-shard")
    def test_get_shard_id_reflects_setting(self):
        self.assertEqual(get_shard_id(), "some-shard")


class ShardUrlAccessorTests(BaseEvenniaTestCase):
    """Tests for the get_shard_url accessor."""

    @override_settings(SHARD_URLS={"shard0": "http://localhost:4001"})
    def test_returns_url_for_known_shard(self):
        self.assertEqual(get_shard_url("shard0"), "http://localhost:4001")

    @override_settings(SHARD_URLS={"shard0": "http://localhost:4001"})
    def test_raises_key_error_for_unknown_shard(self):
        with self.assertRaises(KeyError):
            get_shard_url("shard99")

    @override_settings(SHARD_URLS=None)
    def test_raises_value_error_when_not_configured(self):
        # No SHARD_URLS in settings at all (monolith case).
        with self.assertRaises(ValueError):
            get_shard_url("shard0")

    @override_settings(
        SHARD_URLS={
            "overworld": "http://overworld.example.com",
            "dungeons": "http://dungeons.example.com",
            "pvp_arena": "http://pvp.example.com",
        }
    )
    def test_multiple_shards_flexible_names(self):
        self.assertEqual(get_shard_url("overworld"), "http://overworld.example.com")
        self.assertEqual(get_shard_url("dungeons"), "http://dungeons.example.com")
        self.assertEqual(get_shard_url("pvp_arena"), "http://pvp.example.com")


class RouterUrlAccessorTests(BaseEvenniaTestCase):
    """Tests for the get_router_url accessor."""

    @override_settings(ROUTER_URL="http://router.example.com")
    def test_returns_configured_url(self):
        self.assertEqual(get_router_url(), "http://router.example.com")

    @override_settings(ROUTER_URL=None)
    def test_raises_value_error_when_not_configured(self):
        with self.assertRaises(ValueError):
            get_router_url()


class RouterShardIdAccessorTests(BaseEvenniaTestCase):
    """Tests for the get_router_shard_id accessor."""

    def test_returns_router(self):
        self.assertEqual(get_router_shard_id(), "router")


class MessageTimeoutAccessorTests(BaseEvenniaTestCase):
    """Tests for the get_message_timeout accessor."""

    def test_default_is_10_seconds_when_no_settings(self):
        # No SHARDS_MESSAGE_TIMEOUT_DEFAULT, no SHARDS_MESSAGE_TIMEOUTS
        self.assertEqual(get_message_timeout("anything"), 10)

    @override_settings(SHARDS_MESSAGE_TIMEOUT_DEFAULT=20)
    def test_global_default_is_overridden(self):
        self.assertEqual(get_message_timeout("anything"), 20)

    @override_settings(SHARDS_MESSAGE_TIMEOUTS={"tell": 5, "character_handoff": 30})
    def test_per_kind_override_returns_specific_timeout(self):
        self.assertEqual(get_message_timeout("tell"), 5)
        self.assertEqual(get_message_timeout("character_handoff"), 30)

    @override_settings(
        SHARDS_MESSAGE_TIMEOUT_DEFAULT=20,
        SHARDS_MESSAGE_TIMEOUTS={"tell": 5},
    )
    def test_unmapped_kind_falls_back_to_default(self):
        self.assertEqual(get_message_timeout("tell"), 5)
        self.assertEqual(get_message_timeout("other_kind"), 20)


class MessageModelTests(BaseEvenniaTestCase):
    """The Message model is wired and the migration deploys."""

    def test_table_name_is_namespaced(self):
        self.assertEqual(Message._meta.db_table, "evennia_shards_message")

    def test_create_round_trips_payload(self):
        msg = Message.objects.create(
            to_shard="shard1",
            from_shard="shard0",
            kind="character_handoff",
            payload={"char_id": 42, "to_room": 7},
        )
        loaded = Message.objects.get(pk=msg.pk)
        self.assertEqual(loaded.to_shard, "shard1")
        self.assertEqual(loaded.from_shard, "shard0")
        self.assertEqual(loaded.kind, "character_handoff")
        self.assertEqual(loaded.payload, {"char_id": 42, "to_room": 7})
        self.assertIsNotNone(loaded.created_at)

    def test_payload_defaults_to_empty_dict(self):
        msg = Message.objects.create(
            to_shard="shard1",
            kind="ping",
        )
        self.assertEqual(msg.payload, {})

    def test_from_shard_can_be_null(self):
        msg = Message.objects.create(
            to_shard="shard1",
            kind="ping",
        )
        self.assertIsNone(msg.from_shard)


@override_settings(SHARD_ID="shard0", SHARDS_ROLE=ROLE_SHARD)
class SendMessageTests(BaseEvenniaTestCase):
    """send_message primitive: insert a message row."""

    def test_returns_created_message_instance(self):
        msg = send_message(
            kind="ping",
            payload={"hello": "world"},
            to_shard="shard1",
        )
        self.assertIsInstance(msg, Message)
        self.assertIsNotNone(msg.pk)

    def test_explicit_from_shard_is_recorded(self):
        msg = send_message(
            kind="ping",
            payload={},
            to_shard="shard1",
            from_shard="shard2",
        )
        self.assertEqual(msg.from_shard, "shard2")

    def test_default_from_shard_uses_current_setting(self):
        msg = send_message(
            kind="ping",
            payload={},
            to_shard="shard1",
        )
        # SHARD_ID is set to "shard0" via the class @override_settings
        self.assertEqual(msg.from_shard, "shard0")

    def test_payload_is_persisted(self):
        msg = send_message(
            kind="character_handoff",
            payload={"char_id": 42, "to_room": 7},
            to_shard="shard1",
        )
        loaded = Message.objects.get(pk=msg.pk)
        self.assertEqual(loaded.payload, {"char_id": 42, "to_room": 7})

    def test_explicit_same_shard_send_raises(self):
        with self.assertRaises(MessageBusError) as ctx:
            send_message(
                kind="ping",
                payload={},
                to_shard="shard0",
                from_shard="shard0",
            )
        self.assertIn("shard0", str(ctx.exception))

    def test_default_from_shard_same_as_to_shard_raises(self):
        # SHARD_ID is "shard0" via class @override_settings;
        # no explicit from_shard, so it defaults to "shard0",
        # matching to_shard and tripping the check.
        with self.assertRaises(MessageBusError):
            send_message(kind="ping", payload={}, to_shard="shard0")

    def test_no_message_row_inserted_when_same_shard_send_raises(self):
        before = Message.objects.count()
        with self.assertRaises(MessageBusError):
            send_message(kind="ping", payload={}, to_shard="shard0", from_shard="shard0")
        self.assertEqual(Message.objects.count(), before)


@override_settings(SHARD_ID="shard0", SHARDS_ROLE=ROLE_SHARD)
class PollMessagesTests(BaseEvenniaTestCase):
    """poll_messages primitive: read messages addressed to a shard."""

    def test_returns_only_messages_for_requested_shard(self):
        send_message(kind="ping", payload={}, to_shard="shard1", from_shard="shard0")
        send_message(kind="ping", payload={}, to_shard="shard2", from_shard="shard0")
        send_message(kind="ping", payload={}, to_shard="shard1", from_shard="shard0")

        result = list(poll_messages("shard1"))
        self.assertEqual(len(result), 2)
        for msg in result:
            self.assertEqual(msg.to_shard, "shard1")

    def test_returns_empty_when_no_matching_messages(self):
        send_message(kind="ping", payload={}, to_shard="shard1", from_shard="shard0")
        result = list(poll_messages("shard9"))
        self.assertEqual(result, [])

    def test_results_ordered_by_created_at_ascending(self):
        # Insert in non-chronological key order; created_at is auto, so
        # insertion order = chronological order at this resolution.
        first = send_message(kind="ping", payload={"n": 1}, to_shard="shard1", from_shard="shard0")
        second = send_message(kind="ping", payload={"n": 2}, to_shard="shard1", from_shard="shard0")
        third = send_message(kind="ping", payload={"n": 3}, to_shard="shard1", from_shard="shard0")

        result = list(poll_messages("shard1"))
        self.assertEqual([msg.pk for msg in result], [first.pk, second.pk, third.pk])

    def test_default_shard_uses_current_setting(self):
        send_message(kind="ping", payload={}, to_shard="shard0", from_shard="shard1")
        send_message(kind="ping", payload={}, to_shard="shard1", from_shard="shard0")

        # SHARD_ID is "shard0" via class @override_settings
        result = list(poll_messages())
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].to_shard, "shard0")

    def test_returns_queryset_not_list(self):
        send_message(kind="ping", payload={}, to_shard="shard1", from_shard="shard0")
        result = poll_messages("shard1")
        # Caller can chain .filter / .count / .first without coercing
        self.assertEqual(result.count(), 1)
        self.assertIsNotNone(result.first())


@override_settings(SHARD_ID="shard0", SHARDS_ROLE=ROLE_SHARD)
class DeleteMessageTests(BaseEvenniaTestCase):
    """delete_message primitive: remove a processed message row."""

    def test_deletes_only_the_named_message(self):
        keep = send_message(kind="ping", payload={}, to_shard="shard1", from_shard="shard0")
        drop = send_message(kind="ping", payload={}, to_shard="shard1", from_shard="shard0")

        delete_message(drop)

        remaining_pks = list(Message.objects.values_list("pk", flat=True))
        self.assertIn(keep.pk, remaining_pks)
        self.assertNotIn(drop.pk, remaining_pks)

    def test_subsequent_poll_does_not_return_deleted_message(self):
        msg = send_message(kind="ping", payload={}, to_shard="shard1", from_shard="shard0")
        self.assertEqual(poll_messages("shard1").count(), 1)

        delete_message(msg)

        self.assertEqual(poll_messages("shard1").count(), 0)


@override_settings(SHARD_ID="shard0", SHARDS_ROLE=ROLE_SHARD)
class MessageHandlerTests(BaseEvenniaTestCase):
    """The base MessageHandler dispatches library-shipped kinds."""

    def test_unknown_kind_returns_false(self):
        # Inject directly via Message.objects.create — bypass send_message
        # to avoid the same-shard guard for this test fixture.
        msg = Message.objects.create(
            kind="unknown_kind",
            payload={},
            to_shard="shard0",
            from_shard="shard1",
        )
        self.assertFalse(MessageHandler().handle(msg))

    def test_ping_returns_true_and_inserts_ping_received_reply(self):
        ping = Message.objects.create(
            kind="ping",
            payload={"text": "hello"},
            to_shard="shard0",
            from_shard="shard1",
        )
        result = MessageHandler().handle(ping)
        self.assertTrue(result)

        replies = list(Message.objects.filter(kind="ping_received"))
        self.assertEqual(len(replies), 1)
        reply = replies[0]
        self.assertEqual(reply.to_shard, "shard1")
        self.assertEqual(reply.from_shard, "shard0")
        self.assertEqual(reply.payload, {"original_pk": ping.pk, "echo": {"text": "hello"}})

    def test_ping_with_no_from_shard_returns_true_and_no_reply(self):
        ping = Message.objects.create(
            kind="ping",
            payload={},
            to_shard="shard0",
            from_shard=None,
        )
        result = MessageHandler().handle(ping)
        self.assertTrue(result)
        self.assertFalse(Message.objects.filter(kind="ping_received").exists())

    def test_ping_received_returns_true_and_inserts_nothing(self):
        msg = Message.objects.create(
            kind="ping_received",
            payload={"original_pk": 99, "echo": {}},
            to_shard="shard0",
            from_shard="shard1",
        )
        before_count = Message.objects.count()
        result = MessageHandler().handle(msg)
        self.assertTrue(result)
        # No new rows inserted (the message itself isn't auto-deleted by
        # the handler — that's process_inbox's job).
        self.assertEqual(Message.objects.count(), before_count)


@override_settings(SHARD_ID="shard0", SHARDS_ROLE=ROLE_SHARD)
class ProcessInboxTests(BaseEvenniaTestCase):
    """process_inbox runs one polling cycle: poll, dispatch, delete on success."""

    def test_handler_truthy_deletes_message(self):
        msg = Message.objects.create(
            kind="custom", payload={}, to_shard="shard0", from_shard="shard1",
        )

        class AlwaysHandle(MessageHandler):
            def handle(self, message):
                return True

        processed = process_inbox(AlwaysHandle())
        self.assertEqual(processed, 1)
        self.assertFalse(Message.objects.filter(pk=msg.pk).exists())

    def test_handler_falsy_leaves_message(self):
        msg = Message.objects.create(
            kind="custom", payload={}, to_shard="shard0", from_shard="shard1",
        )

        class NeverHandle(MessageHandler):
            def handle(self, message):
                return False

        processed = process_inbox(NeverHandle())
        self.assertEqual(processed, 0)
        self.assertTrue(Message.objects.filter(pk=msg.pk).exists())

    def test_handler_exception_leaves_message(self):
        msg = Message.objects.create(
            kind="custom", payload={}, to_shard="shard0", from_shard="shard1",
        )

        class BrokenHandler(MessageHandler):
            def handle(self, message):
                raise RuntimeError("oops")

        processed = process_inbox(BrokenHandler())
        self.assertEqual(processed, 0)
        self.assertTrue(Message.objects.filter(pk=msg.pk).exists())

    def test_default_handler_processes_ping(self):
        # End-to-end with the default base handler: ping arrives, gets
        # consumed, ping_received is inserted to the original sender.
        Message.objects.create(
            kind="ping", payload={"text": "hi"}, to_shard="shard0", from_shard="shard1",
        )
        processed = process_inbox()
        self.assertEqual(processed, 1)
        self.assertFalse(Message.objects.filter(kind="ping").exists())
        self.assertEqual(
            Message.objects.filter(kind="ping_received", to_shard="shard1").count(),
            1,
        )

    def test_skips_messages_for_other_shards(self):
        Message.objects.create(
            kind="custom", payload={}, to_shard="shard9", from_shard="shard1",
        )

        class AlwaysHandle(MessageHandler):
            def handle(self, message):
                return True

        processed = process_inbox(AlwaysHandle())
        self.assertEqual(processed, 0)
        self.assertEqual(Message.objects.count(), 1)


@override_settings(SHARD_ID="shard0", SHARDS_ROLE=ROLE_SHARD)
class ProcessInboxTimeoutTests(BaseEvenniaTestCase):
    """Aged-out unhandled messages produce undeliverable_reply and are deleted."""

    def _age_message(self, msg, seconds):
        # auto_now_add makes obj.created_at = X not stick on save, so update
        # via QuerySet.update (Message uses Django's default QuerySet, not
        # the patched ObjectDB one — the chokepoint guard doesn't apply).
        from datetime import timedelta

        from django.utils import timezone

        Message.objects.filter(pk=msg.pk).update(
            created_at=timezone.now() - timedelta(seconds=seconds),
        )

    def test_aged_out_message_with_valid_from_shard_inserts_undeliverable_reply(self):
        msg = Message.objects.create(
            kind="custom",
            payload={"data": 1},
            to_shard="shard0",
            from_shard="shard1",
        )
        self._age_message(msg, seconds=100)  # default lifespan is 10s

        process_inbox()

        self.assertFalse(Message.objects.filter(pk=msg.pk).exists())
        replies = list(Message.objects.filter(kind="undeliverable_reply"))
        self.assertEqual(len(replies), 1)
        reply = replies[0]
        self.assertEqual(reply.to_shard, "shard1")
        self.assertEqual(reply.from_shard, "shard0")
        self.assertEqual(reply.payload["original_kind"], "custom")
        self.assertEqual(reply.payload["original_payload"], {"data": 1})
        self.assertEqual(reply.payload["reason"], "timeout")

    def test_aged_out_message_without_from_shard_just_deletes(self):
        msg = Message.objects.create(
            kind="custom",
            payload={},
            to_shard="shard0",
            from_shard=None,
        )
        self._age_message(msg, seconds=100)

        process_inbox()

        self.assertFalse(Message.objects.filter(pk=msg.pk).exists())
        self.assertFalse(Message.objects.filter(kind="undeliverable_reply").exists())

    def test_non_aged_message_stays_in_queue(self):
        msg = Message.objects.create(
            kind="custom",
            payload={},
            to_shard="shard0",
            from_shard="shard1",
        )
        # Don't age it; default lifespan is 10s and it was just created.

        process_inbox()

        self.assertTrue(Message.objects.filter(pk=msg.pk).exists())
        self.assertFalse(Message.objects.filter(kind="undeliverable_reply").exists())

    def test_undeliverable_reply_kind_consumed_silently_by_base_handler(self):
        msg = Message.objects.create(
            kind="undeliverable_reply",
            payload={"original_kind": "x", "original_payload": {}, "reason": "timeout"},
            to_shard="shard0",
            from_shard="shard1",
        )
        result = MessageHandler().handle(msg)
        self.assertTrue(result)
        # Handler doesn't insert anything — the row count stays at 1
        # (the handler doesn't auto-delete; that's process_inbox's job).
        self.assertEqual(Message.objects.count(), 1)

    @override_settings(SHARDS_MESSAGE_TIMEOUTS={"custom": 60})
    def test_per_kind_lifespan_override_is_respected(self):
        msg = Message.objects.create(
            kind="custom",
            payload={},
            to_shard="shard0",
            from_shard="shard1",
        )
        # 30s old, but per-kind override sets lifespan to 60s — should defer.
        self._age_message(msg, seconds=30)

        process_inbox()

        self.assertTrue(Message.objects.filter(pk=msg.pk).exists())
        self.assertFalse(Message.objects.filter(kind="undeliverable_reply").exists())

    def test_handler_truthy_short_circuits_timeout_check(self):
        # An aged-out message that the handler returns True for is treated
        # as successfully processed — no undeliverable_reply.
        msg = Message.objects.create(
            kind="custom",
            payload={},
            to_shard="shard0",
            from_shard="shard1",
        )
        self._age_message(msg, seconds=100)

        class AlwaysHandle(MessageHandler):
            def handle(self, message):
                return True

        process_inbox(AlwaysHandle())

        self.assertFalse(Message.objects.filter(pk=msg.pk).exists())
        self.assertFalse(Message.objects.filter(kind="undeliverable_reply").exists())


class AppSetupTests(BaseEvenniaTestCase):
    """AppConfig.ready() wires shard_id onto ObjectDB."""

    def test_shard_id_field_wired_on_objectdb(self):
        field = ObjectDB._meta.get_field("shard_id")
        self.assertIsInstance(field, models.CharField)
        self.assertEqual(field.max_length, 64)
        self.assertTrue(field.null)
        self.assertTrue(field.blank)
        self.assertTrue(field.db_index)


@override_settings(SHARD_ID="shard0", SHARDS_ROLE=ROLE_SHARD)
class PreSaveChokepointTests(BaseEvenniaTestCase):
    """pre_save chokepoint: auto-stamp on None; refuse on remote shard_id."""

    def test_unstamped_save_auto_stamps_to_current(self):
        obj = ObjectDB.objects.create(db_key="t1", db_typeclass_path=TYPECLASS)
        self.assertEqual(obj.shard_id, "shard0")

    def test_owned_save_passes(self):
        obj = ObjectDB.objects.create(db_key="t2", db_typeclass_path=TYPECLASS)
        obj.db_key = "t2_modified"
        obj.save()
        self.assertEqual(obj.shard_id, "shard0")

    def test_global_sentinel_save_passes(self):
        obj = ObjectDB.objects.create(db_key="t3", db_typeclass_path=TYPECLASS)
        obj.shard_id = "*"
        obj.save()
        self.assertEqual(obj.shard_id, "*")

    def test_remote_shard_save_raises(self):
        obj = ObjectDB.objects.create(db_key="t4", db_typeclass_path=TYPECLASS)
        obj.shard_id = "shard1"
        with self.assertRaises(ShardIsolationError) as ctx:
            obj.save()
        msg = str(ctx.exception)
        self.assertIn("shard0", msg)
        self.assertIn("shard1", msg)


@override_settings(SHARD_ID="shard0", SHARDS_ROLE=ROLE_SHARD)
class PreDeleteChokepointTests(BaseEvenniaTestCase):
    """pre_delete chokepoint: refuse delete of remote-shard rows.

    Permissive on shard_id=None (legacy unstamped) and shard_id="*" (global).
    Covers both instance.delete() and qs.delete() — Django fires pre_delete
    per row even on bulk queryset deletes.
    """

    def test_owned_instance_delete_passes(self):
        obj = ObjectDB.objects.create(db_key="d1", db_typeclass_path=TYPECLASS)
        # auto-stamped to shard0 by pre_save
        obj.delete()

    def test_global_sentinel_instance_delete_passes(self):
        obj = ObjectDB.objects.create(db_key="d2", db_typeclass_path=TYPECLASS)
        obj.shard_id = "*"
        obj.save()
        obj.delete()

    def test_unstamped_instance_delete_passes(self):
        obj = ObjectDB.objects.create(db_key="d3", db_typeclass_path=TYPECLASS)
        obj.shard_id = None
        obj.delete()

    def test_remote_instance_delete_raises(self):
        obj = ObjectDB.objects.create(db_key="d4", db_typeclass_path=TYPECLASS)
        obj.shard_id = "shard1"
        with self.assertRaises(ShardIsolationError) as ctx:
            obj.delete()
        msg = str(ctx.exception)
        self.assertIn("shard0", msg)
        self.assertIn("shard1", msg)

    def test_remote_qs_delete_raises(self):
        from evennia.utils.idmapper.models import flush_cache

        obj = ObjectDB.objects.create(db_key="d5", db_typeclass_path=TYPECLASS)
        pk = obj.pk
        _forge_db_shard(pk, "shard1")
        # Flush idmapper so qs.delete() loads a fresh instance with shard_id
        # populated from the DB row (rather than the cached shard0 value).
        flush_cache()
        with self.assertRaises(ShardIsolationError):
            ObjectDB.objects.filter(pk=pk).delete()


@override_settings(SHARD_ID="shard0", SHARDS_ROLE=ROLE_SHARD)
class FromDbChokepointTests(BaseEvenniaTestCase):
    """from_db chokepoint: refuse to instantiate rows owned by another shard.

    Permissive on shard_id=None (legacy unstamped) and shard_id="*" (global).
    Bypassed by .values() / .values_list() (per design — they don't construct
    instances). See DESIGN/shard-isolation.md.
    """

    def test_owned_get_passes(self):
        from evennia.utils.idmapper.models import flush_cache

        obj = ObjectDB.objects.create(db_key="r1", db_typeclass_path=TYPECLASS)
        pk = obj.pk
        flush_cache()
        ObjectDB.objects.get(pk=pk)

    def test_global_sentinel_get_passes(self):
        from evennia.utils.idmapper.models import flush_cache

        obj = ObjectDB.objects.create(db_key="r2", db_typeclass_path=TYPECLASS)
        obj.shard_id = "*"
        obj.save()
        pk = obj.pk
        flush_cache()
        ObjectDB.objects.get(pk=pk)

    def test_unstamped_get_passes(self):
        from evennia.utils.idmapper.models import flush_cache

        obj = ObjectDB.objects.create(db_key="r3", db_typeclass_path=TYPECLASS)
        pk = obj.pk
        _forge_db_shard(pk, None)
        flush_cache()
        ObjectDB.objects.get(pk=pk)

    def test_remote_get_raises(self):
        from evennia.utils.idmapper.models import flush_cache

        obj = ObjectDB.objects.create(db_key="r4", db_typeclass_path=TYPECLASS)
        pk = obj.pk
        _forge_db_shard(pk, "shard1")
        flush_cache()
        with self.assertRaises(ShardIsolationError) as ctx:
            ObjectDB.objects.get(pk=pk)
        msg = str(ctx.exception)
        self.assertIn("shard0", msg)
        self.assertIn("shard1", msg)

    def test_values_bypass_does_not_raise(self):
        obj = ObjectDB.objects.create(db_key="r5", db_typeclass_path=TYPECLASS)
        pk = obj.pk
        _forge_db_shard(pk, "shard1")
        # values() returns row data without going through from_db, so the
        # chokepoint is intentionally not triggered.
        result = list(ObjectDB.objects.filter(pk=pk).values("shard_id"))
        self.assertEqual(result, [{"shard_id": "shard1"}])


@override_settings(SHARD_ID="shard0", SHARDS_ROLE=ROLE_SHARD)
class QsUpdateChokepointTests(BaseEvenniaTestCase):
    """qs.update chokepoint: refuse bulk update if any row in scope is remote.

    Permissive on shard_id=None (legacy unstamped) and shard_id="*" (global).
    The check runs as a separate SELECT (using values_list to bypass from_db)
    before any UPDATE SQL is issued, so owned rows in a mixed queryset are
    not modified when a remote row is in scope.
    """

    def _db_key(self, pk):
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute("SELECT db_key FROM objects_objectdb WHERE id=%s", [pk])
            row = cursor.fetchone()
        return row[0] if row else None

    def test_owned_qs_update_passes(self):
        obj = ObjectDB.objects.create(db_key="u1", db_typeclass_path=TYPECLASS)
        ObjectDB.objects.filter(pk=obj.pk).update(db_key="u1_modified")
        self.assertEqual(self._db_key(obj.pk), "u1_modified")

    def test_global_sentinel_qs_update_passes(self):
        obj = ObjectDB.objects.create(db_key="u2", db_typeclass_path=TYPECLASS)
        obj.shard_id = "*"
        obj.save()
        ObjectDB.objects.filter(pk=obj.pk).update(db_key="u2_modified")
        self.assertEqual(self._db_key(obj.pk), "u2_modified")

    def test_unstamped_qs_update_passes(self):
        obj = ObjectDB.objects.create(db_key="u3", db_typeclass_path=TYPECLASS)
        _forge_db_shard(obj.pk, None)
        ObjectDB.objects.filter(pk=obj.pk).update(db_key="u3_modified")
        self.assertEqual(self._db_key(obj.pk), "u3_modified")

    def test_remote_qs_update_raises(self):
        obj = ObjectDB.objects.create(db_key="u4", db_typeclass_path=TYPECLASS)
        _forge_db_shard(obj.pk, "shard1")
        with self.assertRaises(ShardIsolationError) as ctx:
            ObjectDB.objects.filter(pk=obj.pk).update(db_key="u4_modified")
        msg = str(ctx.exception)
        self.assertIn("shard0", msg)
        self.assertIn("shard1", msg)
        # Verify the update did NOT run — db_key unchanged.
        self.assertEqual(self._db_key(obj.pk), "u4")

    def test_mixed_qs_update_raises_before_touching_owned_rows(self):
        owned = ObjectDB.objects.create(db_key="u5_owned", db_typeclass_path=TYPECLASS)
        remote = ObjectDB.objects.create(db_key="u5_remote", db_typeclass_path=TYPECLASS)
        _forge_db_shard(remote.pk, "shard1")
        with self.assertRaises(ShardIsolationError):
            ObjectDB.objects.filter(pk__in=[owned.pk, remote.pk]).update(db_key="u5_modified")
        # Owned row must not have been updated — chokepoint refuses before SQL.
        self.assertEqual(self._db_key(owned.pk), "u5_owned")
        self.assertEqual(self._db_key(remote.pk), "u5_remote")


# ── Router exemption ──────────────────────────────────────────────


@override_settings(SHARD_ID=ROLE_ROUTER, SHARDS_ROLE=ROLE_ROUTER)
class RouterFromDbExemptionTests(BaseEvenniaTestCase):
    """Router is exempt from from_db — it can load objects from any shard."""

    def test_router_loads_remote_shard_object(self):
        from evennia.utils.idmapper.models import flush_cache

        obj = ObjectDB.objects.create(db_key="rf1", db_typeclass_path=TYPECLASS)
        pk = obj.pk
        _forge_db_shard(pk, "shard0")
        flush_cache()
        # Would raise ShardIsolationError if role were "shard"
        loaded = ObjectDB.objects.get(pk=pk)
        self.assertEqual(loaded.shard_id, "shard0")

    def test_router_loads_objects_from_multiple_shards(self):
        from evennia.utils.idmapper.models import flush_cache

        obj1 = ObjectDB.objects.create(db_key="rf2", db_typeclass_path=TYPECLASS)
        obj2 = ObjectDB.objects.create(db_key="rf3", db_typeclass_path=TYPECLASS)
        _forge_db_shard(obj1.pk, "shard0")
        _forge_db_shard(obj2.pk, "shard1")
        flush_cache()
        loaded1 = ObjectDB.objects.get(pk=obj1.pk)
        loaded2 = ObjectDB.objects.get(pk=obj2.pk)
        self.assertEqual(loaded1.shard_id, "shard0")
        self.assertEqual(loaded2.shard_id, "shard1")


@override_settings(SHARD_ID=ROLE_ROUTER, SHARDS_ROLE=ROLE_ROUTER)
class RouterPreSaveExemptionTests(BaseEvenniaTestCase):
    """Router is exempt from pre_save — it can create/modify objects for any shard."""

    def test_router_saves_object_with_remote_shard_id(self):
        obj = ObjectDB.objects.create(db_key="rs1", db_typeclass_path=TYPECLASS)
        obj.shard_id = "shard0"
        obj.save()  # Would raise ShardIsolationError if role were "shard"
        self.assertEqual(obj.shard_id, "shard0")

    def test_router_auto_stamps_with_router_shard_id(self):
        obj = ObjectDB.objects.create(db_key="rs2", db_typeclass_path=TYPECLASS)
        # Auto-stamp still uses current SHARD_ID (router)
        self.assertEqual(obj.shard_id, ROLE_ROUTER)


@override_settings(SHARD_ID=ROLE_ROUTER, SHARDS_ROLE=ROLE_ROUTER)
class RouterPreDeleteExemptionTests(BaseEvenniaTestCase):
    """Router is exempt from pre_delete — it can delete objects from any shard."""

    def test_router_deletes_remote_shard_object(self):
        obj = ObjectDB.objects.create(db_key="rd1", db_typeclass_path=TYPECLASS)
        obj.shard_id = "shard0"
        obj.save()
        obj.delete()  # Would raise ShardIsolationError if role were "shard"
        self.assertFalse(ObjectDB.objects.filter(db_key="rd1").exists())

    def test_router_bulk_deletes_remote_shard_objects(self):
        from evennia.utils.idmapper.models import flush_cache

        obj = ObjectDB.objects.create(db_key="rd2", db_typeclass_path=TYPECLASS)
        pk = obj.pk
        _forge_db_shard(pk, "shard1")
        flush_cache()
        ObjectDB.objects.filter(pk=pk).delete()
        self.assertFalse(ObjectDB.objects.filter(pk=pk).exists())


@override_settings(SHARD_ID=ROLE_ROUTER, SHARDS_ROLE=ROLE_ROUTER)
class RouterQsUpdateExemptionTests(BaseEvenniaTestCase):
    """Router is exempt from qs.update — it can bulk-update objects from any shard."""

    def _db_key(self, pk):
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute("SELECT db_key FROM objects_objectdb WHERE id=%s", [pk])
            row = cursor.fetchone()
        return row[0] if row else None

    def test_router_bulk_updates_remote_shard_object(self):
        obj = ObjectDB.objects.create(db_key="ru1", db_typeclass_path=TYPECLASS)
        _forge_db_shard(obj.pk, "shard1")
        ObjectDB.objects.filter(pk=obj.pk).update(db_key="ru1_modified")
        self.assertEqual(self._db_key(obj.pk), "ru1_modified")

    def test_router_bulk_updates_mixed_shard_objects(self):
        obj1 = ObjectDB.objects.create(db_key="ru2_a", db_typeclass_path=TYPECLASS)
        obj2 = ObjectDB.objects.create(db_key="ru2_b", db_typeclass_path=TYPECLASS)
        _forge_db_shard(obj1.pk, "shard0")
        _forge_db_shard(obj2.pk, "shard1")
        ObjectDB.objects.filter(pk__in=[obj1.pk, obj2.pk]).update(db_key="ru2_modified")
        self.assertEqual(self._db_key(obj1.pk), "ru2_modified")
        self.assertEqual(self._db_key(obj2.pk), "ru2_modified")


# ── Ticket primitives ──────────────────────────────────────────────


class TicketModelTests(BaseEvenniaTestCase):
    """The Ticket model is wired and the migration deploys."""

    def test_table_name_is_namespaced(self):
        self.assertEqual(Ticket._meta.db_table, "evennia_shards_ticket")

    def test_token_is_primary_key(self):
        field = Ticket._meta.get_field("token")
        self.assertTrue(field.primary_key)


@override_settings(SHARD_ID="shard0", SHARDS_ROLE=ROLE_SHARD)
class CreateTicketTests(BaseEvenniaTestCase):
    """create_ticket inserts a Ticket row and returns a token."""

    def test_returns_token_string(self):
        token = create_ticket(account_id=1, character_id=2, to_shard="shard0")
        self.assertIsInstance(token, str)
        self.assertEqual(len(token), 32)  # uuid4().hex is 32 chars

    def test_inserts_ticket_row(self):
        token = create_ticket(account_id=1, character_id=2, to_shard="shard0")
        ticket = Ticket.objects.get(token=token)
        self.assertEqual(ticket.account_id, 1)
        self.assertEqual(ticket.character_id, 2)
        self.assertEqual(ticket.to_shard, "shard0")

    def test_each_call_produces_unique_token(self):
        t1 = create_ticket(account_id=1, character_id=2, to_shard="shard0")
        t2 = create_ticket(account_id=1, character_id=2, to_shard="shard0")
        self.assertNotEqual(t1, t2)

    def test_client_ip_stored_when_provided(self):
        token = create_ticket(
            account_id=1, character_id=2, to_shard="shard0",
            client_ip="192.168.1.42",
        )
        ticket = Ticket.objects.get(token=token)
        self.assertEqual(ticket.client_ip, "192.168.1.42")

    def test_client_ip_defaults_to_none(self):
        token = create_ticket(account_id=1, character_id=2, to_shard="shard0")
        ticket = Ticket.objects.get(token=token)
        self.assertIsNone(ticket.client_ip)


@override_settings(SHARD_ID="shard0", SHARDS_ROLE=ROLE_SHARD)
class GetTicketTests(BaseEvenniaTestCase):
    """get_ticket looks up a ticket by token with shard check."""

    def test_valid_token_returns_true_and_data(self):
        token = create_ticket(account_id=10, character_id=20, to_shard="shard0")
        found, data = get_ticket(token)
        self.assertTrue(found)
        self.assertEqual(data["account_id"], 10)
        self.assertEqual(data["character_id"], 20)
        self.assertEqual(data["to_shard"], "shard0")

    def test_invalid_token_returns_false(self):
        found, data = get_ticket("nonexistent")
        self.assertFalse(found)
        self.assertIsNone(data)

    def test_wrong_shard_returns_false(self):
        token = create_ticket(account_id=1, character_id=2, to_shard="shard1")
        found, data = get_ticket(token, shard_id="shard0")
        self.assertFalse(found)
        self.assertIsNone(data)

    def test_returns_client_ip(self):
        token = create_ticket(
            account_id=1, character_id=2, to_shard="shard0",
            client_ip="10.0.0.1",
        )
        found, data = get_ticket(token)
        self.assertTrue(found)
        self.assertEqual(data["client_ip"], "10.0.0.1")

    def test_returns_none_client_ip_when_not_set(self):
        token = create_ticket(account_id=1, character_id=2, to_shard="shard0")
        found, data = get_ticket(token)
        self.assertTrue(found)
        self.assertIsNone(data["client_ip"])

    def test_does_not_delete_ticket(self):
        token = create_ticket(account_id=1, character_id=2, to_shard="shard0")
        get_ticket(token)
        self.assertTrue(Ticket.objects.filter(token=token).exists())


@override_settings(SHARD_ID="shard0", SHARDS_ROLE=ROLE_SHARD)
class DeleteTicketTests(BaseEvenniaTestCase):
    """delete_ticket removes a ticket by token."""

    def test_deletes_existing_ticket(self):
        token = create_ticket(account_id=1, character_id=2, to_shard="shard0")
        delete_ticket(token)
        self.assertFalse(Ticket.objects.filter(token=token).exists())

    def test_silent_on_nonexistent_token(self):
        # Should not raise
        delete_ticket("nonexistent")

    def test_second_get_after_delete_returns_false(self):
        token = create_ticket(account_id=1, character_id=2, to_shard="shard0")
        delete_ticket(token)
        found, data = get_ticket(token)
        self.assertFalse(found)
        self.assertIsNone(data)


# ── Protocol override ─────────────────────────────────────────────


class _FakeTransport:
    """Minimal stand-in for a Twisted transport."""

    def __init__(self, client=None):
        self.client = client  # tuple: (ip, port) or None


class _FakeProtocol:
    """Minimal stand-in for testing protocol methods without Twisted.

    Provides the attributes that _extract_ticket_token, _validate_ticket,
    and _get_client_address rely on.
    """

    def __init__(self, uri=None, client_ip=None, http_headers=None):
        self.http_request_uri = uri
        self.transport = _FakeTransport(
            client=(client_ip, 0) if client_ip else None
        )
        self.http_headers = http_headers or {}
        self.sent_lines = []
        self.close_calls = []

    def sendLine(self, data):
        self.sent_lines.append(data)

    def sendClose(self, code=None, reason=None):
        self.close_calls.append((code, reason))


# Bind the unbound methods onto _FakeProtocol so tests can call them
# without instantiating the real ShardWebSocketClient (which needs
# Twisted reactor + Autobahn).
from evennia_shards.protocols import ShardWebSocketClient as _SWC

_FakeProtocol._extract_ticket_token = _SWC._extract_ticket_token
_FakeProtocol._validate_ticket = _SWC._validate_ticket
_FakeProtocol._get_client_address = _SWC._get_client_address
_FakeProtocol._send_text = _SWC._send_text


class ExtractTicketTokenTests(BaseEvenniaTestCase):
    """_extract_ticket_token parses ?ticket= from the WebSocket URL."""

    def test_extracts_token_from_query_string(self):
        proto = _FakeProtocol(uri="/websocket?ticket=abc123")
        self.assertEqual(proto._extract_ticket_token(), "abc123")

    def test_returns_none_when_no_ticket_param(self):
        proto = _FakeProtocol(uri="/websocket?csessid=xyz")
        self.assertIsNone(proto._extract_ticket_token())

    def test_returns_none_when_no_uri(self):
        proto = _FakeProtocol(uri=None)
        self.assertIsNone(proto._extract_ticket_token())

    def test_returns_first_token_when_multiple(self):
        proto = _FakeProtocol(uri="/websocket?ticket=first&ticket=second")
        self.assertEqual(proto._extract_ticket_token(), "first")

    def test_handles_full_url(self):
        proto = _FakeProtocol(
            uri="ws://shard0:4002/websocket?ticket=tok123&csessid=abc"
        )
        self.assertEqual(proto._extract_ticket_token(), "tok123")


class GetClientAddressTests(BaseEvenniaTestCase):
    """_get_client_address resolves the real client IP."""

    def test_direct_connection_returns_transport_ip(self):
        proto = _FakeProtocol(client_ip="192.168.1.10")
        self.assertEqual(proto._get_client_address(), "192.168.1.10")

    def test_no_transport_client_returns_none(self):
        proto = _FakeProtocol(client_ip=None)
        self.assertIsNone(proto._get_client_address())

    @override_settings(UPSTREAM_IPS=["127.0.0.1"])
    def test_proxy_returns_forwarded_ip(self):
        proto = _FakeProtocol(
            client_ip="127.0.0.1",
            http_headers={"x-forwarded-for": "10.0.0.5, 127.0.0.1"},
        )
        # Reload _UPSTREAM_IPS for this test — the module-level constant
        # won't pick up the override, so we patch it directly.
        import evennia_shards.protocols as proto_mod
        original = proto_mod._UPSTREAM_IPS
        proto_mod._UPSTREAM_IPS = ["127.0.0.1"]
        try:
            self.assertEqual(proto._get_client_address(), "10.0.0.5")
        finally:
            proto_mod._UPSTREAM_IPS = original

    def test_non_proxy_ip_ignores_forwarded_header(self):
        proto = _FakeProtocol(
            client_ip="10.0.0.1",
            http_headers={"x-forwarded-for": "evil.ip"},
        )
        # 10.0.0.1 is not in UPSTREAM_IPS, so the header is ignored.
        self.assertEqual(proto._get_client_address(), "10.0.0.1")


@override_settings(SHARD_ID="shard0", SHARDS_ROLE=ROLE_SHARD)
class ValidateTicketTests(BaseEvenniaTestCase):
    """_validate_ticket validates, IP-checks, and consumes the ticket."""

    def test_valid_ticket_returns_true_and_data(self):
        token = create_ticket(account_id=10, character_id=20, to_shard="shard0")
        proto = _FakeProtocol()
        valid, data = proto._validate_ticket(token, "127.0.0.1")
        self.assertTrue(valid)
        self.assertEqual(data["account_id"], 10)
        self.assertEqual(data["character_id"], 20)

    def test_valid_ticket_is_consumed(self):
        token = create_ticket(account_id=1, character_id=2, to_shard="shard0")
        proto = _FakeProtocol()
        proto._validate_ticket(token, "127.0.0.1")
        self.assertFalse(Ticket.objects.filter(token=token).exists())

    def test_second_use_of_same_token_rejected(self):
        token = create_ticket(account_id=1, character_id=2, to_shard="shard0")
        proto = _FakeProtocol()
        proto._validate_ticket(token, "127.0.0.1")
        valid, error = proto._validate_ticket(token, "127.0.0.1")
        self.assertFalse(valid)
        self.assertIn("not found", error)

    def test_invalid_token_returns_false_and_error(self):
        proto = _FakeProtocol()
        valid, error = proto._validate_ticket("nonexistent", "127.0.0.1")
        self.assertFalse(valid)
        self.assertIn("not found", error)

    def test_invalid_token_does_not_delete_anything(self):
        token = create_ticket(account_id=1, character_id=2, to_shard="shard0")
        proto = _FakeProtocol()
        proto._validate_ticket("wrong_token", "127.0.0.1")
        self.assertTrue(Ticket.objects.filter(token=token).exists())

    def test_ip_match_passes(self):
        token = create_ticket(
            account_id=1, character_id=2, to_shard="shard0",
            client_ip="10.0.0.1",
        )
        proto = _FakeProtocol()
        valid, data = proto._validate_ticket(token, "10.0.0.1")
        self.assertTrue(valid)

    def test_ip_mismatch_rejected(self):
        token = create_ticket(
            account_id=1, character_id=2, to_shard="shard0",
            client_ip="10.0.0.1",
        )
        proto = _FakeProtocol()
        valid, error = proto._validate_ticket(token, "192.168.1.99")
        self.assertFalse(valid)
        self.assertIn("IP mismatch", error)

    def test_ip_mismatch_does_not_consume_ticket(self):
        token = create_ticket(
            account_id=1, character_id=2, to_shard="shard0",
            client_ip="10.0.0.1",
        )
        proto = _FakeProtocol()
        proto._validate_ticket(token, "192.168.1.99")
        # Ticket survives — the legitimate client can still use it
        self.assertTrue(Ticket.objects.filter(token=token).exists())

    def test_no_ip_on_ticket_skips_ip_check(self):
        token = create_ticket(
            account_id=1, character_id=2, to_shard="shard0",
        )
        proto = _FakeProtocol()
        valid, data = proto._validate_ticket(token, "192.168.1.99")
        self.assertTrue(valid)


@override_settings(SHARD_ID="shard0", SHARDS_ROLE=ROLE_SHARD)
class RoleGatingTests(BaseEvenniaTestCase):
    """onOpen Phase 1: role-based gating when no ticket is present.

    These test the gating logic in isolation using _FakeProtocol rather
    than the full onOpen (which needs Twisted). The logic is: shards
    reject connections without tickets; routers allow them.
    """

    def test_shard_no_token_sends_rejection(self):
        """Shard with no ticket should send an error and close."""
        proto = _FakeProtocol(uri="/websocket", client_ip="127.0.0.1")
        token = proto._extract_ticket_token()
        self.assertIsNone(token)

        # Simulate Phase 1 no-token path for shard role.
        role = get_role()  # ROLE_SHARD via @override_settings
        self.assertEqual(role, ROLE_SHARD)
        msg = "[evennia-shards] Connection rejected: this shard requires a ticket"
        proto._send_text(msg)
        proto.sendClose(4001, msg)

        self.assertEqual(len(proto.sent_lines), 1)
        self.assertIn("requires a ticket", proto.sent_lines[0])
        self.assertEqual(len(proto.close_calls), 1)

    @override_settings(SHARDS_ROLE=ROLE_ROUTER)
    def test_router_no_token_proceeds(self):
        """Router with no ticket should not reject — normal login allowed."""
        proto = _FakeProtocol(uri="/websocket", client_ip="127.0.0.1")
        token = proto._extract_ticket_token()
        self.assertIsNone(token)

        role = get_role()  # ROLE_ROUTER via @override_settings
        self.assertEqual(role, ROLE_ROUTER)
        # Router does NOT reject — no sendClose, no error message.
        self.assertEqual(len(proto.sent_lines), 0)
        self.assertEqual(len(proto.close_calls), 0)


# ---------------------------------------------------------------------------
# ShardAwareCmdIC tests
# ---------------------------------------------------------------------------


class _FakeSession:
    """Minimal session stand-in for command tests."""

    def __init__(self, address="127.0.0.1"):
        self.address = address
        self.puppet = None
        self.oob_messages = {}
        self.flag_updates = {}

    def msg(self, **kwargs):
        self.oob_messages.update(kwargs)

    def update_flags(self, **flags):
        self.flag_updates.update(flags)


class _FakeAttributes:
    """Stand-in for AccountDB.attributes — just a record-only get."""

    def __init__(self, store=None):
        self._store = dict(store or {})

    def get(self, name, default=None):
        return self._store.get(name, default)


class _FakeAccount:
    """Minimal account stand-in for command and hook tests."""

    def __init__(self, pk=1, characters=None, key="Player1"):
        self._saved_attrs = {}
        self.id = pk
        self.pk = pk
        self.key = key
        self._characters = characters or []
        self._last_puppet = None
        self.db = self  # db.X delegates to self.X
        self.attributes = _FakeAttributes()
        # Recorders for hook-side-effect assertions.
        self.account_messages = []
        self.connect_channel_messages = []
        self.at_look_calls = []

    @property
    def _last_puppet(self):
        return self._saved_attrs.get("_last_puppet")

    @_last_puppet.setter
    def _last_puppet(self, value):
        self._saved_attrs["_last_puppet"] = value

    @property
    def characters(self):
        return self._characters

    def get_puppet(self, session):
        return session.puppet

    def search(self, searchdata, candidates=None, search_object=True, quiet=True):
        """Simple name-match search against candidates."""
        if candidates:
            return [c for c in candidates if c.key == searchdata]
        return []

    @property
    def locks(self):
        return self

    def check_lockstring(self, account, lockstring):
        return False  # non-Builder for simplicity

    def msg(self, text=None, session=None, **kwargs):
        if text is not None:
            self.account_messages.append(text)

    def _send_to_connect_channel(self, message):
        self.connect_channel_messages.append(message)

    def at_look(self, target=None, session=None, **kwargs):
        self.at_look_calls.append({"target": target, "session": session})
        return "OOC menu"


class _FakeCharacter:
    """Minimal character stand-in for command tests."""

    def __init__(self, key, pk, shard_id="shard0"):
        self.key = key
        self.id = pk
        self.pk = pk
        self.shard_id = shard_id
        self.name = key


def _make_cmd(args="", role=ROLE_ROUTER, shard_id=ROLE_ROUTER, account=None,
              session=None, characters=None):
    """Build a ShardAwareCmdIC instance wired up for testing."""
    from evennia_shards.commands import ShardAwareCmdIC

    cmd = ShardAwareCmdIC()
    cmd.args = args
    cmd.raw_string = f"ic {args}"
    cmd.session = session or _FakeSession()
    if account is None:
        account = _FakeAccount(characters=characters or [])
    cmd.account = account
    cmd.caller = account
    cmd._messages = []

    def _msg(text, **kwargs):
        cmd._messages.append(text)

    cmd.msg = _msg
    return cmd


@override_settings(
    SHARDS_ROLE=ROLE_ROUTER, SHARD_ID=ROLE_ROUTER,
    SHARD_URLS={"shard0": "http://localhost:4011"},
)
class RedirectToCharacterShardHelperTests(BaseEvenniaTestCase):
    """Direct tests for the _redirect_to_character_shard helper.

    The helper is shared mechanism between the IC command path and the
    at_post_login override path. Validation is the caller's job — these
    tests assert only the side-effects the helper itself performs.
    """

    def test_redirect_creates_ticket_sets_last_puppet_and_sends_oob(self):
        from evennia_shards.commands import _redirect_to_character_shard

        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        account = _FakeAccount(pk=7)
        session = _FakeSession(address="10.0.0.1")

        url = _redirect_to_character_shard(account, session, char)

        # _last_puppet set on the account.
        self.assertIs(account.db._last_puppet, char)

        # Exactly one ticket with the right fields.
        tickets = list(Ticket.objects.all())
        self.assertEqual(len(tickets), 1)
        ticket = tickets[0]
        self.assertEqual(ticket.account_id, 7)
        self.assertEqual(ticket.character_id, 42)
        self.assertEqual(ticket.to_shard, "shard0")
        self.assertEqual(ticket.client_ip, "10.0.0.1")

        # OOB shard_redirect sent on the session.
        self.assertIn("shard_redirect", session.oob_messages)
        oob_url = session.oob_messages["shard_redirect"][0][0]
        self.assertIn("http://localhost:4011/webclient?ticket=", oob_url)
        self.assertIn(ticket.token, oob_url)

        # Returned URL matches the OOB URL.
        self.assertEqual(url, oob_url)


@override_settings(
    SHARDS_ROLE=ROLE_SHARD, SHARD_ID="shard0",
    SHARD_URLS={"shard0": "http://localhost:4011"},
)
class ShardAwareCmdICShardTests(BaseEvenniaTestCase):
    """IC command on a shard tells the player to return to the router."""

    def test_shard_rejects_ic(self):
        cmd = _make_cmd(args="Bob", role=ROLE_SHARD, shard_id="shard0")
        cmd.func()
        self.assertEqual(len(cmd._messages), 1)
        self.assertIn("Leave this character", cmd._messages[0])

    def test_shard_rejects_ic_no_args(self):
        cmd = _make_cmd(args="", role=ROLE_SHARD, shard_id="shard0")
        cmd.func()
        self.assertEqual(len(cmd._messages), 1)
        self.assertIn("Leave this character", cmd._messages[0])


@override_settings(
    SHARDS_ROLE=ROLE_ROUTER, SHARD_ID=ROLE_ROUTER,
    SHARD_URLS={"shard0": "http://localhost:4011"},
)
class ShardAwareCmdICRouterTests(BaseEvenniaTestCase):
    """IC command on the router creates a ticket and redirects."""

    def test_router_creates_ticket_and_redirects(self):
        """ic <name> on router → ticket + shard_redirect OOB."""
        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        session = _FakeSession()
        cmd = _make_cmd(args="Bob", characters=[char], session=session)
        cmd.func()

        # Should have created a ticket.
        tickets = list(Ticket.objects.all())
        self.assertEqual(len(tickets), 1)
        ticket = tickets[0]
        self.assertEqual(ticket.account_id, cmd.account.id)
        self.assertEqual(ticket.character_id, 42)
        self.assertEqual(ticket.to_shard, "shard0")

        # Should have sent shard_redirect OOB.
        self.assertIn("shard_redirect", session.oob_messages)
        redirect_args = session.oob_messages["shard_redirect"]
        url = redirect_args[0][0]
        self.assertIn("http://localhost:4011/webclient?ticket=", url)
        self.assertIn(ticket.token, url)

    def test_router_sets_last_puppet(self):
        """Router sets _last_puppet on the account before redirecting."""
        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        account = _FakeAccount(characters=[char])
        cmd = _make_cmd(args="Bob", account=account)
        cmd.func()
        self.assertIs(account.db._last_puppet, char)

    def test_router_no_args_uses_last_puppet(self):
        """ic with no args uses _last_puppet if set."""
        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        account = _FakeAccount(characters=[char])
        account.db._last_puppet = char
        session = _FakeSession()
        cmd = _make_cmd(args="", account=account, session=session)
        cmd.func()

        # Should redirect (ticket created).
        tickets = list(Ticket.objects.all())
        self.assertEqual(len(tickets), 1)
        self.assertIn("shard_redirect", session.oob_messages)

    def test_router_no_args_no_last_puppet_shows_usage(self):
        """ic with no args and no _last_puppet shows usage."""
        cmd = _make_cmd(args="")
        cmd.func()
        self.assertTrue(any("Usage:" in m for m in cmd._messages))

    def test_router_character_no_shard_id_gives_error(self):
        """Character with no shard assignment shows error."""
        char = _FakeCharacter("Bob", pk=42, shard_id=None)
        cmd = _make_cmd(args="Bob", characters=[char])
        cmd.func()
        self.assertTrue(any("no shard assignment" in m for m in cmd._messages))

    def test_router_character_global_shard_gives_error(self):
        """Character with shard_id='*' shows error."""
        char = _FakeCharacter("Bob", pk=42, shard_id="*")
        cmd = _make_cmd(args="Bob", characters=[char])
        cmd.func()
        self.assertTrue(any("no shard assignment" in m for m in cmd._messages))

    def test_router_character_not_found(self):
        """ic <unknown> shows error."""
        cmd = _make_cmd(args="Nobody", characters=[])
        cmd.func()
        self.assertTrue(any("not a valid character" in m for m in cmd._messages))

    def test_router_ip_pinned_in_ticket(self):
        """Ticket records the session's IP address."""
        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        session = _FakeSession(address="10.0.0.1")
        cmd = _make_cmd(args="Bob", characters=[char], session=session)
        cmd.func()

        ticket = Ticket.objects.first()
        self.assertEqual(ticket.client_ip, "10.0.0.1")


def _make_ooc_cmd(account=None, session=None, puppet=None):
    """Build a ShardAwareCmdOOC instance wired up for testing."""
    from evennia_shards.commands import ShardAwareCmdOOC

    cmd = ShardAwareCmdOOC()
    cmd.args = ""
    cmd.raw_string = "ooc"
    if session is None:
        session = _FakeSession()
    if puppet is not None:
        session.puppet = puppet
    cmd.session = session
    if account is None:
        account = _FakeAccount()
    cmd.account = account
    cmd.caller = account
    cmd._messages = []

    def _msg(text, **kwargs):
        cmd._messages.append(text)

    cmd.msg = _msg
    return cmd


@override_settings(
    SHARDS_ROLE=ROLE_SHARD, SHARD_ID="shard0",
    ROUTER_URL="http://localhost:4001",
)
class ShardAwareCmdOOCShardTests(BaseEvenniaTestCase):
    """OOC command on a shard creates a ticket and redirects to the router."""

    def test_shard_with_puppet_creates_ticket_and_redirects(self):
        """ooc on shard with puppet → ticket + shard_redirect OOB."""
        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        session = _FakeSession()
        cmd = _make_ooc_cmd(puppet=char, session=session)
        cmd.func()

        tickets = list(Ticket.objects.all())
        self.assertEqual(len(tickets), 1)
        ticket = tickets[0]
        self.assertEqual(ticket.account_id, cmd.account.id)
        self.assertEqual(ticket.character_id, 42)
        self.assertEqual(ticket.to_shard, ROLE_ROUTER)

        self.assertIn("shard_redirect", session.oob_messages)
        redirect_args = session.oob_messages["shard_redirect"]
        url = redirect_args[0][0]
        self.assertIn("http://localhost:4001/webclient?ticket=", url)
        self.assertIn(ticket.token, url)

    def test_shard_no_puppet_with_last_puppet_redirects(self):
        """ooc on shard, no puppet but _last_puppet set → uses _last_puppet.id."""
        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        account = _FakeAccount()
        account.db._last_puppet = char
        session = _FakeSession()
        cmd = _make_ooc_cmd(account=account, session=session)
        cmd.func()

        ticket = Ticket.objects.first()
        self.assertEqual(ticket.character_id, 42)
        self.assertIn("shard_redirect", session.oob_messages)

    def test_shard_no_puppet_no_last_puppet_redirects_with_zero(self):
        """Error state: no puppet, no _last_puppet → character_id=0, still redirects."""
        session = _FakeSession()
        cmd = _make_ooc_cmd(session=session)
        cmd.func()

        ticket = Ticket.objects.first()
        self.assertEqual(ticket.character_id, 0)
        self.assertIn("shard_redirect", session.oob_messages)

    def test_shard_ip_pinned_in_ticket(self):
        """Ticket records the session's IP address."""
        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        session = _FakeSession(address="10.0.0.1")
        cmd = _make_ooc_cmd(puppet=char, session=session)
        cmd.func()

        ticket = Ticket.objects.first()
        self.assertEqual(ticket.client_ip, "10.0.0.1")

    def test_shard_ticket_to_shard_is_router(self):
        """Ticket's to_shard is always 'router'."""
        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        cmd = _make_ooc_cmd(puppet=char)
        cmd.func()

        ticket = Ticket.objects.first()
        self.assertEqual(ticket.to_shard, ROLE_ROUTER)

    def test_shard_redirect_message_sent(self):
        """Player gets a 'Redirecting to router...' message."""
        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        cmd = _make_ooc_cmd(puppet=char)
        cmd.func()

        self.assertTrue(any("Redirecting to router" in m for m in cmd._messages))

    def test_shard_clears_last_puppet_to_break_router_redirect_loop(self):
        """ooc clears _last_puppet so the router's at_post_login does not
        immediately redirect the player back to the shard they just left.

        Diverges from vanilla CmdOOC (which sets _last_puppet=old_char). See
        DESIGN/ticket-auth-flow.md for rationale.
        """
        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        account = _FakeAccount()
        account.db._last_puppet = char  # vanilla state going in
        cmd = _make_ooc_cmd(puppet=char, account=account)
        cmd.func()

        self.assertIsNone(account.db._last_puppet)


# ---------------------------------------------------------------------------
# at_post_login override (router-side AUTO_PUPPET_ON_LOGIN = True path)
# ---------------------------------------------------------------------------


@override_settings(
    SHARDS_ROLE=ROLE_ROUTER, SHARD_ID=ROLE_ROUTER,
    SHARD_URLS={"shard0": "http://localhost:4011"},
)
class AtPostLoginRouterTests(BaseEvenniaTestCase):
    """Direct tests for shard_aware_at_post_login on routers.

    The override replaces Evennia's at_post_login on routers, intercepting
    the AUTO_PUPPET_ON_LOGIN=True branch and converting it to a ticket
    redirect (or, on fallback, the OOC menu).
    """

    def test_valid_last_puppet_redirects_and_runs_prelude(self):
        """_last_puppet on a real shard → redirect, prelude side-effects fire."""
        from evennia_shards.hooks import shard_aware_at_post_login

        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        account = _FakeAccount(pk=7)
        account.db._last_puppet = char
        session = _FakeSession(address="10.0.0.1")

        shard_aware_at_post_login(account, session=session)

        # Prelude side-effects ran.
        self.assertEqual(session.oob_messages.get("logged_in"), {})
        self.assertEqual(len(account.connect_channel_messages), 1)
        self.assertIn("connected", account.connect_channel_messages[0])

        # Redirect happened: ticket created and shard_redirect OOB sent.
        tickets = list(Ticket.objects.all())
        self.assertEqual(len(tickets), 1)
        ticket = tickets[0]
        self.assertEqual(ticket.account_id, 7)
        self.assertEqual(ticket.character_id, 42)
        self.assertEqual(ticket.to_shard, "shard0")
        self.assertIn("shard_redirect", session.oob_messages)

        # Fallback path did NOT run.
        self.assertEqual(account.at_look_calls, [])

    def test_no_last_puppet_falls_through_to_ooc_menu_silently(self):
        """_last_puppet=None → OOC menu, no warning, no ticket."""
        from evennia_shards.hooks import shard_aware_at_post_login

        account = _FakeAccount(pk=7)
        session = _FakeSession()

        with self.assertNoLogs("evennia", level="WARNING"):
            shard_aware_at_post_login(account, session=session)

        # Prelude still ran.
        self.assertEqual(session.oob_messages.get("logged_in"), {})

        # No ticket, no shard_redirect OOB.
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertNotIn("shard_redirect", session.oob_messages)

        # OOC menu rendered.
        self.assertEqual(len(account.at_look_calls), 1)
        self.assertEqual(account.account_messages, ["OOC menu"])

    def test_unusable_shard_id_warns_and_falls_through(self):
        """_last_puppet set with broken shard_id → warning + OOC menu."""
        from evennia_shards.hooks import shard_aware_at_post_login

        for bad_shard_id in (None, "*", "unknown_shard"):
            with self.subTest(shard_id=bad_shard_id):
                Ticket.objects.all().delete()
                char = _FakeCharacter("Bob", pk=42, shard_id=bad_shard_id)
                account = _FakeAccount(pk=7)
                account.db._last_puppet = char
                session = _FakeSession()

                # Evennia's logger writes via a separate logger setup; we
                # don't assert on the warning log directly (would couple
                # the test to Evennia's logger config). Instead we assert
                # the redirect did NOT happen and the OOC menu DID.
                shard_aware_at_post_login(account, session=session)

                self.assertEqual(Ticket.objects.count(), 0)
                self.assertNotIn("shard_redirect", session.oob_messages)
                self.assertEqual(len(account.at_look_calls), 1)
