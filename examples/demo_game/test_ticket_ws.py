#!/usr/bin/env python3
"""WebSocket test client for the ticket-auth spike.

Connects to the Evennia WebSocket with a ticket token and prints whatever
the server sends back, then disconnects.

Usage:
    python test_ticket_ws.py <token> [ws_url]

    token   - the ticket token (required)
    ws_url  - WebSocket URL (default: ws://localhost:4002/websocket)

Examples:
    python test_ticket_ws.py abc123def456
    python test_ticket_ws.py abc123def456 ws://localhost:4002/websocket

TEMPORARY — added for the ticket-auth spike. Delete after.
"""

import json
import sys

from autobahn.twisted.websocket import WebSocketClientFactory, WebSocketClientProtocol
from twisted.internet import reactor


class TicketTestProtocol(WebSocketClientProtocol):
    """Connect, receive messages, print them, disconnect."""

    def onOpen(self):
        print(f"[connected] {self.factory.url}")
        # Give the server a moment to send its response, then disconnect.
        reactor.callLater(3, self._done)

    def onMessage(self, payload, isBinary):
        if isBinary:
            print(f"[binary] {payload!r}")
            return

        text = payload.decode("utf-8", errors="replace")
        # Try to parse as Evennia's JSON protocol: ["text", [...], {}]
        try:
            msg = json.loads(text)
            if isinstance(msg, list) and len(msg) >= 2 and msg[0] == "text":
                for line in msg[1]:
                    print(f"[server] {line}")
                return
        except (json.JSONDecodeError, TypeError):
            pass

        print(f"[raw] {text}")

    def onClose(self, wasClean, code, reason):
        print(f"[closed] clean={wasClean} code={code} reason={reason}")
        if reactor.running:
            reactor.stop()

    def _done(self):
        print("[timeout] disconnecting after 3s")
        self.sendClose()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    token = sys.argv[1]
    base_url = sys.argv[2] if len(sys.argv) > 2 else "ws://localhost:4002/websocket"
    url = f"{base_url}?ticket={token}"

    print(f"[connecting] {url}")
    factory = WebSocketClientFactory(url)
    factory.protocol = TicketTestProtocol

    reactor.connectTCP("localhost", 4002, factory)
    reactor.run()


if __name__ == "__main__":
    main()
