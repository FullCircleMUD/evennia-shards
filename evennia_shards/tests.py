# SPDX-License-Identifier: BSD-3-Clause
"""Unit tests for the evennia-shards library."""

from django.db import models
from django.test import override_settings
from evennia.objects.models import ObjectDB
from evennia.utils.test_resources import BaseEvenniaTestCase

from evennia_shards import (
    ShardIsolationError,
    get_message_timeout,
    get_role,
    get_shard_id,
    send_message,
)
from evennia_shards.models import Message

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

    @override_settings(SHARDS_ROLE="router")
    def test_get_role_reflects_setting_router(self):
        self.assertEqual(get_role(), "router")

    @override_settings(SHARDS_ROLE="shard")
    def test_get_role_reflects_setting_shard(self):
        self.assertEqual(get_role(), "shard")

    @override_settings(SHARD_ID="some-shard")
    def test_get_shard_id_reflects_setting(self):
        self.assertEqual(get_shard_id(), "some-shard")


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


@override_settings(SHARD_ID="shard0", SHARDS_ROLE="shard")
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


class AppSetupTests(BaseEvenniaTestCase):
    """AppConfig.ready() wires shard_id onto ObjectDB."""

    def test_shard_id_field_wired_on_objectdb(self):
        field = ObjectDB._meta.get_field("shard_id")
        self.assertIsInstance(field, models.CharField)
        self.assertEqual(field.max_length, 64)
        self.assertTrue(field.null)
        self.assertTrue(field.blank)
        self.assertTrue(field.db_index)


@override_settings(SHARD_ID="shard0", SHARDS_ROLE="shard")
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


@override_settings(SHARD_ID="shard0", SHARDS_ROLE="shard")
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


@override_settings(SHARD_ID="shard0", SHARDS_ROLE="shard")
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


@override_settings(SHARD_ID="shard0", SHARDS_ROLE="shard")
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
