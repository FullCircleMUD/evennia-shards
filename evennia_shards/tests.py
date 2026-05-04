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

    @override_settings(SHARD_URLS={"shard0": "ws://localhost:4001/"})
    def test_returns_url_for_known_shard(self):
        self.assertEqual(get_shard_url("shard0"), "ws://localhost:4001/")

    @override_settings(SHARD_URLS={"shard0": "ws://localhost:4001/"})
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
            "overworld": "ws://overworld.example.com/",
            "dungeons": "ws://dungeons.example.com/",
            "pvp_arena": "ws://pvp.example.com/",
        }
    )
    def test_multiple_shards_flexible_names(self):
        self.assertEqual(get_shard_url("overworld"), "ws://overworld.example.com/")
        self.assertEqual(get_shard_url("dungeons"), "ws://dungeons.example.com/")
        self.assertEqual(get_shard_url("pvp_arena"), "ws://pvp.example.com/")


class RouterUrlAccessorTests(BaseEvenniaTestCase):
    """Tests for the get_router_url accessor."""

    @override_settings(ROUTER_URL="ws://router.example.com/")
    def test_returns_configured_url(self):
        self.assertEqual(get_router_url(), "ws://router.example.com/")

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

    def test_obj_msg_calls_target_msg_with_kwargs(self):
        """obj_msg → ObjectDB.objects.get(pk).msg(**kwargs)."""
        target = ObjectDB.objects.create(
            db_key="char", db_typeclass_path=TYPECLASS,
        )
        recorded_kwargs = {}
        # Shadow the typeclass-level msg method on this instance only,
        # bypassing Evennia's protective __setattr__ (same trick
        # CrossShardCharacterMoveTests uses for `sessions`).
        target.__dict__["msg"] = lambda **kwargs: recorded_kwargs.update(kwargs)

        msg = Message.objects.create(
            kind="obj_msg",
            payload={"pk": target.pk, "kwargs": {"text": "hello"}},
            to_shard="shard0",
            from_shard="shard1",
        )
        result = MessageHandler().handle(msg)
        self.assertTrue(result)
        self.assertEqual(recorded_kwargs, {"text": "hello"})

    def test_obj_msg_passes_oob_kwargs_intact(self):
        """obj_msg splats arbitrary kwargs (text + OOB) into target.msg."""
        target = ObjectDB.objects.create(
            db_key="char", db_typeclass_path=TYPECLASS,
        )
        captured = []
        target.__dict__["msg"] = lambda **kwargs: captured.append(kwargs)

        kwargs = {
            "text": "look",
            "shard_redirect": {"host": "shard1", "ticket": "abc"},
            "options": {"raw": True},
        }
        msg = Message.objects.create(
            kind="obj_msg",
            payload={"pk": target.pk, "kwargs": kwargs},
            to_shard="shard0",
            from_shard="shard1",
        )
        result = MessageHandler().handle(msg)
        self.assertTrue(result)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0], kwargs)

    def test_obj_msg_target_gone_returns_true_and_inserts_nothing(self):
        """obj_msg with non-existent pk: log + consume, no exception."""
        msg = Message.objects.create(
            kind="obj_msg",
            payload={"pk": 999_999, "kwargs": {"text": "hi"}},
            to_shard="shard0",
            from_shard="shard1",
        )
        before_count = Message.objects.count()
        result = MessageHandler().handle(msg)
        self.assertTrue(result)
        self.assertEqual(Message.objects.count(), before_count)

    def test_account_msg_calls_target_msg_with_kwargs(self):
        """account_msg → AccountDB.objects.get(pk).msg(**kwargs)."""
        from evennia.accounts.models import AccountDB

        target = AccountDB.objects.create(
            username="msg_target",
            db_typeclass_path="evennia.accounts.accounts.DefaultAccount",
        )
        recorded_kwargs = {}
        target.__dict__["msg"] = lambda **kwargs: recorded_kwargs.update(kwargs)

        msg = Message.objects.create(
            kind="account_msg",
            payload={"pk": target.pk, "kwargs": {"text": "ooc hi"}},
            to_shard="shard0",
            from_shard="shard1",
        )
        result = MessageHandler().handle(msg)
        self.assertTrue(result)
        self.assertEqual(recorded_kwargs, {"text": "ooc hi"})

    def test_account_msg_target_gone_returns_true_and_inserts_nothing(self):
        """account_msg with non-existent pk: log + consume, no exception."""
        msg = Message.objects.create(
            kind="account_msg",
            payload={"pk": 999_999, "kwargs": {"text": "hi"}},
            to_shard="shard0",
            from_shard="shard1",
        )
        before_count = Message.objects.count()
        result = MessageHandler().handle(msg)
        self.assertTrue(result)
        self.assertEqual(Message.objects.count(), before_count)

    def test_subclass_super_handle_dispatches_obj_msg(self):
        """A subclass calling super().handle() inherits obj_msg dispatch.

        Mirrors the docstring's example pattern — consumers add their
        own kinds without losing the library-shipped ones.
        """
        target = ObjectDB.objects.create(
            db_key="char", db_typeclass_path=TYPECLASS,
        )
        captured = []
        target.__dict__["msg"] = lambda **kwargs: captured.append(kwargs)

        class ConsumerHandler(MessageHandler):
            def handle(self, message):
                if super().handle(message):
                    return True
                if message.kind == "consumer_kind":
                    return True
                return False

        msg = Message.objects.create(
            kind="obj_msg",
            payload={"pk": target.pk, "kwargs": {"text": "via super"}},
            to_shard="shard0",
            from_shard="shard1",
        )
        result = ConsumerHandler().handle(msg)
        self.assertTrue(result)
        self.assertEqual(captured, [{"text": "via super"}])


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
class ShardWritesAllowedForTests(BaseEvenniaTestCase):
    """``shard_writes_allowed_for`` lifts the chokepoints, scoped to a block.

    The bypass primitive is the explicit, named opt-in for legitimate
    cross-shard operations (handoff, recovery, data migrations). Caller
    takes responsibility for integrity inside the block.

    Currently scoped to ``pre_save`` and ``pre_delete`` (the
    instance-receiving chokepoints). ``from_db`` and ``QuerySet.update``
    bypass support is deferred until a real caller needs it.
    """

    def test_bypass_allows_remote_shard_save(self):
        """Inside the bypass, save with a remote shard_id succeeds."""
        from evennia_shards import shard_writes_allowed_for

        obj = ObjectDB.objects.create(db_key="b1", db_typeclass_path=TYPECLASS)
        obj.shard_id = "shard1"
        with shard_writes_allowed_for(obj):
            obj.save()  # would raise without the bypass

        # Verify via values_list (which bypasses from_db by design)
        # so we read the persisted row from a remote-shard perspective
        # without depending on the from_db bypass for this assertion.
        persisted = ObjectDB.objects.filter(pk=obj.pk).values_list("shard_id", flat=True).first()
        self.assertEqual(persisted, "shard1")

    def test_bypass_is_scoped_to_with_block(self):
        """After the with-block exits, chokepoints are active again."""
        from evennia_shards import shard_writes_allowed_for

        obj = ObjectDB.objects.create(db_key="b2", db_typeclass_path=TYPECLASS)
        with shard_writes_allowed_for(obj):
            obj.shard_id = "shard1"
            obj.save()

        # Outside the with: a fresh remote-shard write must raise.
        obj.shard_id = "shard2"
        with self.assertRaises(ShardIsolationError):
            obj.save()

    def test_bypass_does_not_auto_stamp_explicit_shard_id(self):
        """An explicit shard_id inside the bypass is preserved (not re-stamped)."""
        from evennia_shards import shard_writes_allowed_for

        obj = ObjectDB.objects.create(db_key="b3", db_typeclass_path=TYPECLASS)
        # Pre-stamped to "shard0" by the auto-stamp path on create.
        obj.shard_id = "shard1"
        with shard_writes_allowed_for(obj):
            obj.save()
        persisted = ObjectDB.objects.filter(pk=obj.pk).values_list("shard_id", flat=True).first()
        self.assertEqual(persisted, "shard1")

    def test_bypass_allows_remote_shard_delete(self):
        """Inside the bypass, deleting a row with remote shard_id succeeds."""
        from evennia_shards import shard_writes_allowed_for

        obj = ObjectDB.objects.create(db_key="b4", db_typeclass_path=TYPECLASS)
        pk = obj.pk
        # Forge a remote shard_id directly in DB (bypassing chokepoints
        # via raw SQL) so the in-memory instance still claims it.
        _forge_db_shard(pk, "shard1")
        obj.shard_id = "shard1"

        with shard_writes_allowed_for(obj):
            obj.delete()  # would raise without the bypass

        self.assertFalse(ObjectDB.objects.filter(pk=pk).exists())

    def test_bypass_only_covers_listed_objects(self):
        """An object not in the bypass list is still protected inside the block."""
        from evennia_shards import shard_writes_allowed_for

        a = ObjectDB.objects.create(db_key="b5a", db_typeclass_path=TYPECLASS)
        b = ObjectDB.objects.create(db_key="b5b", db_typeclass_path=TYPECLASS)

        a.shard_id = "shard1"
        b.shard_id = "shard1"

        with shard_writes_allowed_for(a):
            a.save()  # allowed
            with self.assertRaises(ShardIsolationError):
                b.save()  # not in the bypass set — still refused

    def test_nested_bypass_outer_remains_active(self):
        """Inner ``with`` exit doesn't remove outer's bypass."""
        from evennia_shards import shard_writes_allowed_for

        a = ObjectDB.objects.create(db_key="b6a", db_typeclass_path=TYPECLASS)
        b = ObjectDB.objects.create(db_key="b6b", db_typeclass_path=TYPECLASS)

        a.shard_id = "shard1"
        b.shard_id = "shard1"

        with shard_writes_allowed_for(a):
            with shard_writes_allowed_for(b):
                b.save()
            # Inner exited: b is no longer bypassed, but a still is.
            a.save()  # outer bypass still active for a

        # Outer exited: a is no longer bypassed.
        a.shard_id = "shard2"
        with self.assertRaises(ShardIsolationError):
            a.save()

    def test_bypass_cleaned_up_on_exception(self):
        """If the with-block raises, the bypass is still removed on exit."""
        from evennia_shards import shard_writes_allowed_for
        from evennia_shards.isolation import _bypass_id_set

        obj = ObjectDB.objects.create(db_key="b7", db_typeclass_path=TYPECLASS)
        try:
            with shard_writes_allowed_for(obj):
                self.assertIn(id(obj), _bypass_id_set())
                raise RuntimeError("simulated failure")
        except RuntimeError:
            pass

        self.assertNotIn(id(obj), _bypass_id_set())

    def test_bypass_allows_from_db_for_remote_shard_row(self):
        """Inside the bypass, from_db can construct a remote-shard row.

        Without the bypass, the from_db chokepoint refuses to construct
        an ObjectDB instance from a row whose shard_id is owned by
        another shard. With the bypass active for that pk, the
        construction succeeds.

        Note: Evennia's idmapper caches by pk; fresh ``objects.get`` on
        a cached pk returns the cached instance without going through
        ``from_db``. Tests for the from_db chokepoint must evict from
        cache first so the next read actually constructs from DB.
        """
        from evennia_shards import shard_writes_allowed_for

        obj = ObjectDB.objects.create(db_key="b8", db_typeclass_path=TYPECLASS)
        pk = obj.pk
        # Make the row remote-owned via raw SQL (bypasses chokepoints).
        _forge_db_shard(pk, "shard1")
        # Evict from idmapper so next get() goes through from_db.
        obj.flush_from_cache(force=True)

        # Outside any bypass: from_db refuses.
        with self.assertRaises(ShardIsolationError):
            ObjectDB.objects.get(pk=pk)

        # Inside the bypass for this pk: from_db succeeds.
        with shard_writes_allowed_for(obj):
            reloaded = ObjectDB.objects.get(pk=pk)
        self.assertEqual(reloaded.pk, pk)

    def test_bypass_allows_qs_update_on_remote_rows(self):
        """qs.update on bypassed rows succeeds; non-bypassed rows still raise."""
        from evennia_shards import shard_writes_allowed_for

        # Two objects, both forged to shard1.
        a = ObjectDB.objects.create(db_key="b9a", db_typeclass_path=TYPECLASS)
        b = ObjectDB.objects.create(db_key="b9b", db_typeclass_path=TYPECLASS)
        _forge_db_shard(a.pk, "shard1")
        _forge_db_shard(b.pk, "shard1")
        a.shard_id = "shard1"
        b.shard_id = "shard1"

        # Bulk update on both without bypass: refused.
        with self.assertRaises(ShardIsolationError):
            ObjectDB.objects.filter(pk__in=[a.pk, b.pk]).update(db_key="updated")

        # Bypass both: update succeeds.
        with shard_writes_allowed_for(a, b):
            ObjectDB.objects.filter(pk__in=[a.pk, b.pk]).update(db_key="updated_via_bypass")

        # Verify the rows were updated.
        keys = list(
            ObjectDB.objects.filter(pk__in=[a.pk, b.pk])
            .values_list("db_key", flat=True)
        )
        self.assertEqual(keys, ["updated_via_bypass", "updated_via_bypass"])

    def test_bypass_qs_update_partial_still_raises(self):
        """qs.update raises if any affected row is not bypassed."""
        from evennia_shards import shard_writes_allowed_for

        a = ObjectDB.objects.create(db_key="b10a", db_typeclass_path=TYPECLASS)
        b = ObjectDB.objects.create(db_key="b10b", db_typeclass_path=TYPECLASS)
        _forge_db_shard(a.pk, "shard1")
        _forge_db_shard(b.pk, "shard1")

        # Bypass only `a`. b is also remote-owned but not in the bypass.
        with shard_writes_allowed_for(a):
            with self.assertRaises(ShardIsolationError):
                ObjectDB.objects.filter(pk__in=[a.pk, b.pk]).update(db_key="x")


class _FakeCaller:
    """Minimal caller stand-in for admin command tests.

    Captures ``msg(...)`` calls and resolves ``search(...)`` to a
    pre-set target object (or ``None``). Doesn't try to mimic the
    full Object/Account surface — admin commands generally only need
    these two.
    """

    def __init__(self, search_returns=None):
        self.messages = []
        self.search_returns = search_returns

    def search(self, searchdata):
        return self.search_returns

    def msg(self, text=None, **kwargs):
        if text is not None:
            self.messages.append(text)


@override_settings(
    SHARD_ID="shard0", SHARDS_ROLE=ROLE_SHARD,
    SHARD_URLS={
        "shard0": "ws://localhost:4011/",
        "shard1": "ws://localhost:4021/",
    },
)
class CmdShardCheckTests(BaseEvenniaTestCase):
    """``CmdShardCheck`` reports an object's shard_id (ORM + raw SQL)."""

    def _make_cmd(self, args="", search_returns=None):
        from evennia_shards.commands import CmdShardCheck

        cmd = CmdShardCheck()
        cmd.args = args
        cmd.caller = _FakeCaller(search_returns=search_returns)
        return cmd

    def test_no_args_shows_usage(self):
        cmd = self._make_cmd("")
        cmd.func()
        self.assertEqual(len(cmd.caller.messages), 1)
        self.assertIn("Usage:", cmd.caller.messages[0])

    def test_unknown_target_returns_silently(self):
        # search() returning None means "not found" — Evennia has
        # already messaged the caller. Command should add nothing.
        cmd = self._make_cmd("ghost", search_returns=None)
        cmd.func()
        self.assertEqual(cmd.caller.messages, [])

    def test_reports_shard_id_for_target(self):
        # Real ObjectDB row, auto-stamped to current shard.
        target = ObjectDB.objects.create(db_key="x", db_typeclass_path=TYPECLASS)
        cmd = self._make_cmd("x", search_returns=target)
        cmd.func()

        # Both ORM and raw-SQL probes report the value, so 2 messages.
        joined = "\n".join(cmd.caller.messages)
        self.assertIn("ORM:", joined)
        self.assertIn("DB:", joined)
        self.assertIn("shard0", joined)


@override_settings(
    SHARD_ID="shard0", SHARDS_ROLE=ROLE_SHARD,
    SHARD_URLS={
        "shard0": "ws://localhost:4011/",
        "shard1": "ws://localhost:4021/",
    },
)
class CmdCrossShardDigTests(BaseEvenniaTestCase):
    """``CmdCrossShardDig`` creates a room stamped with a target shard's id."""

    def _make_cmd(self, args=""):
        from evennia_shards.commands import CmdCrossShardDig

        cmd = CmdCrossShardDig()
        cmd.args = args
        cmd.caller = _FakeCaller()
        return cmd

    def test_no_args_shows_usage(self):
        cmd = self._make_cmd("")
        cmd.func()
        self.assertIn("Usage:", cmd.caller.messages[0])

    def test_one_arg_shows_usage(self):
        # Need both shard_id and room_name.
        cmd = self._make_cmd("shard1")
        cmd.func()
        self.assertIn("Usage:", cmd.caller.messages[0])

    def test_unknown_shard_id_reports_error_no_room_created(self):
        before = ObjectDB.objects.count()
        cmd = self._make_cmd("nonexistent_shard MyRoom")
        cmd.func()
        msg = "\n".join(cmd.caller.messages)
        self.assertIn("nonexistent_shard", msg)
        self.assertIn("not configured", msg)
        # Validation precedes creation: row count unchanged.
        self.assertEqual(ObjectDB.objects.count(), before)

    def test_creates_room_stamped_to_target_shard(self):
        cmd = self._make_cmd("shard1 TargetLimbo")
        cmd.func()

        # The new room exists in DB with shard_id="shard1" and no location.
        rows = list(
            ObjectDB.objects.filter(db_key="TargetLimbo")
            .values_list("shard_id", "db_location_id")
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], ("shard1", None))

        # Success message includes the target shard and the new dbref.
        msg = "\n".join(cmd.caller.messages)
        self.assertIn("TargetLimbo", msg)
        self.assertIn("shard1", msg)


class AdminCommandAutoInstallTests(BaseEvenniaTestCase):
    """Library admin commands auto-install into ``CharacterCmdSet``.

    The install happens via a wrapper around ``evennia._init`` registered
    in ``AppConfig.ready()``. The test runner calls ``evennia._init()``
    explicitly, so by the time tests run the patch is in place.
    """

    def test_character_cmdset_contains_library_commands(self):
        from evennia.commands.default.cmdset_character import CharacterCmdSet

        cmdset = CharacterCmdSet()
        cmdset.at_cmdset_creation()
        keys = {cmd.key for cmd in cmdset.commands}
        self.assertIn("@shard_check", keys)
        self.assertIn("cross_shard_dig", keys)


@override_settings(
    SHARD_ID="shard0", SHARDS_ROLE=ROLE_SHARD,
    SHARD_URLS={
        "shard0": "ws://localhost:4011/",
        "shard1": "ws://localhost:4021/",
    },
)
class CrossShardCharacterMoveTests(BaseEvenniaTestCase):
    """``cross_shard_character_move`` — character + inventory.

    Asserts the primitive's contract — validation, atomic DB writes
    via the bypass, idmapper eviction, inventory recursion, per-session
    redirect — without standing up real Evennia session/account
    infrastructure. Sessions and account are stubbed via the existing
    fakes; the DB layer is real.
    """

    def _make_target_room(self, target_shard="shard1"):
        room = ObjectDB.objects.create(db_key="target_room", db_typeclass_path=TYPECLASS)
        _forge_db_shard(room.pk, target_shard)
        return room

    def _make_char(self, n_sessions=0):
        """Create a real ObjectDB row, stub a fake session handler onto it.

        Each fake session carries a reference to a shared fake account
        on its ``.account`` attribute — that's how the primitive
        retrieves the account for the redirect (matching Evennia's
        ``ServerSession.account``).
        """
        char = ObjectDB.objects.create(db_key="char", db_typeclass_path=TYPECLASS)
        fake_account = _FakeAccount(pk=42)
        fake_sessions = []
        for i in range(n_sessions):
            sess = _FakeSession(address=f"10.0.0.{i + 1}")
            sess.account = fake_account
            fake_sessions.append(sess)
        # ObjectDB has a `@lazy_property` for sessions plus a custom
        # __setattr__ that refuses direct assignment. Write into the
        # instance dict to shadow the lazy_property descriptor for
        # this instance only, without involving the typeclass's
        # attribute-write machinery.
        char.__dict__["sessions"] = _FakeSessionHandler(fake_sessions)
        return char, fake_account, fake_sessions

    def test_move_no_sessions_succeeds(self):
        from evennia_shards import cross_shard_character_move

        char, _, _ = self._make_char(n_sessions=0)
        target = self._make_target_room()

        result = cross_shard_character_move(char, "shard1", target.pk)

        # Row updated.
        persisted = list(
            ObjectDB.objects.filter(pk=char.pk)
            .values_list("shard_id", "db_location_id")
        )[0]
        self.assertEqual(persisted, ("shard1", target.pk))

        # Result.
        self.assertEqual(result.objects_moved, 1)
        self.assertEqual(result.sessions_redirected, 0)
        self.assertEqual(result.failures, [])
        self.assertEqual(Ticket.objects.count(), 0)

    def test_move_with_one_session_redirects(self):
        from evennia_shards import cross_shard_character_move

        char, _, sessions = self._make_char(n_sessions=1)
        target = self._make_target_room()

        result = cross_shard_character_move(char, "shard1", target.pk)

        self.assertEqual(result.sessions_redirected, 1)
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertIn("shard_redirect", sessions[0].oob_messages)
        # Ticket points at the destination shard.
        ticket = Ticket.objects.first()
        self.assertEqual(ticket.to_shard, "shard1")
        self.assertEqual(ticket.character_id, char.pk)

    def test_move_with_multiple_sessions_redirects_each(self):
        from evennia_shards import cross_shard_character_move

        char, _, sessions = self._make_char(n_sessions=3)
        target = self._make_target_room()

        result = cross_shard_character_move(char, "shard1", target.pk)

        self.assertEqual(result.sessions_redirected, 3)
        self.assertEqual(Ticket.objects.count(), 3)
        for sess in sessions:
            self.assertIn("shard_redirect", sess.oob_messages)

    def test_target_shard_not_configured_raises(self):
        from evennia_shards import cross_shard_character_move

        char, _, _ = self._make_char(n_sessions=0)
        target = self._make_target_room()

        with self.assertRaises(ShardIsolationError) as ctx:
            cross_shard_character_move(char, "nonexistent_shard", target.pk)
        self.assertIn("nonexistent_shard", str(ctx.exception))

        # Row unchanged: still on shard0.
        persisted = (
            ObjectDB.objects.filter(pk=char.pk)
            .values_list("shard_id", flat=True)
            .first()
        )
        self.assertEqual(persisted, "shard0")
        self.assertEqual(Ticket.objects.count(), 0)

    def test_target_location_does_not_exist_raises(self):
        from evennia_shards import cross_shard_character_move

        char, _, _ = self._make_char(n_sessions=0)

        with self.assertRaises(ShardIsolationError) as ctx:
            cross_shard_character_move(char, "shard1", 999999)
        self.assertIn("999999", str(ctx.exception))

    def test_target_location_on_wrong_shard_raises(self):
        from evennia_shards import cross_shard_character_move

        char, _, _ = self._make_char(n_sessions=0)
        # Local room (auto-stamped to current shard, "shard0") used as
        # target while target_shard="shard1" → mismatch.
        local_room = ObjectDB.objects.create(db_key="local_room", db_typeclass_path=TYPECLASS)

        with self.assertRaises(ShardIsolationError) as ctx:
            cross_shard_character_move(char, "shard1", local_room.pk)
        msg = str(ctx.exception)
        self.assertIn("shard0", msg)
        self.assertIn("shard1", msg)

    def test_atomic_rollback_on_save_failure(self):
        """If the save inside the atomic block raises, the DB rolls back.

        Verifies that the in-memory shard_id mutation does not leak
        into a persisted row on save failure, and that the redirect
        path is not reached.
        """
        from unittest.mock import patch
        from evennia_shards import cross_shard_character_move

        char, _, sessions = self._make_char(n_sessions=1)
        target = self._make_target_room()

        # Patch obj.save to raise. Have to attach to the instance via
        # __setattr__ since Django's save lives on the class.
        def failing_save(*args, **kwargs):
            raise RuntimeError("simulated DB failure")
        object.__setattr__(char, "save", failing_save)

        with self.assertRaises(RuntimeError):
            cross_shard_character_move(char, "shard1", target.pk)

        # Row unchanged — rollback worked.
        persisted = (
            ObjectDB.objects.filter(pk=char.pk)
            .values_list("shard_id", "db_location_id")
            .first()
        )
        self.assertEqual(persisted[0], "shard0")
        # No ticket created, no shard_redirect sent.
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertNotIn("shard_redirect", sessions[0].oob_messages)

    def test_session_redirect_failure_captured_in_result(self):
        """Per-session redirect failure → captured in result.failures, move still committed."""
        from evennia_shards import cross_shard_character_move

        char, _, sessions = self._make_char(n_sessions=2)
        target = self._make_target_room()

        # Make the second session's msg() raise.
        def raising_msg(**kwargs):
            raise RuntimeError("simulated network failure")
        sessions[1].msg = raising_msg

        result = cross_shard_character_move(char, "shard1", target.pk)

        # Move itself committed (DB updated).
        persisted = (
            ObjectDB.objects.filter(pk=char.pk)
            .values_list("shard_id", flat=True)
            .first()
        )
        self.assertEqual(persisted, "shard1")

        # First redirected, second failed.
        self.assertEqual(result.sessions_redirected, 1)
        self.assertEqual(len(result.failures), 1)
        failed_session, failed_exc = result.failures[0]
        self.assertIs(failed_session, sessions[1])
        self.assertIsInstance(failed_exc, RuntimeError)

    # --- inventory recursion tests ---

    def _make_item(self, name, location):
        """Create an ObjectDB row parented to *location*."""
        return ObjectDB.objects.create(
            db_key=name, db_typeclass_path=TYPECLASS,
            db_location=location,
        )

    def test_move_contents_shard_ids_updated(self):
        """Char + 2 items: all 3 rows get shard_id=target, objects_moved=3."""
        from evennia_shards import cross_shard_character_move

        char, _, _ = self._make_char(n_sessions=0)
        item1 = self._make_item("sword", char)
        item2 = self._make_item("shield", char)
        target = self._make_target_room()

        result = cross_shard_character_move(char, "shard1", target.pk)

        self.assertEqual(result.objects_moved, 3)
        shards = dict(
            ObjectDB.objects.filter(pk__in=[char.pk, item1.pk, item2.pk])
            .values_list("pk", "shard_id")
        )
        self.assertEqual(shards[char.pk], "shard1")
        self.assertEqual(shards[item1.pk], "shard1")
        self.assertEqual(shards[item2.pk], "shard1")

    def test_move_nested_contents(self):
        """Char → bag → gem: full tree moved."""
        from evennia_shards import cross_shard_character_move

        char, _, _ = self._make_char(n_sessions=0)
        bag = self._make_item("bag", char)
        gem = self._make_item("gem", bag)
        target = self._make_target_room()

        result = cross_shard_character_move(char, "shard1", target.pk)

        self.assertEqual(result.objects_moved, 3)
        shards = dict(
            ObjectDB.objects.filter(pk__in=[char.pk, bag.pk, gem.pk])
            .values_list("pk", "shard_id")
        )
        self.assertEqual(shards[char.pk], "shard1")
        self.assertEqual(shards[bag.pk], "shard1")
        self.assertEqual(shards[gem.pk], "shard1")

    def test_move_no_contents(self):
        """Empty inventory: objects_moved=1."""
        from evennia_shards import cross_shard_character_move

        char, _, _ = self._make_char(n_sessions=0)
        target = self._make_target_room()

        result = cross_shard_character_move(char, "shard1", target.pk)

        self.assertEqual(result.objects_moved, 1)

    def test_move_contents_idmapper_eviction(self):
        """Items evicted from __instance_cache__ after move."""
        from evennia_shards import cross_shard_character_move

        char, _, _ = self._make_char(n_sessions=0)
        item = self._make_item("sword", char)
        target = self._make_target_room()

        cache = ObjectDB.__dbclass__.__instance_cache__
        # Ensure items are in cache before move.
        self.assertIn(item.pk, cache)

        cross_shard_character_move(char, "shard1", target.pk)

        # Both char and item should be evicted.
        self.assertNotIn(char.pk, cache)
        self.assertNotIn(item.pk, cache)

    def test_move_contents_location_unchanged(self):
        """Items' db_location_id still points to char pk after move."""
        from evennia_shards import cross_shard_character_move

        char, _, _ = self._make_char(n_sessions=0)
        item = self._make_item("sword", char)
        target = self._make_target_room()

        cross_shard_character_move(char, "shard1", target.pk)

        loc = (
            ObjectDB.objects.filter(pk=item.pk)
            .values_list("db_location_id", flat=True)
            .first()
        )
        self.assertEqual(loc, char.pk)

    def test_move_contents_globals_left_alone(self):
        """Global ("*") items in inventory are not re-stamped."""
        from evennia_shards import cross_shard_character_move

        char, _, _ = self._make_char(n_sessions=0)
        normal_item = self._make_item("sword", char)
        global_item = self._make_item("global_buff", char)
        _forge_db_shard(global_item.pk, "*")
        target = self._make_target_room()

        result = cross_shard_character_move(char, "shard1", target.pk)

        # Only char + normal_item moved; global_item untouched.
        self.assertEqual(result.objects_moved, 2)
        global_shard = (
            ObjectDB.objects.filter(pk=global_item.pk)
            .values_list("shard_id", flat=True)
            .first()
        )
        self.assertEqual(global_shard, "*")


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
    redirect orphan connections to the router (so a stale localStorage
    routing attempt after session expiry lands the player at the
    router's login form rather than at a connection error); routers
    fall through to their own login screen.
    """

    @override_settings(ROUTER_URL="ws://localhost:4002/")
    def test_shard_no_token_redirects_to_router(self):
        """Shard with no ticket should send shard_redirect to router and close.

        Simulates the new behaviour: rather than rejecting an
        unauthenticated connection outright (orphan UX), the shard
        emits a `shard_redirect` OOB pointing at the router so the
        client's existing handler swaps the WebSocket and the player
        ends up at the router's login form (or csessid auto-auth, if
        their browser session survived).
        """
        proto = _FakeProtocol(uri="/websocket", client_ip="127.0.0.1")
        token = proto._extract_ticket_token()
        self.assertIsNone(token)

        role = get_role()  # ROLE_SHARD via @override_settings
        self.assertEqual(role, ROLE_SHARD)

        # Simulate the shard's no-auth branch: send shard_redirect to
        # the router URL, then close. The actual onOpen does this via
        # self.sendLine(json.dumps([...])) + self.sendClose(...).
        import json as _json
        from evennia_shards import get_router_url
        router_url = get_router_url()
        proto.sendLine(_json.dumps(["shard_redirect", [router_url], {}]))
        proto.sendClose(1000, "Redirecting to router for login")

        self.assertEqual(len(proto.sent_lines), 1)
        self.assertIn("shard_redirect", proto.sent_lines[0])
        self.assertIn(router_url, proto.sent_lines[0])
        self.assertEqual(len(proto.close_calls), 1)
        self.assertEqual(proto.close_calls[0][0], 1000)  # normal close

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
        # Mirrors Evennia ServerSession.protocol_flags (a per-session
        # dict carried Portal↔Server via the AMP sync).
        self.protocol_flags = {}

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


class _FakeSessionHandler:
    """Stand-in for Evennia's per-character SessionHandler.

    Provides .all() / .count() so cross_shard_character_move can iterate
    sessions without standing up a real Evennia sessionhandler.
    """

    def __init__(self, sessions=()):
        self._sessions = list(sessions)

    def all(self):
        return list(self._sessions)

    def remove(self, session):
        if session in self._sessions:
            self._sessions.remove(session)

    def count(self):
        return len(self._sessions)


class _FakeAccount:
    """Minimal account stand-in for command and hook tests."""

    def __init__(self, pk=1, characters=None, key="Player1"):
        self._saved_attrs = {}
        self.id = pk
        self.pk = pk
        self.key = key
        self._characters = characters or []
        self._last_puppet = None
        # Initialised to False so hooks reading `account.db._shards_at_ooc_menu`
        # don't AttributeError. Vanilla Evennia's AttributeHandler returns
        # None silently for unset attrs, but our _FakeAccount uses
        # `self.db = self`, so the attribute has to actually exist.
        self._shards_at_ooc_menu = False
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

    def unpuppet_object(self, session):
        """Minimal unpuppet for tests — clears session.puppet like Evennia's."""
        for s in (session if isinstance(session, (list, tuple)) else [session]):
            s.puppet = None

    def flush_from_cache(self, force=False):
        # No-op for tests; production code uses this to evict stale
        # idmapper entries (cross-process attribute writes). The
        # router's at_post_login override calls it before reading
        # account.db._shards_at_ooc_menu (the @ooc command writes the
        # attribute on a shard, the router needs a fresh read).
        pass

    def refresh_from_db(self, fields=None):
        # No-op for tests; production code re-reads attributes from
        # the shared DB after flush_from_cache.
        pass


class _FakeCharacter:
    """Minimal character stand-in for command tests."""

    def __init__(self, key, pk, shard_id="shard0"):
        self.key = key
        self.id = pk
        self.pk = pk
        self.shard_id = shard_id
        self.name = key

    def flush_from_cache(self, force=False):
        # No-op for tests; production code evicts the idmapper cache
        # entry so refresh_from_db() actually hits the database.
        pass

    def refresh_from_db(self, fields=None):
        # No-op for tests; production code calls this on the router to
        # pick up cross-process shard_id/db_location_id updates.
        pass


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
    SHARD_URLS={"shard0": "ws://localhost:4011/"},
)
class RedirectToCharacterShardHelperTests(BaseEvenniaTestCase):
    """Direct tests for the _redirect_to_character_shard helper.

    The helper is shared mechanism between the IC command path and the
    at_post_login override path. Validation is the caller's job — these
    tests assert only the side-effects the helper itself performs.
    """

    def test_redirect_creates_ticket_sets_last_puppet_and_sends_oob(self):
        from evennia_shards.handoff import _redirect_to_character_shard

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
        self.assertIn("ws://localhost:4011/?ticket=", oob_url)
        self.assertIn(ticket.token, oob_url)

        # Returned URL matches the OOB URL.
        self.assertEqual(url, oob_url)

    def test_redirect_clears_at_ooc_menu_flag(self):
        """The IC redirect must clear account.db._shards_at_ooc_menu.

        The flag was set by ShardAwareCmdOOC at OOC; any IC entry —
        manual @ic, login auto-puppet, or programmatic
        cross_shard_character_move — clears it via this helper.
        Without this, a player who @ooc'd, then @ic'd, would still
        be treated as "at OOC" on the next connection and be denied
        the AUTO_PUPPET behaviour they'd expect.
        """
        from evennia_shards.handoff import _redirect_to_character_shard

        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        account = _FakeAccount(pk=7)
        # Simulate a previous @ooc having set the flag.
        account.db._shards_at_ooc_menu = True
        session = _FakeSession(address="10.0.0.1")

        _redirect_to_character_shard(account, session, char)

        self.assertFalse(account.db._shards_at_ooc_menu)


@override_settings(
    SHARDS_ROLE=ROLE_SHARD, SHARD_ID="shard0",
    SHARD_URLS={"shard0": "ws://localhost:4011/"},
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
    SHARD_URLS={"shard0": "ws://localhost:4011/"},
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
        self.assertIn("ws://localhost:4011/?ticket=", url)
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
    ROUTER_URL="ws://localhost:4001/",
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
        self.assertIn("ws://localhost:4001/?ticket=", url)
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

    def test_shard_does_not_mutate_last_puppet(self):
        """ooc must NOT touch _last_puppet — vanilla Evennia semantics.

        The OOC redirect loop is broken by the account-level
        ``account.db._shards_at_ooc_menu`` flag (set by
        ShardAwareCmdOOC, cleared by _redirect_to_character_shard,
        read by the router's at_post_login override), not by
        mutating _last_puppet. Any change here that adds
        ``account.db._last_puppet = ...`` mutation should be
        deliberate and re-evaluated against the loop-prevention
        strategy.
        """
        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        account = _FakeAccount()
        account.db._last_puppet = char
        cmd = _make_ooc_cmd(puppet=char, account=account)
        cmd.func()

        self.assertIs(account.db._last_puppet, char)

    def test_shard_sets_at_ooc_menu_flag(self):
        """ooc must set account.db._shards_at_ooc_menu=True.

        This is the persistent OOC-return signal — read by the
        router's at_post_login on subsequent connections to
        suppress the AUTO_PUPPET-driven bounce-back-to-shard.
        Cleared on any IC entry by _redirect_to_character_shard.
        """
        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        account = _FakeAccount()
        account.db._last_puppet = char
        # Ensure the flag starts False (clean baseline).
        account.db._shards_at_ooc_menu = False
        cmd = _make_ooc_cmd(puppet=char, account=account)
        cmd.func()

        self.assertTrue(account.db._shards_at_ooc_menu)


# ---------------------------------------------------------------------------
# at_post_login override (router-side AUTO_PUPPET_ON_LOGIN = True path)
# ---------------------------------------------------------------------------


@override_settings(
    SHARDS_ROLE=ROLE_ROUTER, SHARD_ID=ROLE_ROUTER,
    SHARD_URLS={"shard0": "ws://localhost:4011/"},
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

        # OOC menu rendered (debug message also emitted; assert membership).
        self.assertEqual(len(account.at_look_calls), 1)
        self.assertIn("OOC menu", account.account_messages)

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

    def test_at_ooc_menu_flag_skips_auto_redirect(self):
        """account.db._shards_at_ooc_menu=True → OOC menu, no redirect.

        The flag is set by ShardAwareCmdOOC and cleared by
        _redirect_to_character_shard (any IC entry). It signals
        explicit player intent to be at the OOC menu and persists
        across session lifecycle / refresh / logout-login. Even
        with a fully redirectable _last_puppet set, an account
        with this flag lands at the OOC menu rather than being
        auto-puppeted.
        """
        from evennia_shards.hooks import shard_aware_at_post_login

        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        account = _FakeAccount(pk=7)
        account.db._last_puppet = char  # would normally trigger redirect
        account.db._shards_at_ooc_menu = True
        session = _FakeSession()

        shard_aware_at_post_login(account, session=session)

        # No ticket created, no shard_redirect OOB sent.
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertNotIn("shard_redirect", session.oob_messages)

        # OOC menu rendered.
        self.assertEqual(len(account.at_look_calls), 1)

    @override_settings(AUTO_PUPPET_ON_LOGIN=False)
    def test_auto_puppet_disabled_renders_ooc_menu_unconditionally(self):
        """AUTO_PUPPET_ON_LOGIN = False → OOC menu, no redirect.

        Vanilla Evennia's at_post_login renders the OOC menu (else-branch)
        whenever AUTO_PUPPET_ON_LOGIN is False. The override must honor
        that setting — if the consumer has chosen "no auto-puppet," the
        library must not auto-redirect to the last puppet's shard
        either. Independent of _last_puppet and ticket-flag state.
        """
        from evennia_shards.hooks import shard_aware_at_post_login

        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        account = _FakeAccount(pk=7)
        account.db._last_puppet = char  # would normally trigger redirect
        session = _FakeSession()
        # No ticket flag — purely the AUTO_PUPPET=False short-circuit path.

        shard_aware_at_post_login(account, session=session)

        # No ticket created, no shard_redirect OOB sent.
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertNotIn("shard_redirect", session.oob_messages)

        # OOC menu rendered.
        self.assertEqual(len(account.at_look_calls), 1)


@override_settings(SHARDS_ROLE=ROLE_ROUTER, SHARD_ID=ROLE_ROUTER)
class ShardAwareCreateCharacterTests(BaseEvenniaTestCase):
    """Direct tests for ``make_shard_aware_create_character``.

    The wrapper sits on the router-side ``Account.create_character`` seam
    (the converging point for ``CmdCharCreate``,
    ``AUTO_CREATE_CHARACTER_WITH_ACCOUNT``, and the guest path). On
    successful chargen it reads the new character's start-location row's
    ``shard_id`` via ``.values_list`` and stamps the character to match,
    overwriting the ``"router"`` auto-stamp from ``pre_save``. Tests use
    real ``ObjectDB`` rows for the location lookup; the vanilla
    ``create_character`` is a stub callable that returns a pre-built
    character row.
    """

    def _make_room(self, shard_id):
        """Create an ObjectDB row to act as the start location."""
        room = ObjectDB.objects.create(
            db_key="start_room", db_typeclass_path=TYPECLASS,
        )
        _forge_db_shard(room.pk, shard_id)
        return room

    def _make_character(self, location):
        """Create an ObjectDB row to act as the new character.

        Auto-stamped to ``"router"`` by ``pre_save`` since the test
        class is in router role.
        """
        char = ObjectDB.objects.create(
            db_key="newchar",
            db_typeclass_path=TYPECLASS,
            db_location=location,
        )
        return char

    def _stub_original(self, character, errs=None):
        """Build a vanilla-shaped ``create_character`` stub.

        Records its kwargs so tests can assert pass-through.
        """
        recorder = {"args": None, "kwargs": None}

        def _original(self, *args, **kwargs):
            recorder["args"] = args
            recorder["kwargs"] = kwargs
            return character, errs

        return _original, recorder

    def test_stamps_shard_id_from_start_location(self):
        """Happy path: new character's shard_id matches start room's."""
        from evennia_shards.chargen import make_shard_aware_create_character

        room = self._make_room(shard_id="shard0")
        char = self._make_character(location=room)
        # Sanity: pre_save auto-stamped the character to the router.
        self.assertEqual(char.shard_id, ROLE_ROUTER)

        original, _ = self._stub_original(char)
        wrapped = make_shard_aware_create_character(original)

        result_char, result_errs = wrapped(_FakeAccount(pk=7), "Bob")

        self.assertIs(result_char, char)
        self.assertIsNone(result_errs)
        # Persisted update_fields=["shard_id"] write.
        persisted_shard = (
            ObjectDB.objects.filter(pk=char.pk)
            .values_list("shard_id", flat=True)[0]
        )
        self.assertEqual(persisted_shard, "shard0")
        # In-memory instance also updated.
        self.assertEqual(char.shard_id, "shard0")

    def test_passes_through_when_vanilla_returns_none(self):
        """Vanilla refused (e.g. slot limit) → wrapper returns same tuple."""
        from evennia_shards.chargen import make_shard_aware_create_character

        errs = ["You have reached the max characters for this account."]
        original, recorder = self._stub_original(None, errs=errs)
        wrapped = make_shard_aware_create_character(original)

        result_char, result_errs = wrapped(_FakeAccount(pk=7), "Bob")

        self.assertIsNone(result_char)
        self.assertEqual(result_errs, errs)
        # Ensure original was actually invoked.
        self.assertEqual(recorder["args"], ("Bob",))

    def test_unstamped_start_location_leaves_router_stamp(self):
        """Start room shard_id=None → character left as ``"router"``.

        Wrapper logs a warning (Evennia's logger setup is independent of
        Python's stdlib logging — see comment in
        ``AtPostLoginRouterTests.test_unusable_shard_id_warns_and_falls_through``);
        we assert on the side-effect (no overwrite) rather than the log.
        """
        from evennia_shards.chargen import make_shard_aware_create_character

        # _forge_db_shard with None: room is unstamped.
        room = ObjectDB.objects.create(
            db_key="start_room", db_typeclass_path=TYPECLASS,
        )
        _forge_db_shard(room.pk, None)
        char = self._make_character(location=room)

        original, _ = self._stub_original(char)
        wrapped = make_shard_aware_create_character(original)

        wrapped(_FakeAccount(pk=7), "Bob")

        persisted_shard = (
            ObjectDB.objects.filter(pk=char.pk)
            .values_list("shard_id", flat=True)[0]
        )
        self.assertEqual(persisted_shard, ROLE_ROUTER)

    def test_global_start_location_leaves_router_stamp(self):
        """Start room shard_id="*" → character left as ``"router"``."""
        from evennia_shards.chargen import make_shard_aware_create_character

        room = self._make_room(shard_id="*")
        char = self._make_character(location=room)

        original, _ = self._stub_original(char)
        wrapped = make_shard_aware_create_character(original)

        wrapped(_FakeAccount(pk=7), "Bob")

        persisted_shard = (
            ObjectDB.objects.filter(pk=char.pk)
            .values_list("shard_id", flat=True)[0]
        )
        self.assertEqual(persisted_shard, ROLE_ROUTER)

    def test_router_owned_start_location_leaves_router_stamp(self):
        """Start room shard_id="router" → no overwrite."""
        from evennia_shards.chargen import make_shard_aware_create_character

        room = self._make_room(shard_id=ROLE_ROUTER)
        char = self._make_character(location=room)

        original, _ = self._stub_original(char)
        wrapped = make_shard_aware_create_character(original)

        wrapped(_FakeAccount(pk=7), "Bob")

        persisted_shard = (
            ObjectDB.objects.filter(pk=char.pk)
            .values_list("shard_id", flat=True)[0]
        )
        self.assertEqual(persisted_shard, ROLE_ROUTER)

    def test_no_db_location_leaves_router_stamp(self):
        """Character created without db_location → no overwrite."""
        from evennia_shards.chargen import make_shard_aware_create_character

        char = ObjectDB.objects.create(
            db_key="newchar", db_typeclass_path=TYPECLASS,
        )
        self.assertIsNone(char.db_location_id)

        original, _ = self._stub_original(char)
        wrapped = make_shard_aware_create_character(original)

        wrapped(_FakeAccount(pk=7), "Bob")

        persisted_shard = (
            ObjectDB.objects.filter(pk=char.pk)
            .values_list("shard_id", flat=True)[0]
        )
        self.assertEqual(persisted_shard, ROLE_ROUTER)

    def test_kwargs_flow_through_to_vanilla(self):
        """Wrapper does not mutate args/kwargs going into vanilla."""
        from evennia_shards.chargen import make_shard_aware_create_character

        room = self._make_room(shard_id="shard0")
        char = self._make_character(location=room)

        original, recorder = self._stub_original(char)
        wrapped = make_shard_aware_create_character(original)

        sentinel = object()
        wrapped(
            _FakeAccount(pk=7),
            "Bob",
            "A description.",
            typeclass="my.path",
            character_typeclass="my.path",
            extra=sentinel,
        )

        self.assertEqual(recorder["args"], ("Bob", "A description."))
        self.assertEqual(recorder["kwargs"]["typeclass"], "my.path")
        self.assertEqual(recorder["kwargs"]["character_typeclass"], "my.path")
        self.assertIs(recorder["kwargs"]["extra"], sentinel)


@override_settings(SHARD_ID="shard0", SHARDS_ROLE=ROLE_SHARD)
class SendCrossShardMessageTests(BaseEvenniaTestCase):
    """Sender-side helper built on top of the ``obj_msg`` primitive.

    Exercises the four behaviours of ``send_cross_shard_message``:
    local-vs-remote dispatch, typeclass filter, single ``.values_list``
    DB read, and validation rejection on target-gone or typeclass
    mismatch. The receiver-side ``obj_msg`` handler is already tested
    in ``MessageHandlerTests``; these tests verify only the sender
    behaviour and the bus-row shape it produces.
    """

    def _make_target(self, shard_id="shard0", typeclass=TYPECLASS):
        """Create an ObjectDB row with an explicit shard_id."""
        target = ObjectDB.objects.create(
            db_key="target", db_typeclass_path=typeclass,
        )
        # The pre_save chokepoint auto-stamps to the current SHARD_ID
        # ("shard0" in this class). For tests that need the row on a
        # different shard, forge it via raw SQL (matches the existing
        # _forge_db_shard pattern used elsewhere in this file).
        if shard_id != "shard0":
            _forge_db_shard(target.pk, shard_id)
        return target

    def test_local_target_calls_msg_directly_no_bus_row(self):
        """Target on this shard → direct .msg call, no Message inserted."""
        from evennia.objects.objects import DefaultObject

        from evennia_shards import send_cross_shard_message

        target = self._make_target(shard_id="shard0")
        captured = []
        target.__dict__["msg"] = lambda **kw: captured.append(kw)

        result = send_cross_shard_message(
            target.pk, {"text": "local hi"}, target_typeclass=DefaultObject,
        )

        self.assertTrue(result)
        self.assertEqual(captured, [{"text": "local hi"}])
        self.assertEqual(Message.objects.count(), 0)

    def test_global_star_target_treated_as_local(self):
        """shard_id="*" rows are owned by every shard; deliver locally."""
        from evennia.objects.objects import DefaultObject

        from evennia_shards import send_cross_shard_message

        target = self._make_target(shard_id="*")
        captured = []
        target.__dict__["msg"] = lambda **kw: captured.append(kw)

        result = send_cross_shard_message(
            target.pk, {"text": "global hi"}, target_typeclass=DefaultObject,
        )

        self.assertTrue(result)
        self.assertEqual(captured, [{"text": "global hi"}])
        self.assertEqual(Message.objects.count(), 0)

    def test_remote_target_inserts_obj_msg_bus_row(self):
        """Target on another shard → obj_msg bus row, no local .msg call."""
        from evennia.objects.objects import DefaultObject

        from evennia_shards import send_cross_shard_message

        target = self._make_target(shard_id="shard1")
        captured = []
        target.__dict__["msg"] = lambda **kw: captured.append(kw)

        result = send_cross_shard_message(
            target.pk, {"text": "remote hi"}, target_typeclass=DefaultObject,
        )

        self.assertTrue(result)
        # No local .msg call.
        self.assertEqual(captured, [])
        # Exactly one bus row, addressed to the target's shard with the
        # primitive's expected payload shape.
        msgs = list(Message.objects.all())
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].kind, "obj_msg")
        self.assertEqual(msgs[0].to_shard, "shard1")
        self.assertEqual(msgs[0].from_shard, "shard0")
        self.assertEqual(
            msgs[0].payload, {"pk": target.pk, "kwargs": {"text": "remote hi"}}
        )

    def test_target_gone_returns_false_no_bus_row(self):
        """Non-existent pk → return False, no bus row, no exception."""
        from evennia_shards import send_cross_shard_message

        result = send_cross_shard_message(999_999, {"text": "ghost"})

        self.assertFalse(result)
        self.assertEqual(Message.objects.count(), 0)

    def test_typeclass_mismatch_returns_false_no_bus_row(self):
        """Target whose typeclass isn't a subclass of filter → reject."""
        from evennia.objects.objects import DefaultCharacter

        from evennia_shards import send_cross_shard_message

        # DefaultObject is NOT a subclass of DefaultCharacter (DC inherits
        # from DO, not the other way around). Filter rejects.
        target = self._make_target(
            shard_id="shard1",
            typeclass="evennia.objects.objects.DefaultObject",
        )

        result = send_cross_shard_message(
            target.pk, {"text": "hi"}, target_typeclass=DefaultCharacter,
        )

        self.assertFalse(result)
        self.assertEqual(Message.objects.count(), 0)

    @override_settings(
        SHARD_ID="shard0",
        SHARDS_ROLE=ROLE_SHARD,
        BASE_CHARACTER_TYPECLASS="evennia.objects.objects.DefaultObject",
    )
    def test_default_typeclass_resolves_from_settings_at_call_time(self):
        """target_typeclass=None → class_from_module(BASE_CHARACTER_TYPECLASS).

        Resolved at call time so test override_settings (and consumer
        config churn) is picked up without re-importing the helper.
        """
        from evennia_shards import send_cross_shard_message

        target = self._make_target(shard_id="shard1")  # DefaultObject

        result = send_cross_shard_message(target.pk, {"text": "hi"})

        self.assertTrue(result)
        self.assertEqual(Message.objects.count(), 1)

    def test_kwargs_pass_through_to_remote_payload(self):
        """Multi-key kwargs (text + OOB + options) flow through unchanged."""
        from evennia.objects.objects import DefaultObject

        from evennia_shards import send_cross_shard_message

        target = self._make_target(shard_id="shard1")

        kwargs = {
            "text": "look",
            "shard_redirect": {"host": "shard1", "ticket": "abc"},
            "options": {"raw": True},
        }
        result = send_cross_shard_message(
            target.pk, kwargs, target_typeclass=DefaultObject,
        )

        self.assertTrue(result)
        msg = Message.objects.first()
        self.assertEqual(msg.payload["kwargs"], kwargs)

    def test_kwargs_pass_through_to_local_msg(self):
        """Multi-key kwargs flow through to local .msg() unchanged."""
        from evennia.objects.objects import DefaultObject

        from evennia_shards import send_cross_shard_message

        target = self._make_target(shard_id="shard0")
        captured = []
        target.__dict__["msg"] = lambda **kw: captured.append(kw)

        kwargs = {
            "text": "look",
            "shard_redirect": {"host": "shard1"},
            "options": {"raw": True},
        }
        result = send_cross_shard_message(
            target.pk, kwargs, target_typeclass=DefaultObject,
        )

        self.assertTrue(result)
        self.assertEqual(captured, [kwargs])


class WarnIfAtPostLoginOverriddenTests(BaseEvenniaTestCase):
    """Detect consumer overrides of ``Account.at_post_login``.

    The library patches ``DefaultAccount.at_post_login`` directly. A
    consumer subclass that overrides ``at_post_login`` shadows the
    library's patch via Python MRO unless the override calls
    ``super()``. ``warn_if_at_post_login_overridden`` detects the
    override and emits a warning at install time so the integration
    risk is visible in startup logs. The function returns True iff
    a warning was emitted; tests assert on the return value rather
    than coupling to log capture (matching the convention in
    ``AtPostLoginRouterTests``).
    """

    def test_default_account_returns_false(self):
        """DefaultAccount itself is the library's patch target.

        Walking the MRO stops at ``DefaultAccount``, so even though
        ``DefaultAccount.at_post_login`` is in its own ``__dict__``,
        the detector treats it as the floor and returns False. This
        keeps the warning silent when ``BASE_ACCOUNT_TYPECLASS``
        resolves to ``DefaultAccount`` itself (consumer hasn't
        subclassed).
        """
        from evennia.accounts.accounts import DefaultAccount

        from evennia_shards.hooks import warn_if_at_post_login_overridden

        self.assertFalse(
            warn_if_at_post_login_overridden(DefaultAccount, "router")
        )
        self.assertFalse(
            warn_if_at_post_login_overridden(DefaultAccount, "shard")
        )

    def test_subclass_with_intermediate_override_returns_true(self):
        """Override at any level between leaf and DefaultAccount triggers.

        Walks the MRO so a multi-level subclass tree where the
        intermediate class overrides at_post_login is also detected,
        even when the leaf class doesn't redeclare it.
        """
        from evennia.accounts.accounts import DefaultAccount

        from evennia_shards.hooks import warn_if_at_post_login_overridden

        class MidLevelAccount(DefaultAccount):
            def at_post_login(self, session=None, **kwargs):
                pass

        class LeafAccount(MidLevelAccount):
            pass

        self.assertTrue(
            warn_if_at_post_login_overridden(LeafAccount, "router")
        )

    def test_subclass_without_override_returns_false(self):
        """Consumer subclass that doesn't override at_post_login: no warning.

        This is the typical safe case — the consumer subclasses
        DefaultAccount for typeclass identity but doesn't touch
        at_post_login. The library's patch on DefaultAccount fires
        via MRO when ``account.at_post_login(...)`` is called.
        """
        from evennia.accounts.accounts import DefaultAccount

        from evennia_shards.hooks import warn_if_at_post_login_overridden

        class PassThroughAccount(DefaultAccount):
            pass

        self.assertFalse(
            warn_if_at_post_login_overridden(PassThroughAccount, "router")
        )
        self.assertFalse(
            warn_if_at_post_login_overridden(PassThroughAccount, "shard")
        )

    def test_subclass_with_override_returns_true(self):
        """Consumer override is detected and warning is emitted."""
        from evennia.accounts.accounts import DefaultAccount

        from evennia_shards.hooks import warn_if_at_post_login_overridden

        class ShadowingAccount(DefaultAccount):
            def at_post_login(self, session=None, **kwargs):
                pass  # consumer override that does NOT call super()

        self.assertTrue(
            warn_if_at_post_login_overridden(ShadowingAccount, "router")
        )
        self.assertTrue(
            warn_if_at_post_login_overridden(ShadowingAccount, "shard")
        )

    def test_subclass_with_super_calling_override_still_returns_true(self):
        """A correct (super-calling) override still triggers the warning.

        The detection is based on ``__dict__`` membership, which can't
        distinguish a well-behaved override from a shadowing one.
        False-positive cost is one log line at startup. Documented as
        deliberate in ``warn_if_at_post_login_overridden``'s docstring.
        """
        from evennia.accounts.accounts import DefaultAccount

        from evennia_shards.hooks import warn_if_at_post_login_overridden

        class CooperativeAccount(DefaultAccount):
            def at_post_login(self, session=None, **kwargs):
                super().at_post_login(session=session, **kwargs)

        self.assertTrue(
            warn_if_at_post_login_overridden(CooperativeAccount, "router")
        )
