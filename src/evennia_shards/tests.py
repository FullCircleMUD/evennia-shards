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


# === Test fixtures ===================================================
#
# Minimal stand-ins for Evennia session / account infrastructure. They
# let the cross_shard_move tests assert on session.msg() side-effects
# and account.db._last_puppet writes without spinning up the real
# server. Ported from the legacy test suite — neither structure
# changed under multitenant.


class _FakeSession:
    """Minimal session stand-in for cross_shard_move tests."""

    def __init__(self, address="127.0.0.1"):
        self.address = address
        self.puppet = None
        self.puid = None
        self.oob_messages = {}
        self.protocol_flags = {}

    def msg(self, **kwargs):
        self.oob_messages.update(kwargs)


class _FakeAttributes:
    """Stand-in for AccountDB.attributes."""

    def __init__(self, store=None):
        self._store = dict(store or {})

    def get(self, name, default=None):
        return self._store.get(name, default)


class _FakeAccount:
    """Minimal account stand-in. Exposes ``db._last_puppet`` so the
    redirect helper has somewhere to write its last-puppet stamp."""

    def __init__(self, pk=1):
        self._saved_attrs = {}
        self.id = pk
        self.pk = pk
        self.db = self  # db.X delegates to self.X
        self.attributes = _FakeAttributes()

    @property
    def _last_puppet(self):
        return self._saved_attrs.get("_last_puppet")

    @_last_puppet.setter
    def _last_puppet(self, value):
        self._saved_attrs["_last_puppet"] = value


class _FakeSessionHandler:
    """Stand-in for the per-character SessionHandler. Provides ``.all()``
    so cross_shard_move can iterate puppeting sessions."""

    def __init__(self, sessions=()):
        self._sessions = list(sessions)

    def all(self):
        return list(self._sessions)


@override_settings(
    SHARD_ID="shard0",
    SHARDS_ROLE="shard",
    SHARD_URLS={
        "shard0": "ws://localhost:4011/",
        "shard1": "ws://localhost:4021/",
    },
)
class CrossShardMoveTests(BaseEvenniaTestCase):
    """``cross_shard_move`` end-to-end under multitenant.

    The primitive's external contract is unchanged from the chokepoint
    era — same args, same return type, same side effects — so most
    tests port directly from the legacy suite. The internal mechanism
    swapped ``obj.shard_id = X; obj.save()`` inside a
    ``shard_writes_allowed_for`` bypass for plain
    ``qs.update(shard_id=X, db_location_id=Y)``. The qs.update bypasses
    the tenant-column immutability check on ``__setattr__``, which is
    what makes the cross-shard write possible at all under the new
    model.

    Validation failures raise ``ValueError`` (was
    ``ShardIsolationError``).
    """

    def _make_target_room(self, target_shard="shard1"):
        room = ObjectDB.objects.create(
            db_key="target_room", db_typeclass_path=TYPECLASS
        )
        _forge_db_shard(room.pk, target_shard)
        return room

    def _make_char(self, n_sessions=0):
        """Create a real ObjectDB row + stub a fake session handler onto it.

        Each fake session shares one fake account on its ``.account``
        attribute — the primitive reads ``session.account`` for redirects.
        ``char.__dict__["sessions"] = ...`` shadows the lazy_property
        descriptor on this one instance, bypassing the typeclass's
        normal attribute machinery.
        """
        char = ObjectDB.objects.create(
            db_key="char", db_typeclass_path=TYPECLASS
        )
        fake_account = _FakeAccount(pk=42)
        fake_sessions = []
        for i in range(n_sessions):
            sess = _FakeSession(address=f"10.0.0.{i + 1}")
            sess.account = fake_account
            fake_sessions.append(sess)
        char.__dict__["sessions"] = _FakeSessionHandler(fake_sessions)
        return char, fake_account, fake_sessions

    def _make_item(self, name, location):
        return ObjectDB.objects.create(
            db_key=name, db_typeclass_path=TYPECLASS, db_location=location,
        )

    def _read_row(self, pk, *fields):
        """Read columns from a row regardless of which shard owns it.

        Used by post-move assertions — once a row's been moved to
        another shard, the default auto-filter excludes it from
        queries on this process. ``shard_context(None)`` lifts the
        filter for the read.
        """
        with shard_context(None):
            row = (
                ObjectDB.objects.filter(pk=pk)
                .values_list(*fields)
                .first()
            )
        return row

    # ── happy path ──────────────────────────────────────────────────

    def test_move_no_sessions_succeeds(self):
        from evennia_shards import cross_shard_move
        from evennia_shards.models import Ticket

        char, _, _ = self._make_char(n_sessions=0)
        target = self._make_target_room()

        result = cross_shard_move(char, "shard1", target.pk)

        # Row updated at the DB level. Read unscoped — the auto-filter
        # would exclude shard1 from a shard0 process.
        persisted = self._read_row(char.pk, "shard_id", "db_location_id")
        self.assertEqual(persisted, ("shard1", target.pk))

        self.assertEqual(result.objects_moved, 1)
        self.assertEqual(result.sessions_redirected, 0)
        self.assertEqual(result.failures, [])
        self.assertEqual(Ticket.objects.count(), 0)

    def test_move_with_one_session_redirects(self):
        from evennia_shards import cross_shard_move
        from evennia_shards.models import Ticket

        char, _, sessions = self._make_char(n_sessions=1)
        target = self._make_target_room()

        result = cross_shard_move(char, "shard1", target.pk)

        self.assertEqual(result.sessions_redirected, 1)
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertIn("shard_redirect", sessions[0].oob_messages)
        ticket = Ticket.objects.first()
        self.assertEqual(ticket.to_shard, "shard1")
        self.assertEqual(ticket.character_id, char.pk)

    def test_move_with_multiple_sessions_redirects_each(self):
        from evennia_shards import cross_shard_move
        from evennia_shards.models import Ticket

        char, _, sessions = self._make_char(n_sessions=3)
        target = self._make_target_room()

        result = cross_shard_move(char, "shard1", target.pk)

        self.assertEqual(result.sessions_redirected, 3)
        self.assertEqual(Ticket.objects.count(), 3)
        for sess in sessions:
            self.assertIn("shard_redirect", sess.oob_messages)

    # ── validation failures ─────────────────────────────────────────

    def test_target_shard_not_configured_raises(self):
        from evennia_shards import cross_shard_move
        from evennia_shards.models import Ticket

        char, _, _ = self._make_char(n_sessions=0)
        target = self._make_target_room()

        with self.assertRaises(ValueError) as ctx:
            cross_shard_move(char, "nonexistent_shard", target.pk)
        self.assertIn("nonexistent_shard", str(ctx.exception))

        # No move happened — char still on shard0, visible via default scope.
        persisted = (
            ObjectDB.objects.filter(pk=char.pk)
            .values_list("shard_id", flat=True)
            .first()
        )
        self.assertEqual(persisted, "shard0")
        self.assertEqual(Ticket.objects.count(), 0)

    def test_target_location_does_not_exist_raises(self):
        from evennia_shards import cross_shard_move

        char, _, _ = self._make_char(n_sessions=0)

        with self.assertRaises(ValueError) as ctx:
            cross_shard_move(char, "shard1", 999999)
        self.assertIn("999999", str(ctx.exception))

    def test_target_location_on_wrong_shard_raises(self):
        from evennia_shards import cross_shard_move

        char, _, _ = self._make_char(n_sessions=0)
        # Local room (auto-stamped shard0) used as target while
        # target_shard="shard1" → mismatch.
        local_room = ObjectDB.objects.create(
            db_key="local_room", db_typeclass_path=TYPECLASS
        )

        with self.assertRaises(ValueError) as ctx:
            cross_shard_move(char, "shard1", local_room.pk)
        msg = str(ctx.exception)
        self.assertIn("shard0", msg)
        self.assertIn("shard1", msg)

    # ── atomicity / failure handling ────────────────────────────────

    def test_atomic_rollback_on_eviction_failure(self):
        """If anything inside the atomic block raises, the DB rolls back.

        Reworked from the legacy ``test_atomic_rollback_on_save_failure``
        — the legacy version monkey-patched ``obj.save`` to raise, but
        the multitenant rewrite uses ``qs.update`` (not ``save``), so
        we inject failure at a different point: monkey-patch
        ``obj.flush_from_cache`` to raise. It runs inside the same
        atomic block, after the row update; the rollback should
        undo the update.
        """
        from evennia_shards import cross_shard_move
        from evennia_shards.models import Ticket

        char, _, sessions = self._make_char(n_sessions=1)
        target = self._make_target_room()

        def failing_flush(*args, **kwargs):
            raise RuntimeError("simulated eviction failure")
        object.__setattr__(char, "flush_from_cache", failing_flush)

        with self.assertRaises(RuntimeError):
            cross_shard_move(char, "shard1", target.pk)

        # Atomic rollback: row should still be on shard0 (visible
        # to default scope).
        persisted = self._read_row(char.pk, "shard_id", "db_location_id")
        self.assertEqual(persisted[0], "shard0")
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertNotIn("shard_redirect", sessions[0].oob_messages)

    def test_session_redirect_failure_captured_in_result(self):
        """One session's redirect raises — the move commits, other
        sessions redirect, failure recorded in ``result.failures``."""
        from evennia_shards import cross_shard_move

        char, _, sessions = self._make_char(n_sessions=2)
        target = self._make_target_room()

        def raising_msg(**kwargs):
            raise RuntimeError("simulated network failure")
        sessions[1].msg = raising_msg

        result = cross_shard_move(char, "shard1", target.pk)

        persisted = self._read_row(char.pk, "shard_id")
        self.assertEqual(persisted, ("shard1",))

        self.assertEqual(result.sessions_redirected, 1)
        self.assertEqual(len(result.failures), 1)
        failed_session, failed_exc = result.failures[0]
        self.assertIs(failed_session, sessions[1])
        self.assertIsInstance(failed_exc, RuntimeError)

    # ── inventory recursion ─────────────────────────────────────────

    def test_move_contents_shard_ids_updated(self):
        from evennia_shards import cross_shard_move

        char, _, _ = self._make_char(n_sessions=0)
        item1 = self._make_item("sword", char)
        item2 = self._make_item("shield", char)
        target = self._make_target_room()

        result = cross_shard_move(char, "shard1", target.pk)

        self.assertEqual(result.objects_moved, 3)
        with shard_context(None):
            shards = dict(
                ObjectDB.objects.filter(pk__in=[char.pk, item1.pk, item2.pk])
                .values_list("pk", "shard_id")
            )
        self.assertEqual(shards[char.pk], "shard1")
        self.assertEqual(shards[item1.pk], "shard1")
        self.assertEqual(shards[item2.pk], "shard1")

    def test_move_nested_contents(self):
        """Char → bag → gem: the whole tree moves."""
        from evennia_shards import cross_shard_move

        char, _, _ = self._make_char(n_sessions=0)
        bag = self._make_item("bag", char)
        gem = self._make_item("gem", bag)
        target = self._make_target_room()

        result = cross_shard_move(char, "shard1", target.pk)

        self.assertEqual(result.objects_moved, 3)
        with shard_context(None):
            shards = dict(
                ObjectDB.objects.filter(pk__in=[char.pk, bag.pk, gem.pk])
                .values_list("pk", "shard_id")
            )
        self.assertEqual(shards[char.pk], "shard1")
        self.assertEqual(shards[bag.pk], "shard1")
        self.assertEqual(shards[gem.pk], "shard1")

    def test_move_no_contents(self):
        from evennia_shards import cross_shard_move

        char, _, _ = self._make_char(n_sessions=0)
        target = self._make_target_room()

        result = cross_shard_move(char, "shard1", target.pk)

        self.assertEqual(result.objects_moved, 1)

    def test_move_contents_idmapper_eviction(self):
        from evennia_shards import cross_shard_move

        char, _, _ = self._make_char(n_sessions=0)
        item = self._make_item("sword", char)
        target = self._make_target_room()

        cache = ObjectDB.__dbclass__.__instance_cache__
        self.assertIn(item.pk, cache)

        cross_shard_move(char, "shard1", target.pk)

        self.assertNotIn(char.pk, cache)
        self.assertNotIn(item.pk, cache)

    def test_move_contents_location_unchanged(self):
        """Items' ``db_location_id`` still points at char's pk after move
        (FK target's identity doesn't change across shards)."""
        from evennia_shards import cross_shard_move

        char, _, _ = self._make_char(n_sessions=0)
        item = self._make_item("sword", char)
        target = self._make_target_room()

        cross_shard_move(char, "shard1", target.pk)

        loc = self._read_row(item.pk, "db_location_id")
        self.assertEqual(loc, (char.pk,))

    def test_move_contents_globals_left_alone(self):
        """Items stamped ``shard_id="*"`` (globals) are not re-stamped
        — the explicit ``shard_id=current`` filter in the contents
        update excludes them."""
        from evennia_shards import cross_shard_move

        char, _, _ = self._make_char(n_sessions=0)
        normal_item = self._make_item("sword", char)
        global_item = self._make_item("global_buff", char)
        _forge_db_shard(global_item.pk, "*")
        target = self._make_target_room()

        result = cross_shard_move(char, "shard1", target.pk)

        # Char + normal_item moved; global_item untouched.
        self.assertEqual(result.objects_moved, 2)
        global_shard = (
            ObjectDB.objects.filter(pk=global_item.pk)
            .values_list("shard_id", flat=True)
            .first()
        )
        self.assertEqual(global_shard, "*")

    # ── flush_from_cache bus message ────────────────────────────────

    def test_move_inserts_flush_from_cache_bus_message(self):
        """After a cross-shard move, a ``flush_from_cache`` row is
        queued for the destination shard."""
        from evennia_shards import cross_shard_move
        from evennia_shards.models import Message

        char, _, _ = self._make_char(n_sessions=0)
        target = self._make_target_room()

        Message.objects.filter(kind="flush_from_cache").delete()
        cross_shard_move(char, "shard1", target.pk)

        msgs = list(Message.objects.filter(kind="flush_from_cache"))
        self.assertEqual(len(msgs), 1)
        msg = msgs[0]
        self.assertEqual(msg.to_shard, "shard1")
        self.assertEqual(msg.from_shard, "shard0")
        self.assertEqual(msg.payload, {"pks": [target.pk]})

    def test_same_shard_move_skips_flush_send(self):
        """A move whose target equals the current shard does not send
        a flush_from_cache bus message (the bus refuses same-shard
        sends; the gate skips them upstream)."""
        from evennia_shards import cross_shard_move
        from evennia_shards.models import Message

        char, _, _ = self._make_char(n_sessions=0)
        target = self._make_target_room(target_shard="shard0")

        Message.objects.filter(kind="flush_from_cache").delete()
        cross_shard_move(char, "shard0", target.pk)

        self.assertEqual(
            Message.objects.filter(kind="flush_from_cache").count(), 0,
        )

    def test_flush_send_failure_does_not_roll_back_move(self):
        """A bus failure on the post-move flush_from_cache send is
        logged but doesn't roll back the move itself."""
        from unittest import mock
        from evennia_shards import cross_shard_move

        char, _, _ = self._make_char(n_sessions=0)
        target = self._make_target_room()

        with mock.patch(
            "evennia_shards.messagebus.send_message",
            side_effect=RuntimeError("bus down"),
        ):
            result = cross_shard_move(char, "shard1", target.pk)

        self.assertEqual(result.objects_moved, 1)
        char_shard = self._read_row(char.pk, "shard_id")
        self.assertEqual(char_shard, ("shard1",))

    # ── multitenant-specific gap-fillers ────────────────────────────

    def test_moved_row_invisible_to_source_shard_default_query(self):
        """After the move, the source shard's auto-filter excludes the
        moved row from default queries. This is the multitenant
        equivalent of the old ``from_db`` chokepoint refusal — the
        row is foreign to source, so it just isn't in scope anymore.
        """
        from evennia_shards import cross_shard_move

        char, _, _ = self._make_char(n_sessions=0)
        target = self._make_target_room()
        char_pk = char.pk

        cross_shard_move(char, "shard1", target.pk)

        # From shard0's context (the test process), the auto-filter
        # excludes shard1 rows. The default manager doesn't see it.
        self.assertEqual(
            ObjectDB.objects.filter(pk=char_pk).count(), 0,
        )
        with self.assertRaises(ObjectDB.DoesNotExist):
            ObjectDB.objects.get(pk=char_pk)

    def test_moved_row_visible_under_target_shard_context(self):
        """The moved row is visible when scope switches to the target
        shard's context. Confirms the move actually placed the row on
        target_shard (not just hidden it from source)."""
        from evennia_shards import cross_shard_move

        char, _, _ = self._make_char(n_sessions=0)
        target = self._make_target_room()
        char_pk = char.pk

        cross_shard_move(char, "shard1", target.pk)

        with shard_context("shard1"):
            self.assertEqual(
                ObjectDB.objects.filter(pk=char_pk).count(), 1,
            )
            row = ObjectDB.objects.get(pk=char_pk)
            self.assertEqual(row.shard_id, "shard1")
            self.assertEqual(row.db_location_id, target.pk)

    def test_immutability_check_still_protects_against_save_path(self):
        """Even after cross_shard_move's ``qs.update`` succeeds, the
        ``shard_id``-immutability check on ``__setattr__`` + ``save``
        still refuses ordinary user code that tries to mutate the
        tenant column. The escape hatch (``qs.update``) is deliberately
        narrower than ``save()`` so casual misuse still fails loudly.
        """
        from django.db.utils import NotSupportedError

        local = ObjectDB.objects.create(
            db_key="probe", db_typeclass_path=TYPECLASS,
        )
        # Reset so the new shard_id assignment is treated as a mutation.
        self.assertEqual(local.shard_id, "shard0")

        local.shard_id = "shard1"  # flags _try_update_tenant
        with self.assertRaises(NotSupportedError):
            local.save()
