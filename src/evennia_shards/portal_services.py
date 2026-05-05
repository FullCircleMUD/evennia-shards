# SPDX-License-Identifier: BSD-3-Clause
"""Portal-side service plugin: standalone WebSocket registration.

Auto-registered into ``settings.PORTAL_SERVICES_PLUGIN_MODULES`` by
``EvenniaShardsConfig.ready()``. Evennia calls every module listed
there during ``PortalServerFactory.privilegedStartService`` (after
``register_webserver``, before ``super().privilegedStartService()``)
and hands each one the Portal service factory.

This plugin solves a coupling problem in Evennia 6.0.0: the webclient
WebSocket service is registered *inside* ``register_webserver()`` —
specifically nested in the loop over ``WEBSERVER_PORTS`` ahead of the
HTTP reverse-proxy setup
(``evennia/server/portal/service.py:200-237``). That makes the
WebSocket conditional on ``settings.WEBSERVER_ENABLED``, which in
turn forces every process hosting WebSocket sessions to also run a
full HTTP webserver — including the Django reverse-proxy, the
website, the static-asset pipeline, the AJAX webclient, and
``WEB_PLUGINS_MODULE`` hook chain. There is no upstream rationale for
that coupling beyond "they happen to run on the same Twisted
factory"; the WebSocket factory is a self-contained
``WebSocketServerFactory(protocol=...).setServiceParent(portal)``
and could just as easily live alongside the telnet/SSH registrations
at the same level.

For sharded deployments where shards exist *only* to host player
sessions, paying the full HTTP-stack cost on every shard is
gratuitous. This plugin extracts the WebSocket registration into
the level where it belongs and runs it independently when
``WEBSERVER_ENABLED=False``.

Behaviour:

- ``WEBSERVER_ENABLED=True`` (router default): Evennia's normal
  ``register_webserver`` runs. This plugin is a no-op — the WS is
  already registered upstream.
- ``WEBSERVER_ENABLED=False`` (shard default; or router with an
  externally-hosted website): Evennia's ``register_webserver`` is
  skipped, the HTTP stack does not start, and this plugin registers
  the WebSocket factory directly on the Portal service.

Mirrors lines 222-237 of Evennia's ``register_webserver`` (the
WebSocket portion). LOCKDOWN_MODE is honoured the same way.
"""

from django.conf import settings
from evennia.utils.utils import class_from_module
from twisted.application import internet


def start_plugin_services(portal_service):
    """Register the webclient WebSocket service on ``portal_service``.

    Called by ``PortalServerFactory.register_plugins`` for every
    module in ``PORTAL_SERVICES_PLUGIN_MODULES``. Receives the
    Portal's service factory; we register a child Twisted service on
    it via ``setServiceParent``.

    Skips registration if either:

    - ``WEBSERVER_ENABLED`` is True — Evennia's own
      ``register_webserver`` already started the WebSocket. Doing it
      again would duplicate the listener and (depending on OS) fail
      with EADDRINUSE.
    - The webclient WebSocket is itself disabled
      (``WEBSOCKET_CLIENT_ENABLED=False``, or the port/interface
      settings are missing). The consumer doesn't want the WebSocket
      at all; respect that.
    """
    if settings.WEBSERVER_ENABLED:
        # Evennia's register_webserver registered the WS already.
        return

    if not (
        settings.WEBSOCKET_CLIENT_ENABLED
        and settings.WEBSOCKET_CLIENT_PORT
        and settings.WEBSOCKET_CLIENT_INTERFACE
    ):
        # Webclient WS deliberately disabled; nothing to do.
        return

    # Defer the autobahn import: stays out of the main import path
    # for monolith and HTTP-only consumers who never reach this branch.
    from autobahn.twisted.websocket import WebSocketServerFactory
    import evennia
    from evennia.server.portal import webclient  # noqa: F401 — module side-effects

    websocket_protocol = class_from_module(settings.WEBSOCKET_PROTOCOL_CLASS)

    # LOCKDOWN_MODE forces 127.0.0.1 — same posture as Evennia's
    # check_lockdown helper. We can't import that helper directly
    # because it's a method on PortalServerFactory; the inline form
    # is short enough.
    interface = (
        "127.0.0.1"
        if settings.LOCKDOWN_MODE
        else settings.WEBSOCKET_CLIENT_INTERFACE
    )
    port = settings.WEBSOCKET_CLIENT_PORT

    class Websocket(WebSocketServerFactory):
        """Subclass purely for nicer logger output."""

        pass

    factory = Websocket()
    factory.noisy = False
    factory.protocol = websocket_protocol
    factory.sessionhandler = evennia.PORTAL_SESSION_HANDLER

    websocket_service = internet.TCPServer(port, factory, interface=interface)
    websocket_service.setName(f"EvenniaWebSocket-{interface}:{port}")
    websocket_service.setServiceParent(portal_service)

    # Annotate the Portal's info_dict so `evennia status` (or whatever
    # uses it) shows the registered WebSocket. The dict is created
    # by PortalServerFactory.__init__; key may already exist if other
    # parts of Evennia populated it.
    if hasattr(portal_service, "info_dict"):
        portal_service.info_dict.setdefault("webclient", []).append(
            f"webclient-websocket-{interface}: {port} (registered by "
            f"evennia-shards; WEBSERVER_ENABLED=False)"
        )
