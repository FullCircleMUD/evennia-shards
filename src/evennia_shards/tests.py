# SPDX-License-Identifier: BSD-3-Clause
"""Unit tests for the evennia-shards library.

This file is being rebuilt progressively as the library is refactored onto
django-multitenant. The previous test suite is preserved at
``_legacy_tests.py`` (excluded from discovery by the leading underscore);
unchanged tests will be ported back as each subsystem is verified against
the new tenancy model.
"""

import unittest

from django.test import override_settings
from django_multitenant.utils import (
    get_current_tenant,
    get_current_tenant_value,
    unset_current_tenant,
)
from evennia.objects.models import ObjectDB
from evennia.utils.test_resources import BaseEvenniaTestCase

from evennia_shards.tenancy import (
    GLOBAL_SHARD_ID,
    Shard,
    bootstrap_tenant_context,
    clear_shard_context,
    install_tenancy_on_objectdb,
    set_current_shard,
    shard_context,
)

TYPECLASS = "evennia.objects.objects.DefaultObject"


def _forge_db_shard(pk: int, shard_id: str | None) -> None:
    """Force a row's ``shard_id`` via raw SQL — bypasses every chokepoint.

    Used by tests that need to set up "this row lives on another shard"
    scenarios without going through any of the library's safeguards.
    Same helper shape as the legacy chokepoint tests.
    """
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE objects_objectdb SET shard_id=%s WHERE id=%s",
            [shard_id, pk],
        )


class ShardObjectTests(unittest.TestCase):
    """The Shard stand-in exists only to satisfy multitenant's
    ``tenant.tenant_value`` protocol. Verify the surface."""

    def test_tenant_value_attribute(self):
        self.assertEqual(Shard("shard0").tenant_value, "shard0")
        self.assertEqual(Shard(GLOBAL_SHARD_ID).tenant_value, "*")

    def test_repr_includes_shard_id(self):
        self.assertEqual(repr(Shard("shard0")), "Shard('shard0')")

    def test_equality_by_shard_id(self):
        self.assertEqual(Shard("shard0"), Shard("shard0"))
        self.assertNotEqual(Shard("shard0"), Shard("shard1"))

    def test_equality_with_non_shard_is_notimplemented(self):
        # Returning NotImplemented lets Python fall back to the other
        # side's __eq__; sanity-check the != case too.
        self.assertNotEqual(Shard("shard0"), "shard0")
        self.assertNotEqual(Shard("shard0"), 0)

    def test_hash_matches_equality(self):
        # Set membership depends on hash + eq agreeing.
        self.assertEqual(
            len({Shard("a"), Shard("a"), Shard("b")}),
            2,
        )

    def test_global_shard_id_constant(self):
        # The "*" sentinel is hard-wired across the codebase; the
        # constant is the canonical source.
        self.assertEqual(GLOBAL_SHARD_ID, "*")


class TenancyContextTests(unittest.TestCase):
    """The set/clear/shard_context primitives are the public surface for
    switching the multitenant scope. Each test isolates context state via
    setUp/tearDown so a leak in one test doesn't pollute the next."""

    def setUp(self):
        # Start every test from a clean unscoped state — even if a
        # previous test (or an outer harness) left a tenant set.
        unset_current_tenant()

    def tearDown(self):
        unset_current_tenant()

    def test_baseline_is_unscoped(self):
        self.assertIsNone(get_current_tenant())

    def test_set_current_shard_stores_two_element_list(self):
        # Multitenant's IN-filter mode triggers on a list; the list
        # shape — [shard, global] — is what makes the auto-filter
        # produce WHERE shard_id IN (current, '*').
        set_current_shard("shard0")
        tenant = get_current_tenant()
        self.assertEqual(tenant, [Shard("shard0"), Shard("*")])

    def test_set_current_shard_value_extraction(self):
        # get_current_tenant_value reads `.tenant_value` off each item
        # in the list. This is what gets fed to the SQL filter.
        set_current_shard("shard0")
        self.assertEqual(get_current_tenant_value(), ["shard0", "*"])

    def test_clear_shard_context_returns_to_unscoped(self):
        set_current_shard("shard0")
        clear_shard_context()
        self.assertIsNone(get_current_tenant())

    def test_shard_context_switches_inside_block(self):
        set_current_shard("shard0")
        with shard_context("shard1"):
            self.assertEqual(get_current_tenant_value(), ["shard1", "*"])
        self.assertEqual(get_current_tenant_value(), ["shard0", "*"])

    def test_shard_context_none_runs_unscoped(self):
        # Passing None into the context manager is the "act as router"
        # escape — visible to every row inside the block, restored
        # after.
        set_current_shard("shard0")
        with shard_context(None):
            self.assertIsNone(get_current_tenant())
        self.assertEqual(get_current_tenant_value(), ["shard0", "*"])

    def test_shard_context_restores_after_exception(self):
        # `finally` must run on exception so a raised block doesn't
        # leak the inner scope into subsequent code.
        set_current_shard("shard0")
        with self.assertRaises(RuntimeError):
            with shard_context("shard1"):
                self.assertEqual(get_current_tenant_value(), ["shard1", "*"])
                raise RuntimeError("boom")
        self.assertEqual(get_current_tenant_value(), ["shard0", "*"])

    def test_shard_context_restores_from_unscoped_baseline(self):
        # Starting unscoped and entering a context should restore back
        # to unscoped, not leave the entry scope active.
        self.assertIsNone(get_current_tenant())
        with shard_context("shard1"):
            self.assertEqual(get_current_tenant_value(), ["shard1", "*"])
        self.assertIsNone(get_current_tenant())

    def test_shard_context_nests_three_deep(self):
        # Each nested level records its entry context and restores it
        # on exit; verifies the previous-context capture is per-call,
        # not shared.
        set_current_shard("shard0")
        with shard_context("shard1"):
            self.assertEqual(get_current_tenant_value(), ["shard1", "*"])
            with shard_context("shard2"):
                self.assertEqual(get_current_tenant_value(), ["shard2", "*"])
                with shard_context("shard3"):
                    self.assertEqual(get_current_tenant_value(), ["shard3", "*"])
                self.assertEqual(get_current_tenant_value(), ["shard2", "*"])
            self.assertEqual(get_current_tenant_value(), ["shard1", "*"])
        self.assertEqual(get_current_tenant_value(), ["shard0", "*"])

    def test_shard_context_around_unscoped_outer(self):
        # If the outer context is unscoped (router process), entering
        # and exiting a shard_context should leave us unscoped again.
        with shard_context("shard1"):
            self.assertEqual(get_current_tenant_value(), ["shard1", "*"])
        self.assertIsNone(get_current_tenant())


class BootstrapTenantContextTests(unittest.TestCase):
    """Tests the role -> tenant-context decision invoked from apps.ready().

    Uses ``override_settings`` to simulate each deployment role. The
    helper is the single point where the role string is translated into
    a multitenant call, so these tests pin down that contract."""

    def setUp(self):
        unset_current_tenant()

    def tearDown(self):
        unset_current_tenant()

    @override_settings(SHARDS_ROLE="shard", SHARD_ID="shard0")
    def test_shard_role_scopes_to_own_shard_plus_global(self):
        bootstrap_tenant_context()
        self.assertEqual(get_current_tenant_value(), ["shard0", "*"])

    @override_settings(SHARDS_ROLE="router")
    def test_router_role_runs_unscoped(self):
        # Start from a scoped state so we can verify the helper
        # actively clears, not just leaves things alone.
        set_current_shard("shard0")
        self.assertEqual(get_current_tenant_value(), ["shard0", "*"])
        bootstrap_tenant_context()
        self.assertIsNone(get_current_tenant())

    @override_settings(SHARDS_ROLE="monolith")
    def test_monolith_role_leaves_context_untouched(self):
        # Defensive branch: in monolith the library shouldn't be loaded
        # at all, but if a consumer calls the helper directly, it
        # should not modify any existing tenant state.
        set_current_shard("shard0")
        bootstrap_tenant_context()
        self.assertEqual(get_current_tenant_value(), ["shard0", "*"])

    @override_settings(SHARDS_ROLE="shard", SHARD_ID="shard1")
    def test_shard_role_is_idempotent(self):
        # Re-running bootstrap from the same role should produce the
        # same context. apps.ready() runs once per process, but this
        # guards against double-load scenarios (dev reload, tests).
        bootstrap_tenant_context()
        first = get_current_tenant_value()
        bootstrap_tenant_context()
        second = get_current_tenant_value()
        self.assertEqual(first, second)
        self.assertEqual(first, ["shard1", "*"])


class InstallTenancyShapeTests(unittest.TestCase):
    """Verifies the install function attached the right machinery to
    ObjectDB. These are pure inspection — they don't touch the DB."""

    def test_install_marker_is_set(self):
        # apps.ready() runs the install during test startup; the marker
        # must be present so any subsequent install call is a no-op.
        self.assertTrue(
            getattr(ObjectDB, "_evennia_shards_tenancy_installed", False)
        )

    def test_install_is_idempotent(self):
        # Re-running install must not error or alter the class further.
        # Marker stays set, manager class stays the same.
        before = ObjectDB.objects.__class__
        install_tenancy_on_objectdb()
        self.assertIs(ObjectDB.objects.__class__, before)
        self.assertTrue(ObjectDB._evennia_shards_tenancy_installed)

    def test_tenant_meta_declares_shard_id(self):
        # Multitenant resolves the tenant column by reading TenantMeta.
        # The contract: tenant_field_name maps to the shard_id column.
        self.assertTrue(hasattr(ObjectDB, "TenantMeta"))
        self.assertEqual(ObjectDB.TenantMeta.tenant_field_name, "shard_id")

    def test_tenant_field_property_returns_shard_id(self):
        # The instance-level lookup multitenant uses to figure out the
        # column at filter / stamp time.
        # We can read the property via the class with a dummy access
        # path, but checking on a transient instance is more honest.
        obj = ObjectDB(db_key="probe", db_typeclass_path=TYPECLASS)
        self.assertEqual(obj.tenant_field, "shard_id")

    def test_manager_class_is_patched(self):
        # The install patches ``get_queryset`` and ``bulk_create`` on
        # the existing manager class (rather than swapping in a new
        # subclass — Django's manager dedup makes that approach silently
        # ineffective). Verify the patch marker is in place and the
        # idmapper-aware base class is still in the chain.
        from evennia.utils.idmapper.manager import SharedMemoryManager

        manager_cls = ObjectDB.objects.__class__
        self.assertTrue(
            getattr(manager_cls, "_evennia_shards_tenant_patched", False)
        )
        self.assertIn(SharedMemoryManager, manager_cls.__mro__)


class AutoStampAndFilterTests(BaseEvenniaTestCase):
    """Integration tests: ObjectDB creation under shard context stamps
    the row, queries auto-scope to the current shard, raw-SQL-forged
    foreign rows are invisible. Requires DB — uses BaseEvenniaTestCase.

    These tests run with the process-wide tenant set to ``shard0`` (by
    apps.ready() reading the test settings). They verify the auto-filter
    is doing real work, not just configured."""

    def test_create_auto_stamps_current_shard(self):
        # Auto-stamp on insert: the new row gets shard_id="shard0"
        # (the first item of the current [shard0, *] tenant list) —
        # not "*", and not None.
        obj = ObjectDB.objects.create(
            db_key="auto_stamp_probe",
            db_typeclass_path=TYPECLASS,
        )
        self.assertEqual(obj.shard_id, "shard0")

    def test_local_row_visible_via_default_manager(self):
        # Sanity: a freshly-created shard0 row is visible to a shard0
        # query. If the filter were inverted this would silently fail.
        obj = ObjectDB.objects.create(
            db_key="local_probe",
            db_typeclass_path=TYPECLASS,
        )
        self.assertEqual(
            ObjectDB.objects.filter(pk=obj.pk).count(),
            1,
        )

    def test_global_row_visible_from_shard(self):
        # Rows stamped with the "*" sentinel must be visible from any
        # shard scope — the IN-filter includes "*" alongside the
        # current shard.
        obj = ObjectDB.objects.create(
            db_key="global_probe",
            db_typeclass_path=TYPECLASS,
        )
        _forge_db_shard(obj.pk, "*")
        self.assertEqual(
            ObjectDB.objects.filter(pk=obj.pk).count(),
            1,
        )

    def test_foreign_row_invisible_via_default_manager(self):
        # Forge a row's shard_id to a different shard via raw SQL,
        # then query through the default manager. The auto-filter
        # must exclude it — count is zero, .get raises DoesNotExist.
        obj = ObjectDB.objects.create(
            db_key="foreign_probe",
            db_typeclass_path=TYPECLASS,
        )
        _forge_db_shard(obj.pk, "shard1")

        # The idmapper cache may still hold the instance from before
        # the forge, so flush before re-querying.
        from evennia.utils.idmapper.models import flush_cache

        flush_cache()

        self.assertEqual(
            ObjectDB.objects.filter(pk=obj.pk).count(),
            0,
        )
        with self.assertRaises(ObjectDB.DoesNotExist):
            ObjectDB.objects.get(pk=obj.pk)


class ShardContextReadTests(BaseEvenniaTestCase):
    """Verifies that ``shard_context(...)`` correctly switches the
    auto-filter scope for reads. Test bodies use ``.count()`` /
    ``.exists()`` / ``.values()`` instead of materialising instances
    via ``.get()`` or queryset iteration — those paths go through
    ``from_db``, which the legacy chokepoints in ``isolation.py``
    still guard. Once the chokepoints are removed, these tests can
    be extended to cover instance materialisation under foreign
    context.

    The auto-filter is the single thing all read primitives funnel
    through (P1 in the survey doc), so verifying it via ``.count()``
    establishes that the filter is being applied with the right
    scope. Full materialisation paths inherit the same filter."""

    def _make_row(self, key: str, shard_id: str) -> int:
        # Helper: create a row under the default shard0 context (so
        # auto-stamp + chokepoints behave) and forge its shard_id
        # via raw SQL afterwards. Returns the pk.
        obj = ObjectDB.objects.create(
            db_key=key, db_typeclass_path=TYPECLASS
        )
        if shard_id != "shard0":
            _forge_db_shard(obj.pk, shard_id)
        # Flush the idmapper so subsequent queries don't pick up the
        # cached instance with its original shard_id.
        from evennia.utils.idmapper.models import flush_cache

        flush_cache()
        return obj.pk

    def test_foreign_row_visible_inside_its_own_shard_context(self):
        # A row stamped shard1 is invisible to shard0 by default, but
        # becomes visible when scope switches to shard1 via the
        # context manager.
        pk = self._make_row("inside_own_context", "shard1")
        self.assertEqual(ObjectDB.objects.filter(pk=pk).count(), 0)
        with shard_context("shard1"):
            self.assertEqual(ObjectDB.objects.filter(pk=pk).count(), 1)

    def test_local_row_invisible_inside_foreign_shard_context(self):
        # The inverse: a shard0 row is normally visible, but inside
        # shard_context("shard1") falls out of scope.
        pk = self._make_row("local_in_foreign_context", "shard0")
        self.assertEqual(ObjectDB.objects.filter(pk=pk).count(), 1)
        with shard_context("shard1"):
            self.assertEqual(ObjectDB.objects.filter(pk=pk).count(), 0)

    def test_global_row_visible_in_any_shard_context(self):
        # The "*" sentinel is the OR-arm of every shard's IN-filter,
        # so a "*"-stamped row must be visible from every scope.
        pk = self._make_row("global_in_any_context", "*")
        self.assertEqual(ObjectDB.objects.filter(pk=pk).count(), 1)
        with shard_context("shard1"):
            self.assertEqual(ObjectDB.objects.filter(pk=pk).count(), 1)
        with shard_context("shard2"):
            self.assertEqual(ObjectDB.objects.filter(pk=pk).count(), 1)

    def test_unscoped_context_sees_all_shards(self):
        # shard_context(None) is the "act as router" escape. Without a
        # tenant set, no auto-filter is injected — every row is in
        # scope regardless of shard_id.
        shard0_pk = self._make_row("unscoped_shard0_probe", "shard0")
        shard1_pk = self._make_row("unscoped_shard1_probe", "shard1")
        global_pk = self._make_row("unscoped_global_probe", "*")

        with shard_context(None):
            self.assertEqual(
                ObjectDB.objects.filter(pk=shard0_pk).count(), 1
            )
            self.assertEqual(
                ObjectDB.objects.filter(pk=shard1_pk).count(), 1
            )
            self.assertEqual(
                ObjectDB.objects.filter(pk=global_pk).count(), 1
            )

    def test_scope_restored_after_context_exit(self):
        # Entering and exiting shard_context must leave the
        # process-wide scope unchanged — otherwise a context switch
        # leaks visibility into subsequent code.
        pk = self._make_row("scope_restoration_probe", "shard0")
        self.assertEqual(ObjectDB.objects.filter(pk=pk).count(), 1)
        with shard_context("shard1"):
            self.assertEqual(ObjectDB.objects.filter(pk=pk).count(), 0)
        # Same query, same row, scope restored.
        self.assertEqual(ObjectDB.objects.filter(pk=pk).count(), 1)

    def test_nested_contexts_filter_each_at_their_own_scope(self):
        # Each level of nesting sees only its own shard + globals;
        # inner exits restore to the immediate outer's scope, not all
        # the way to the process baseline.
        shard0_pk = self._make_row("nested_shard0_probe", "shard0")
        shard1_pk = self._make_row("nested_shard1_probe", "shard1")
        global_pk = self._make_row("nested_global_probe", "*")

        # Outer baseline: shard0.
        self.assertEqual(
            ObjectDB.objects.filter(pk=shard0_pk).count(), 1
        )
        self.assertEqual(
            ObjectDB.objects.filter(pk=shard1_pk).count(), 0
        )
        self.assertEqual(
            ObjectDB.objects.filter(pk=global_pk).count(), 1
        )

        with shard_context("shard1"):
            # shard1 + global visible; shard0 hidden.
            self.assertEqual(
                ObjectDB.objects.filter(pk=shard0_pk).count(), 0
            )
            self.assertEqual(
                ObjectDB.objects.filter(pk=shard1_pk).count(), 1
            )
            self.assertEqual(
                ObjectDB.objects.filter(pk=global_pk).count(), 1
            )

            with shard_context("shard2"):
                # Only globals visible — neither shard0 nor shard1.
                self.assertEqual(
                    ObjectDB.objects.filter(pk=shard0_pk).count(), 0
                )
                self.assertEqual(
                    ObjectDB.objects.filter(pk=shard1_pk).count(), 0
                )
                self.assertEqual(
                    ObjectDB.objects.filter(pk=global_pk).count(), 1
                )

            # Restored to shard1 scope, not shard0.
            self.assertEqual(
                ObjectDB.objects.filter(pk=shard0_pk).count(), 0
            )
            self.assertEqual(
                ObjectDB.objects.filter(pk=shard1_pk).count(), 1
            )

        # Restored to shard0 baseline.
        self.assertEqual(
            ObjectDB.objects.filter(pk=shard0_pk).count(), 1
        )
        self.assertEqual(
            ObjectDB.objects.filter(pk=shard1_pk).count(), 0
        )

    def test_exists_respects_shard_context(self):
        # ``.exists()`` takes a different SQL path than ``.count()``
        # (SELECT 1 LIMIT 1 vs SELECT COUNT(*)) but goes through the
        # same queryset filter. Sanity-check both paths.
        pk = self._make_row("exists_probe", "shard1")
        self.assertFalse(ObjectDB.objects.filter(pk=pk).exists())
        with shard_context("shard1"):
            self.assertTrue(ObjectDB.objects.filter(pk=pk).exists())

    def test_values_respects_shard_context(self):
        # ``.values()`` returns dicts directly without going through
        # from_db, but does flow through the manager's get_queryset
        # and therefore the auto-filter. Same scoping behaviour as
        # full querysets.
        pk = self._make_row("values_probe", "shard1")
        self.assertEqual(
            list(ObjectDB.objects.filter(pk=pk).values("db_key")), []
        )
        with shard_context("shard1"):
            self.assertEqual(
                list(ObjectDB.objects.filter(pk=pk).values("db_key")),
                [{"db_key": "values_probe"}],
            )


class CrossShardWriteTests(BaseEvenniaTestCase):
    """Verifies cross-shard reads (full materialisation) and writes
    (creates that auto-stamp under foreign shard_context). These paths
    were previously blocked by ``isolation.py``'s ``from_db`` and
    ``pre_save`` chokepoints; they pass cleanly under the multitenant
    model where the auto-filter is the single boundary."""

    def test_materialise_foreign_row_inside_its_shard_context(self):
        # Forge a row to shard1 via raw SQL, then materialise it
        # inside shard_context("shard1"). The auto-filter includes
        # the row (scope ["shard1", "*"]), from_db fires, instance
        # constructed — full round-trip via .get().
        obj = ObjectDB.objects.create(
            db_key="materialise_probe",
            db_typeclass_path=TYPECLASS,
        )
        _forge_db_shard(obj.pk, "shard1")
        from evennia.utils.idmapper.models import flush_cache

        flush_cache()

        with shard_context("shard1"):
            row = ObjectDB.objects.get(pk=obj.pk)
            self.assertEqual(row.shard_id, "shard1")
            self.assertEqual(row.db_key, "materialise_probe")

    def test_create_inside_foreign_shard_context_stamps_new_row(self):
        # Inside shard_context("shard1"), creating a new ObjectDB
        # auto-stamps shard_id="shard1" (the first item of the tenant
        # list, not the global sentinel). The row is round-tripped via
        # DB to confirm it actually persisted with the right stamp,
        # not just in-memory.
        with shard_context("shard1"):
            obj = ObjectDB.objects.create(
                db_key="cross_shard_create_probe",
                db_typeclass_path=TYPECLASS,
            )
            self.assertEqual(obj.shard_id, "shard1")
            from evennia.utils.idmapper.models import flush_cache

            flush_cache()
            fresh = ObjectDB.objects.get(pk=obj.pk)
            self.assertEqual(fresh.shard_id, "shard1")
