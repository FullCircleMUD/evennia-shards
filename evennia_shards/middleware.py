# SPDX-License-Identifier: BSD-3-Clause
"""Django middleware for shard redirect and ticket injection.

Intercepts HTML responses from the webclient and injects:
1. The shard_redirect.js plugin (OOB redirect handler) — always.
2. An inline script appending &ticket=TOKEN to window.csessid — only
   when ?ticket= is present in the page URL.
3. An EARLY inline script BEFORE evennia.js's <script> tag so it
   runs synchronously after window.wsurl is set but before
   Evennia.init opens the default WS. Currently a console.log probe
   to verify load order.

This avoids requiring consumers to edit templates or manually add
script tags — the library handles it automatically when added to
INSTALLED_APPS (AppConfig.ready() injects this middleware).
"""

import re

from django.utils.deprecation import MiddlewareMixin


class ShardRedirectScriptMiddleware(MiddlewareMixin):
    """Inject the shard redirect plugin and ticket token into webclient pages."""

    _REDIRECT_SCRIPT = (
        b'<script src="/static/evennia_shards/js/shard_redirect.js"'
        b' type="text/javascript"></script>'
    )

    # Match the evennia.js <script> tag so we can inject inline JS
    # immediately before it. Tolerates attribute order / whitespace
    # variations the Django template might emit.
    _EVENNIA_JS_TAG = re.compile(
        rb"<script\b[^>]*\bsrc=[^>]*evennia\.js[^>]*>\s*</script>"
    )

    # Refresh-routing override of window.wsurl. Runs synchronously
    # between the inline `var wsurl = ...` block and the evennia.js
    # <script> tag that loads the webclient runtime. By the time
    # Evennia.init reads window.wsurl (~500ms later, inside the
    # WebsocketConnection constructor), our override has already
    # taken effect.
    #
    # If this is a refresh (PerformanceNavigationTiming.type ===
    # "reload") AND localStorage has a saved target URL from a
    # previous shard_redirect, replace window.wsurl with that target.
    # evennia.js then opens its default connection directly to the
    # shard, no router round-trip, no flash through the router OOC
    # menu, no disconnect-clear race.
    #
    # Fresh navigations (typed URL, link click) report navigation
    # type as "navigate" and skip the override → default router-first
    # flow with login form etc.
    _EARLY_OVERRIDE = (
        b"<script>"
        b"(function(){"
        b"try{"
        b"var nav=performance.getEntriesByType('navigation');"
        b"if(!nav.length||nav[0].type!=='reload')return;"
        b"var saved=localStorage.getItem('evennia_shards_last_target');"
        b"if(saved&&window.wsurl!==saved){"
        b"console.log('[evennia-shards] refresh: overrode window.wsurl '+"
        b"window.wsurl+' \\u2192 '+saved);"
        b"window.wsurl=saved;"
        b"}"
        b"}catch(e){"
        b"console.warn('[evennia-shards] refresh override failed:',e);"
        b"}"
        b"})();"
        b"</script>"
    )

    def process_response(self, request, response):
        # Only inject into webclient HTML pages.
        content_type = response.get("Content-Type", "")
        if "text/html" not in content_type:
            return response
        if "/webclient" not in request.path:
            return response

        # Inject the EARLY override right before evennia.js's <script>
        # tag. The inline JS runs synchronously after the template's
        # `var wsurl = ...` block but before evennia.js loads, so the
        # override takes effect before WebsocketConnection reads
        # window.wsurl.
        match = self._EVENNIA_JS_TAG.search(response.content)
        if match and self._EARLY_OVERRIDE not in response.content:
            response.content = (
                response.content[: match.start()]
                + self._EARLY_OVERRIDE
                + b"\n"
                + response.content[match.start():]
            )

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
