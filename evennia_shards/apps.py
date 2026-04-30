"""Django AppConfig for evennia-shards.

Only loaded when the consumer adds `evennia_shards` to `INSTALLED_APPS`,
which by convention they only do in non-monolith roles.
"""

from django.apps import AppConfig


class EvenniaShardsConfig(AppConfig):
    name = "evennia_shards"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Override the WebSocket protocol class so the library can
        # intercept incoming connections for ticket-based auth.
        # Gated on non-monolith: monolith uses normal login only.
        # Stashes the consumer's current value so protocols.py can
        # subclass it (preserving any consumer customisations).
        from .config import get_role

        if get_role() != "monolith":
            from django.conf import settings

            settings._SHARDS_ORIGINAL_WS_PROTOCOL = getattr(
                settings,
                "WEBSOCKET_PROTOCOL_CLASS",
                "evennia.server.portal.webclient.WebSocketClient",
            )
            settings.WEBSOCKET_PROTOCOL_CLASS = (
                "evennia_shards.protocols.ShardWebSocketClient"
            )

            # Inject the shard redirect JS middleware so the webclient
            # gets the redirect plugin without any template edits.
            _middleware_path = (
                "evennia_shards.middleware.ShardRedirectScriptMiddleware"
            )
            if _middleware_path not in settings.MIDDLEWARE:
                settings.MIDDLEWARE = list(settings.MIDDLEWARE) + [
                    _middleware_path
                ]

        # Late-bind shard_id onto Evennia's ObjectDB so the ORM is aware
        # of the column the migration adds. Idempotent — guards against
        # double-installation in dev reload scenarios.
        from django.db import models
        from django.db.models.signals import pre_delete, pre_save
        from evennia.objects.models import ObjectDB

        if not any(f.name == "shard_id" for f in ObjectDB._meta.get_fields()):
            ObjectDB.add_to_class(
                "shard_id",
                models.CharField(max_length=64, null=True, blank=True, db_index=True),
            )

        # pre_save chokepoint: combines auto-stamp (set shard_id to the
        # current shard for new rows where shard_id is None) and write
        # protection (raise ShardIsolationError if a save would persist
        # to a row owned by another shard). The "*" sentinel denotes
        # rows owned by all shards and is allowed.
        #
        # Connected without a sender filter because Evennia's typeclass
        # system uses concrete Django subclasses of ObjectDB (Room,
        # Character, Exit, consumer-defined typeclasses) that share the
        # ObjectDB table. Django dispatches pre_save with `sender =
        # type(instance)`, which is the subclass — so a sender=ObjectDB
        # filter never matches the saves we care about. The handler does
        # the isinstance check in-line.
        pre_save.connect(
            _pre_save_chokepoint,
            dispatch_uid="evennia_shards.pre_save_chokepoint",
        )

        # pre_delete chokepoint: refuse to delete a row owned by another
        # shard. Covers both instance.delete() and qs.delete() — Django
        # fires pre_delete per affected row even on bulk queryset deletes
        # (it has to, for cascade handling). Connected without a sender
        # filter for the same reason as pre_save.
        pre_delete.connect(
            _pre_delete_chokepoint,
            dispatch_uid="evennia_shards.pre_delete_chokepoint",
        )

        # from_db chokepoint: refuse to construct an instance from a row
        # whose shard_id is owned by another shard. Covers all read paths
        # that produce a Model instance — queryset iteration, raw() queries,
        # and select_related (the three Django call sites that go through
        # Model.from_db). Inherited automatically by typeclass subclasses
        # (Room, Character, ...) since they look up from_db via Python's
        # MRO and ObjectDB is the patched class.
        if not getattr(ObjectDB, "_evennia_shards_from_db_patched", False):
            original_from_db = ObjectDB.from_db.__func__

            def _shard_aware_from_db(cls, db, field_names, values):
                field_names_list = list(field_names)
                if "shard_id" in field_names_list:
                    idx = field_names_list.index("shard_id")
                    row_shard = values[idx]
                    if row_shard is not None and row_shard != "*":
                        from evennia_shards import get_shard_id
                        from evennia_shards.errors import ShardIsolationError

                        current = get_shard_id()
                        if row_shard != current:
                            raise ShardIsolationError(
                                f"from_db refused: shard {current!r} cannot "
                                f"instantiate {cls.__name__} with shard_id={row_shard!r}"
                            )
                return original_from_db(cls, db, field_names, values)

            ObjectDB.from_db = classmethod(_shard_aware_from_db)
            ObjectDB._evennia_shards_from_db_patched = True

        # qs.update chokepoint: refuse a bulk update if the queryset would
        # touch any row whose shard_id is neither current nor "*". This is
        # the one write operation Django does not fire signals for, so it
        # needs an explicit override on the QuerySet's update() method.
        # Idempotent via a marker on the QuerySet class.
        QuerySetClass = type(ObjectDB.objects.get_queryset())
        if not getattr(QuerySetClass, "_evennia_shards_update_patched", False):
            original_update = QuerySetClass.update

            def _shard_aware_update(self, **kwargs):
                # Only enforce for ObjectDB-derived models in case this
                # queryset class is shared with other Evennia models.
                if not (isinstance(self.model, type) and issubclass(self.model, ObjectDB)):
                    return original_update(self, **kwargs)

                from evennia_shards import get_shard_id
                from evennia_shards.errors import ShardIsolationError

                current = get_shard_id()
                # Find non-owned, non-global, non-NULL shard_ids in the
                # queryset's scope. values_list bypasses from_db (per
                # design — see shard-isolation.md), so this SELECT can
                # read remote rows without raising. Cap at 5 distinct
                # values for the error message.
                foreign = list(
                    self.exclude(shard_id__isnull=True)
                    .exclude(shard_id="*")
                    .exclude(shard_id=current)
                    .values_list("shard_id", flat=True)
                    .distinct()[:5]
                )
                if foreign:
                    raise ShardIsolationError(
                        f"qs.update refused: shard {current!r} would touch "
                        f"{self.model.__name__} rows owned by {sorted(set(foreign))!r}"
                    )
                return original_update(self, **kwargs)

            QuerySetClass.update = _shard_aware_update
            QuerySetClass._evennia_shards_update_patched = True


def _pre_save_chokepoint(sender, instance, **kwargs):
    from evennia.objects.models import ObjectDB

    if not isinstance(instance, ObjectDB):
        return

    from evennia_shards import get_shard_id
    from evennia_shards.errors import ShardIsolationError

    current = get_shard_id()

    if instance.shard_id is None:
        instance.shard_id = current
        return

    if instance.shard_id == current or instance.shard_id == "*":
        return

    raise ShardIsolationError(
        f"pre_save refused: shard {current!r} cannot persist "
        f"{type(instance).__name__} pk={instance.pk!r} owned by shard "
        f"{instance.shard_id!r}"
    )


def _pre_delete_chokepoint(sender, instance, **kwargs):
    from evennia.objects.models import ObjectDB

    if not isinstance(instance, ObjectDB):
        return

    from evennia_shards import get_shard_id
    from evennia_shards.errors import ShardIsolationError

    current = get_shard_id()

    if instance.shard_id is None or instance.shard_id == "*":
        return

    if instance.shard_id == current:
        return

    raise ShardIsolationError(
        f"pre_delete refused: shard {current!r} cannot delete "
        f"{type(instance).__name__} pk={instance.pk!r} owned by shard "
        f"{instance.shard_id!r}"
    )
