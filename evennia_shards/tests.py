# SPDX-License-Identifier: BSD-3-Clause
"""Unit tests for the evennia-shards library."""

from django.db import models
from django.test import override_settings
from evennia.objects.models import ObjectDB
from evennia.utils.test_resources import BaseEvenniaTestCase

from evennia_shards import ShardIsolationError, get_role, get_shard_id

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
