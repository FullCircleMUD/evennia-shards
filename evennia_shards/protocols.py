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

    Currently a proof-of-concept: sends a message to the client on
    connect to prove the library can intervene in the connection flow
    without modifying the consumer game.
    """

    def onOpen(self):
        """Called when the WebSocket connection is fully established."""
        super().onOpen()
        # PoC: prove the library can intercept the connection
        self.sendLine(json.dumps(
            ["text", ["[evennia-shards] Protocol override active."], {}]
        ))
