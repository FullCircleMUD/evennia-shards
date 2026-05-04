# SPDX-License-Identifier: BSD-3-Clause
"""WebSocket protocol override for ticket-based auth.

Dynamically subclasses whatever WEBSOCKET_PROTOCOL_CLASS the consumer
had configured (or Evennia's default) so that any consumer customisations
are preserved. The original class path is stashed by AppConfig.ready()
before it overwrites the setting to point here.

Wired in via WEBSOCKET_PROTOCOL_CLASS in AppConfig.ready().
See DESIGN/ticket-auth-flow.md for the full flow.
"""

import json
from urllib.parse import parse_qs, urlparse

from django.conf import settings
from evennia.utils.utils import class_from_module

from .config import ROLE_ROUTER, ROLE_SHARD, get_role, get_router_url

# Resolve the base class dynamically: whatever was configured before
# our AppConfig.ready() overwrote the setting. Falls back to Evennia's
# default if the stash is missing (defensive).
_BASE_WS_CLASS = class_from_module(
    getattr(
        settings,
        "_SHARDS_ORIGINAL_WS_PROTOCOL",
        "evennia.server.portal.webclient.WebSocketClient",
    )
)

_UPSTREAM_IPS = settings.UPSTREAM_IPS


class ShardWebSocketClient(_BASE_WS_CLASS):
    """WebSocket protocol with ticket-based auth support.

    Subclasses the consumer's configured WebSocket protocol class
    (not Evennia core directly) so any existing customisations are
    preserved.

    Overrides ``onOpen()`` to inject ticket-based authentication between
    ``init_session()`` and ``sessionhandler.connect()``. This is the only
    way to get ``uid`` and ``logged_in`` set before the session state is
    synced to the Server — see DESIGN/library-integration-risks.md for
    the full rationale.
    """

    # -- onOpen override ------------------------------------------------
    # Based on Evennia 6.0.0 WebSocketClient.onOpen().
    # See DESIGN/library-integration-risks.md for what to diff on upgrade.

    def onOpen(self):
        """Called when the WebSocket connection is fully established.

        Reproduced from Evennia 6.0.0 WebSocketClient.onOpen() with
        ticket-based auth layered in.
        See DESIGN/library-integration-risks.md for what to diff on upgrade.

        Auth priority:
        1. Existing browser session (csessid) — re-attach to existing
           session, preserves state across reconnects.
        2. Ticket token in URL — fresh session created via ticket auth,
           validates and consumes the ticket.
        3. No session and no token:
           - Shard: emit ``shard_redirect`` OOB to the router and
             close. Orphan connections (typically stale localStorage
             routing after session expiry) get routed to the router's
             login form via the client's existing handler, rather
             than greeted with a raw connection error.
           - Router: fall through to normal login screen.

        The OOC-return signal that prevents the @ooc → router →
        bounce-back-to-shard loop lives at the account level
        (``account.db._shards_at_ooc_menu``) — set on the router by
        ``_mark_ooc_arrival_if_router`` in priority #2 below (an
        inbound ticket on the router is implicitly an @ooc arrival),
        cleared by ``_redirect_to_character_shard``, read by
        ``shard_aware_at_post_login``. Persistent across session
        lifecycle, so refresh and logout-login both honour the
        player's last expressed intent.
        """
        token = self._extract_ticket_token()

        # ── Reproduced Evennia WebSocketClient.onOpen() ────────────
        # Based on Evennia 6.0.0. See DESIGN/library-integration-risks.md.
        client_address = self._get_client_address()

        self.init_session("websocket", client_address, self.factory.sessionhandler)

        csession = self.get_client_session()  # sets self.csessid
        csessid = self.csessid

        # Auth: browser session first, then ticket, then role gate.
        uid = csession and csession.get("webclient_authenticated_uid", None)
        nonce = csession and csession.get("webclient_authenticated_nonce", 0)
        if uid:
            # Existing browser session — use it, ignore any stale token.
            self.uid = uid
            self.nonce = nonce
            self.logged_in = True

            for old_session in self.sessionhandler.sessions_from_csessid(csessid):
                if (
                    hasattr(old_session, "websocket_close_code")
                    and old_session.websocket_close_code != CLOSE_NORMAL
                ):
                    self.sessid = old_session.sessid
                    self.sessionhandler.disconnect(old_session)

            # OOC-arrival signal. The browser landed back at the router
            # carrying both a csessid cookie (Django session still valid
            # from a recent login) and a ?ticket= in the URL. csessid
            # auth above wins for authentication, but the ticket is
            # what tells us this is an @ooc redirect — IC tickets
            # target shards, not the router, so any ticket arriving at
            # the router is implicitly an @ooc arrival regardless of
            # which auth branch resolves the identity. Stamp the flag.
            if token:
                self._mark_ooc_arrival_if_router(uid)
        elif token:
            # No session — try ticket auth.
            valid, result = self._validate_ticket(token, client_address)
            if not valid:
                self._send_text(result)
                self.sendClose(4001, result)
                return
            self.uid = result["account_id"]
            self.logged_in = True
            self._ticket_character_id = result["character_id"]
            self._mark_ooc_arrival_if_router(result["account_id"])
        else:
            # No session, no token — role-dependent gating.
            role = get_role()
            if role == ROLE_SHARD:
                # Shards aren't entry points — only the router is. A
                # connection arriving here without a session and
                # without a ticket is an orphan: typically a stale
                # localStorage routing attempt (player closed the
                # browser while IC, came back later, sessions timed
                # out, JS routed them to the saved shard URL on
                # refresh). Instead of leaving them with a connection
                # error, redirect them to the router where the login
                # form (or csessid auto-auth, if their browser
                # session survived) puts them back on a working path.
                #
                # Emit a shard_redirect OOB pointing at the router's
                # WS URL with no query params — the client's
                # shard_redirect handler will swap the WebSocket and
                # save the router URL to localStorage, replacing the
                # stale shard URL. Router's onOpen with no csessid in
                # the URL and no ticket falls through to its login-
                # form path (or csessid auth via Django session if
                # still valid).
                try:
                    router_url = get_router_url()
                except (ValueError, AttributeError):
                    # ROUTER_URL not configured — shard is mis-
                    # configured. Fall back to the plain rejection
                    # so the player at least sees an error rather
                    # than nothing.
                    msg = (
                        "[evennia-shards] Connection rejected: shard is "
                        "misconfigured (no ROUTER_URL set)"
                    )
                    self._send_text(msg)
                    self.sendClose(4001, msg)
                    return
                self.sendLine(
                    json.dumps(["shard_redirect", [router_url], {}])
                )
                self.sendClose(1000, "Redirecting to router for login")
                return
            # Router (or any non-shard role): fall through to login screen.

        browserstr = f":{self.browserstr}" if self.browserstr else ""
        self.protocol_flags["CLIENTNAME"] = f"Evennia Webclient (websocket{browserstr})"
        self.protocol_flags["UTF-8"] = True
        self.protocol_flags["OOB"] = True
        self.protocol_flags["TRUECOLOR"] = True
        self.protocol_flags["XTERM256"] = True
        self.protocol_flags["ANSI"] = True

        # Watch for dead links.
        self.transport.setTcpKeepAlive(1)
        # Actually do the connection.
        self.sessionhandler.connect(self)

    # -- Helpers --------------------------------------------------------

    def _get_client_address(self):
        """Resolve the real client IP address, handling proxy headers.

        Uses ``self.transport.client`` (available from autobahn before
        ``onOpen()``) and ``self.http_headers`` for ``x-forwarded-for``
        proxy resolution. Reproduces Evennia 6.0.0 WebSocketClient.onOpen()
        lines 101-111.
        """
        client_address = self.transport.client
        client_address = client_address[0] if client_address else None

        if client_address in _UPSTREAM_IPS and "x-forwarded-for" in self.http_headers:
            addresses = [x.strip() for x in self.http_headers["x-forwarded-for"].split(",")]
            addresses.reverse()

            for addr in addresses:
                if addr not in _UPSTREAM_IPS:
                    client_address = addr
                    break

        return client_address

    def _extract_ticket_token(self):
        """Extract the ticket token from the WebSocket URL query string.

        Returns the token string if ``?ticket=<value>`` is present,
        or None otherwise.
        """
        uri = getattr(self, "http_request_uri", None)
        if not uri:
            return None

        query = parse_qs(urlparse(uri).query)
        tokens = query.get("ticket")
        if tokens:
            return tokens[0]
        return None

    def _validate_ticket(self, token, client_address):
        """Validate a ticket token and consume it on success.

        Returns ``(True, data_dict)`` on success or
        ``(False, error_message)`` on failure. Consumes (deletes) the
        ticket only on success — failed validations leave the ticket
        intact.
        """
        from .tickets import delete_ticket, get_ticket

        found, data = get_ticket(token)
        if not found:
            return False, (
                f"[evennia-shards] Ticket not found or wrong shard: "
                f"token={token}"
            )

        # IP validation: reject if ticket is IP-pinned and the
        # connecting client's IP doesn't match.
        if data["client_ip"]:
            if client_address != data["client_ip"]:
                return False, (
                    f"[evennia-shards] Ticket rejected: IP mismatch "
                    f"(expected {data['client_ip']}, got {client_address})"
                )

        # Consume the ticket (single-use).
        delete_ticket(token)

        return True, data

    def _send_text(self, text):
        """Send a text message to the client via the Evennia webclient protocol."""
        self.sendLine(json.dumps(["text", [text], {}]))

    def _mark_ooc_arrival_if_router(self, account_id):
        """On the router, stamp the account's OOC-menu flag.

        An inbound ticket auth on the router is implicitly an @ooc
        arrival from a shard — ``ShardAwareCmdOOC`` is the only path
        that sends a player session from a shard back to the router.
        IC tickets target a specific shard and are validated by that
        shard, never by the router; the role gate here keeps shard-
        side ticket auths from touching this flag.

        Setting the flag here (router process, same idmapper as the
        eventual read in ``shard_aware_at_post_login``) keeps the
        write/read pair inside one process — no cross-process
        Attribute cache coherency to manage.

        A missing Account is treated as a no-op rather than an error:
        the rest of ``onOpen`` will still proceed with ``uid`` set,
        and ``sessionhandler.connect`` will fail downstream the same
        way it would have without the flag write.
        """
        from evennia.utils import logger

        role = get_role()
        if role != ROLE_ROUTER:
            logger.log_info(
                f"[evennia-shards] _mark_ooc_arrival_if_router: skipping "
                f"(role={role!r}, not ROLE_ROUTER) account_id={account_id}"
            )
            return
        from evennia.accounts.models import AccountDB
        try:
            account = AccountDB.objects.get(pk=account_id)
        except AccountDB.DoesNotExist:
            logger.log_info(
                f"[evennia-shards] _mark_ooc_arrival_if_router: account "
                f"pk={account_id} not found; no-op"
            )
            return
        account.db._shards_at_ooc_menu = True
        logger.log_info(
            f"[evennia-shards] _mark_ooc_arrival_if_router: SET "
            f"_shards_at_ooc_menu=True on account id={account.id} "
            f"key={account.key} (read-back="
            f"{account.db._shards_at_ooc_menu!r})"
        )


# CLOSE_NORMAL is used in the browser-session cleanup path (reproduced
# from Evennia). Import it here so the reference in onOpen works.
from autobahn.twisted.websocket import WebSocketServerProtocol  # noqa: E402

CLOSE_NORMAL = WebSocketServerProtocol.CLOSE_STATUS_CODE_NORMAL
