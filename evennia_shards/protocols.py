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


class ShardWebSocketClient(_BASE_WS_CLASS):
    """WebSocket protocol with ticket-based auth support.

    Subclasses the consumer's configured WebSocket protocol class
    (not Evennia core directly) so any existing customisations are
    preserved.

    On connection, extracts a ``?ticket=<token>`` query parameter from
    the WebSocket URL. If present, looks up the ticket in the database
    and reports the result to the client.
    """

    def onOpen(self):
        """Called when the WebSocket connection is fully established."""
        super().onOpen()

        token = self._extract_ticket_token()
        if token:
            self._handle_ticket(token)

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

    def _handle_ticket(self, token):
        """Validate a ticket and report the result to the client.

        Checks the token against the database, verifies the client IP
        if the ticket has one (token-theft protection), consumes the
        ticket (single-use), and sends the result to the client.
        """
        from .tickets import delete_ticket, get_ticket

        found, data = get_ticket(token)
        if not found:
            self.sendLine(json.dumps(
                ["text", [
                    f"[evennia-shards] Ticket not found or wrong shard: "
                    f"token={token}"
                ], {}]
            ))
            return

        # IP validation: reject if ticket is IP-pinned and the
        # connecting client's IP doesn't match.
        if data["client_ip"]:
            client_ip = getattr(self, "address", None)
            if client_ip != data["client_ip"]:
                self.sendLine(json.dumps(
                    ["text", [
                        f"[evennia-shards] Ticket rejected: IP mismatch "
                        f"(expected {data['client_ip']}, got {client_ip})"
                    ], {}]
                ))
                return

        # Consume the ticket (single-use).
        delete_ticket(token)

        self.sendLine(json.dumps(
            ["text", [
                f"[evennia-shards] Ticket validated: "
                f"account_id={data['account_id']}, "
                f"character_id={data['character_id']}, "
                f"to_shard={data['to_shard']}"
            ], {}]
        ))
