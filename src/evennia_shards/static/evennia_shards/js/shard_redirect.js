/*
 * evennia-shards: Shard Redirect Plugin (WebSocket-level redirect, PoC)
 *
 * Handles the "shard_redirect" OOB command sent by the server when a player
 * needs to be moved to a different process (router -> shard, shard -> router,
 * or shard -> shard).
 *
 * The server sends:
 *   ["shard_redirect", ["ws://host:port/?ticket=TOKEN"], {}]
 *
 * The plugin closes the current WebSocket and opens a new one to the target
 * URL. The destination process's onOpen handler validates the ticket and
 * auto-logs-in the session. The page itself is NOT reloaded — UI state,
 * scrollback, plugins, all persist across the cross-process transition.
 *
 * This is a deliberate departure from the earlier full-page-navigation
 * approach. See docs/library-integration-risks.md and the PoC branch
 * for context.
 */
$(document).ready(function () {
    // localStorage key for "where the player's session actually lives".
    // Updated on every shard_redirect, read on page load to route
    // refreshes directly to the right target instead of bouncing
    // through the router.
    var LAST_TARGET_KEY = "evennia_shards_last_target";

    // Track whether the next connection_close event is part of a
    // deliberate cross-shard transfer (vs. a real disconnect). Set
    // true just before we close the old WebSocket; consumed by the
    // emit wrapper below on the first connection_close after.
    var deliberate_transfer = false;
    var deliberate_transfer_timeout = null;

    // Wrap Evennia.emitter.emit so that connection_close events
    // arising from a deliberate cross-shard transfer are swallowed
    // silently — without this, webclient_gui's onConnectionClose
    // prints "The connection was closed or lost." on every
    // transition, which is misleading (the connection wasn't lost,
    // it was deliberately swapped). Plugins' onConnectionClose
    // hooks are also intentionally skipped during deliberate
    // transfers — the page is still alive, just on a different
    // connection, so close-time cleanup shouldn't run.
    var orig_emit = Evennia.emitter.emit;
    Evennia.emitter.emit = function (cmdname, args, kwargs) {
        if (cmdname === "connection_close" && deliberate_transfer) {
            deliberate_transfer = false;
            if (deliberate_transfer_timeout) {
                clearTimeout(deliberate_transfer_timeout);
                deliberate_transfer_timeout = null;
            }
            console.log(
                "[evennia-shards] suppressing connection_close " +
                    "(deliberate cross-shard transfer)"
            );
            return;
        }
        return orig_emit.apply(this, arguments);
    };

    // Strip the query string from a WS URL, returning just the
    // base endpoint (scheme://host:port/path). Used to persist the
    // "current target" without the single-use ticket — on refresh
    // we'll reconstruct the URL with csessid params for normal
    // re-attach auth.
    function strip_query(url) {
        var qmark = url.indexOf("?");
        return qmark >= 0 ? url.substring(0, qmark) : url;
    }

    // Ensure the WS URL carries the browser's csessid/cuid/browserstr
    // as Evennia's positional query args, even when the server emitted
    // a ticket-only URL like `?ticket=XXX`. Without this, Evennia's
    // csessid extraction takes the first &-separated chunk after `?`
    // as csessid — for `?ticket=XXX` that's the literal string
    // `'ticket=XXX'` — and the destination's csession lookup fails
    // (no Django session has that key). Result: priority #1 csessid
    // auth never fires on the destination, and the ticket-auth path
    // is the only thing that works on first arrival. On refresh
    // (where there's no fresh ticket), csessid auth has nothing to
    // attach to and the player is bounced to the router.
    //
    // Augmenting here means the destination's onOpen sees a real
    // csessid (the Django session key, set by the webclient template
    // from `request.session.session_key`) AND, on first arrival,
    // also a ticket. Priority #1 wins — Django auth attaches the
    // session — and the ticket sits unconsumed in the DB until its
    // TTL expires (acceptable; single-use anyway). On refresh the
    // URL is csessid-only and priority #1 still wins.
    //
    // Idempotent: if the URL already has a positional first chunk
    // (no `=`), assume csessid is already present and return as-is.
    // The refresh-routing path below builds URLs that already start
    // with csessid, so calling this on them is a no-op.
    function ensure_csessid_in_url(url) {
        if (!window.csessid) {
            return url;
        }
        var qmark = url.indexOf("?");
        var base = qmark >= 0 ? url.substring(0, qmark) : url;
        var query = qmark >= 0 ? url.substring(qmark + 1) : "";
        if (query) {
            var first_chunk = query.split("&")[0];
            if (first_chunk.length > 0 && first_chunk.indexOf("=") < 0) {
                // Already has positional csessid as first chunk.
                return url;
            }
        }
        var prefix =
            window.csessid +
            "&" +
            (window.cuid || "") +
            "&" +
            (window.browserstr || "browser");
        var combined = query ? prefix + "&" + query : prefix;
        return base + "?" + combined;
    }

    Evennia.emitter.on("shard_redirect", function (args, kwargs) {
        var raw_url = args[0];
        if (!raw_url) {
            console.error("[evennia-shards] shard_redirect: no URL provided");
            return;
        }
        var target_url = ensure_csessid_in_url(raw_url);
        if (target_url !== raw_url) {
            console.log(
                "[evennia-shards] augmented WS URL with csessid: " +
                    raw_url +
                    " → " +
                    target_url
            );
        }
        console.log("[evennia-shards] WS-level redirect to: " + target_url);

        // Persist the base endpoint for refresh-routing. We store the
        // URL without the ticket query string — on refresh the saved
        // value is reconstructed with csessid params (csessid auth
        // re-attaches to the existing session; the ticket itself is
        // single-use and would be invalid by then anyway).
        try {
            localStorage.setItem(LAST_TARGET_KEY, strip_query(target_url));
        } catch (e) {
            // localStorage may be unavailable (private browsing,
            // disabled). Refresh routing will fall back to the
            // router-default behaviour; the redirect itself still
            // works.
            console.warn("[evennia-shards] localStorage unavailable:", e);
        }

        // Mark this transfer as deliberate so the imminent
        // connection_close event (from the old socket's onclose) is
        // suppressed. Safety timeout clears the flag after 5s in
        // case the old socket never fires its close event — without
        // this, a stuck flag could accidentally swallow a real
        // disconnect that happens later.
        deliberate_transfer = true;
        deliberate_transfer_timeout = setTimeout(function () {
            deliberate_transfer = false;
            deliberate_transfer_timeout = null;
        }, 5000);

        // Close the current connection.
        var old_conn = Evennia.connection;
        try {
            if (old_conn && typeof old_conn.close === "function") {
                old_conn.close();
            }
        } catch (e) {
            console.warn("[evennia-shards] error closing old connection:", e);
        }

        // Build a fresh WebSocket-backed connection pointed at the new URL.
        // Mirrors Evennia's own WebsocketConnection contract
        // ({connect, msg, close, isOpen}) so that Evennia.connect(),
        // Evennia.msg(), Evennia.isConnected() continue to work.
        var new_ws = new WebSocket(target_url);
        var open = false;
        var ever_open = false;

        new_ws.onopen = function (event) {
            open = true;
            ever_open = true;
            console.log("[evennia-shards] new WS connection open");
            Evennia.emit("connection_open", ["websocket"], event);
        };

        new_ws.onclose = function (event) {
            if (ever_open) {
                Evennia.emit("connection_close", ["websocket"], event);
            }
            open = false;
        };

        new_ws.onerror = function (event) {
            console.error("[evennia-shards] new WS error:", event);
            if (new_ws.readyState === WebSocket.CLOSED) {
                if (ever_open) {
                    Evennia.emit("connection_error", ["websocket"], event);
                }
                open = false;
            }
        };

        new_ws.onmessage = function (event) {
            var data = event.data;
            if (typeof data !== "string" && data.length < 0) {
                return;
            }
            data = JSON.parse(data);
            // Incoming form: [cmdname, args, kwargs]
            Evennia.emit(data[0], data[1], data[2]);
        };

        Evennia.connection = {
            connect: function () {
                // No-op: the new socket is already opening above.
            },
            msg: function (data) {
                if (open) {
                    new_ws.send(JSON.stringify(data));
                } else {
                    console.warn(
                        "[evennia-shards] msg dropped — WS not yet open:",
                        data,
                    );
                }
            },
            close: function () {
                if (open) {
                    new_ws.send(
                        JSON.stringify(["websocket_close", [], {}]),
                    );
                    open = false;
                }
            },
            isOpen: function () {
                return open;
            },
        };
    });

    // Refresh routing lives elsewhere.
    //
    // On a browser refresh, window.wsurl is overridden in an inline
    // script tag injected by ShardRedirectScriptMiddleware just
    // before the evennia.js <script> tag, so it runs before evennia.js
    // loads and Evennia.init opens the default WS. This file is
    // injected at the end of <body> and runs much later — too late to
    // override window.wsurl. See evennia_shards/middleware.py for the
    // override.
});
