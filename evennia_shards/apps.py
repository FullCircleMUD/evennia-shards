"""Django AppConfig for evennia-shards.

Only loaded when the consumer adds `evennia_shards` to `INSTALLED_APPS`,
which by convention they only do in non-monolith roles.
"""

from django.apps import AppConfig


class EvenniaShardsConfig(AppConfig):
    name = "evennia_shards"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from .config import ROLE_MONOLITH, ROLE_ROUTER, ROLE_SHARD, get_role

        if get_role() != ROLE_MONOLITH:
            from django.conf import settings

            # Override the WebSocket protocol class so the library can
            # intercept incoming connections for ticket-based auth.
            # Stashes the consumer's current value so protocols.py can
            # subclass it (preserving any consumer customisations).
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

            # Replace CmdIC with shard-aware version that redirects on
            # routers and blocks on shards. The AccountCmdSet references
            # account.CmdIC via module attribute, so patching the module
            # makes the cmdset pick up our version on rebuild.
            from evennia.commands.default import account as _account_module

            from .commands import ShardAwareCmdIC

            _account_module.CmdIC = ShardAwareCmdIC

            if get_role() == ROLE_SHARD:
                from .commands import ShardAwareCmdOOC

                _account_module.CmdOOC = ShardAwareCmdOOC

            # Replace DefaultAccount.at_post_login with role-specific
            # overrides.
            #
            # Router: full replacement that intercepts auto-puppet and
            #   converts it to a ticket+redirect to the character's
            #   owning shard. See DESIGN/library-integration-risks.md.
            #
            # Shard: thin wrapper around Evennia's original that flushes
            #   stale idmapper/Attribute-cache entries for _last_puppet
            #   before auto-puppet. Needed because another process's
            #   cross_shard_character_move may have updated the character's
            #   shard_id in the DB while this process's Account Attribute
            #   cache still holds the old Python object.
            from evennia.accounts.accounts import DefaultAccount

            if get_role() == ROLE_ROUTER:
                from .hooks import shard_aware_at_post_login

                DefaultAccount.at_post_login = shard_aware_at_post_login

            elif get_role() == ROLE_SHARD:
                from .hooks import make_shard_at_post_login

                DefaultAccount.at_post_login = make_shard_at_post_login(
                    DefaultAccount.at_post_login
                )

            # Auto-install permanent library admin commands into
            # CharacterCmdSet. We can't import cmdset_character here:
            # its transitive imports (building → prototypes/menus →
            # evmenu) reference ``evennia.Command``, which is still
            # None until ``evennia._init()`` runs. Real-runtime
            # entry points (server.py, portal.py) call ``_init()``
            # AFTER ``django.setup()``, so our ``ready()`` is too
            # early. Wrap ``evennia._init`` instead — when it runs,
            # the lazy exports are populated and the cmdset_character
            # import chain works. See DESIGN/library-integration-risks.md.
            import evennia

            if not getattr(evennia, "_evennia_shards_init_wrapped", False):
                _original_init = evennia._init

                def _shards_wrapped_init(*args, **kwargs):
                    _original_init(*args, **kwargs)
                    from evennia.commands.default.cmdset_character import (
                        CharacterCmdSet,
                    )

                    if getattr(
                        CharacterCmdSet,
                        "_evennia_shards_cmdset_patched",
                        False,
                    ):
                        return
                    _original_at_cmdset_creation = (
                        CharacterCmdSet.at_cmdset_creation
                    )

                    def _shard_aware_at_cmdset_creation(self):
                        _original_at_cmdset_creation(self)
                        from .commands import (
                            CmdCrossShardDig,
                            CmdShardCheck,
                        )

                        self.add(CmdShardCheck())
                        self.add(CmdCrossShardDig())

                    CharacterCmdSet.at_cmdset_creation = (
                        _shard_aware_at_cmdset_creation
                    )
                    CharacterCmdSet._evennia_shards_cmdset_patched = True

                evennia._init = _shards_wrapped_init
                evennia._evennia_shards_init_wrapped = True

        # Install the shard isolation chokepoints + bypass machinery.
        # See evennia_shards/isolation.py and DESIGN/shard-isolation.md.
        from .isolation import install_chokepoints

        install_chokepoints()
