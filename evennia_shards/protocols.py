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

from .config import ROLE_SHARD, get_role

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
        1. Existing browser session (csessid) — handles page refresh
           with a stale ?ticket= in the URL without re-consuming it.
        2. Ticket token in URL — fresh connection, validate and consume.
        3. No session and no token:
           - Shard: reject (shards are ticket-only).
           - Router: fall through to normal login screen.
        """
        # Extract ticket token from URL (if any) but don't validate yet.
        # The library-namespaced "arrived with ticket" flag is stored
        # in protocol_flags further down, alongside the other Evennia
        # protocol_flags assignments — that placement is required so
        # the value crosses the Portal→Server AMP sync.
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
        else:
            # No session, no token — role-dependent gating.
            role = get_role()
            if role == ROLE_SHARD:
                msg = "[evennia-shards] Connection rejected: this shard requires a ticket"
                self._send_text(msg)
                self.sendClose(4001, msg)
                return
            # Router (or any non-shard role): fall through to login screen.

        browserstr = f":{self.browserstr}" if self.browserstr else ""
        self.protocol_flags["CLIENTNAME"] = f"Evennia Webclient (websocket{browserstr})"
        self.protocol_flags["UTF-8"] = True
        self.protocol_flags["OOB"] = True
        self.protocol_flags["TRUECOLOR"] = True
        self.protocol_flags["XTERM256"] = True
        self.protocol_flags["ANSI"] = True

        # Library-namespaced flag: True iff this WebSocket connection
        # arrived with ?ticket= present in its URL. Captures URL
        # presence, not validation outcome — so a page refresh on a
        # redirect-target URL (where a stale token won't re-validate
        # but the browser session is reused) still carries the flag
        # forward.
        #
        # This onOpen runs in the Portal of every non-monolith role,
        # so the flag is set on shard sessions and router sessions
        # alike. Currently, the only consumer is the router-side
        # at_post_login override (evennia_shards/hooks.py), which
        # reads it as the OOC-return signal to skip auto-puppet on
        # an inbound redirected session. Other role-specific consumers
        # may read the flag in the future and should interpret it in
        # the context of their role and the prevailing redirect
        # topology — e.g. shard↔shard transfers, if introduced.
        #
        # Stored in protocol_flags (rather than as an attribute on
        # `self`) so the value crosses the Portal→Server AMP sync.
        # Plain attributes on `self` are dropped at the boundary —
        # only entries listed in settings.SESSION_SYNC_ATTRS survive,
        # and protocol_flags is the conventional carrier for this
        # kind of session-level metadata.
        self.protocol_flags["SHARDS_TICKET_AUTHED"] = bool(token)

        # DEBUG (smoke-test aid; remove once verified live).
        # Marker for grep: SHARDS-DEBUG-TICKET-FLAG
        from evennia.utils import logger as _logger
        _logger.log_info(
            f"onOpen: protocol_flags['SHARDS_TICKET_AUTHED'] = "
            f"{self.protocol_flags['SHARDS_TICKET_AUTHED']} "
            f"(token={token!r}, uri={getattr(self, 'http_request_uri', None)!r}, "
            f"id={id(self):#x})"
        )

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


# CLOSE_NORMAL is used in the browser-session cleanup path (reproduced
# from Evennia). Import it here so the reference in onOpen works.
from autobahn.twisted.websocket import WebSocketServerProtocol  # noqa: E402

CLOSE_NORMAL = WebSocketServerProtocol.CLOSE_STATUS_CODE_NORMAL
