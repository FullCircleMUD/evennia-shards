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

            # Replace DefaultAccount.at_post_login on routers with the
            # shard-aware override that intercepts auto-puppet and
            # converts it to a ticket+redirect to the character's
            # owning shard. Router-only: monolith uses vanilla Evennia,
            # and shards rely on Evennia's default auto-puppet path
            # running after ticket-auth has populated _last_puppet.
            # See DESIGN/library-integration-risks.md for upgrade and
            # consumer-override risks.
            if get_role() == ROLE_ROUTER:
                from evennia.accounts.accounts import DefaultAccount

                from .hooks import shard_aware_at_post_login

                DefaultAccount.at_post_login = shard_aware_at_post_login

        # Install the shard isolation chokepoints + bypass machinery.
        # See evennia_shards/isolation.py and DESIGN/shard-isolation.md.
        from .isolation import install_chokepoints

        install_chokepoints()
