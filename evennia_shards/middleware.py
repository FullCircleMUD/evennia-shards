# SPDX-License-Identifier: BSD-3-Clause
"""Django middleware for shard redirect and ticket injection.

Intercepts HTML responses from the webclient and injects:
1. The shard_redirect.js plugin (OOB redirect handler) — always.
2. An inline script appending &ticket=TOKEN to window.csessid — only
   when ?ticket= is present in the page URL.

This avoids requiring consumers to edit templates or manually add
script tags — the library handles it automatically when added to
INSTALLED_APPS (AppConfig.ready() injects this middleware).
"""

from django.utils.deprecation import MiddlewareMixin


class ShardRedirectScriptMiddleware(MiddlewareMixin):
    """Inject the shard redirect plugin and ticket token into webclient pages."""

    _REDIRECT_SCRIPT = (
        b'<script src="/static/evennia_shards/js/shard_redirect.js"'
        b' type="text/javascript"></script>'
    )

    def process_response(self, request, response):
        # Only inject into webclient HTML pages.
        content_type = response.get("Content-Type", "")
        if "text/html" not in content_type:
            return response
        if "/webclient" not in request.path:
            return response

        injection = b""

        # Always inject the redirect plugin (idempotent).
        if self._REDIRECT_SCRIPT not in response.content:
            injection += self._REDIRECT_SCRIPT + b"\n"

        # If ticket token in page URL, inject it into window.csessid
        # so it flows into the WebSocket URL as &ticket=TOKEN.
        # Runs before $(document).ready() → before Evennia.init().
        ticket = request.GET.get("ticket")
        if ticket:
            safe_ticket = ticket.replace("\\", "\\\\").replace("'", "\\'")
            injection += (
                b"<script>window.csessid += '&ticket="
                + safe_ticket.encode()
                + b"';</script>\n"
            )

        if injection:
            response.content = response.content.replace(
                b"</body>",
                injection + b"</body>",
            )
            if response.get("Content-Length"):
                response["Content-Length"] = len(response.content)

        return response
