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
 * approach. See DESIGN/library-integration-risks.md and the PoC branch
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

    Evennia.emitter.on("shard_redirect", function (args, kwargs) {
        var target_url = args[0];
        if (!target_url) {
            console.error("[evennia-shards] shard_redirect: no URL provided");
            return;
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

    // ── Refresh routing ──────────────────────────────────────────────
    //
    // On page load: if this is a browser refresh AND we have a saved
    // target URL from a previous redirect, swap the freshly-opened
    // (default-router) WebSocket to the saved target. The target's
    // csessid auth (priority #1 in onOpen) will re-attach the player
    // to their existing session, preserving state without going
    // through the router's at_post_login → AUTO_PUPPET path.
    //
    // Constructed URL shape mirrors Evennia's webclient default:
    //   wsurl + '?' + csessid + '&' + cuid + '&' + browser
    // The csessid is the Django session key (shared across all
    // sharded processes via the shared-DB backend), so the same
    // value works on the router and any shard.
    //
    // Gated on PerformanceNavigationTiming.type === "reload" so a
    // genuine fresh navigation (typed URL, link click) doesn't
    // unexpectedly route through localStorage — fresh navigations
    // get the normal router-first flow with login form etc. Only
    // explicit reloads attempt refresh routing.
    function get_navigation_type() {
        try {
            var entries = performance.getEntriesByType("navigation");
            return entries.length ? entries[0].type : null;
        } catch (e) {
            return null;
        }
    }

    var saved_base = null;
    try {
        saved_base = localStorage.getItem(LAST_TARGET_KEY);
    } catch (e) {
        // localStorage unavailable; refresh routing skipped.
    }

    if (get_navigation_type() === "reload" && saved_base) {
        var browser_str = window.browserstr || "browser";
        var cuid = window.cuid || "";
        var refresh_url =
            saved_base + "?" + window.csessid + "&" + cuid + "&" + browser_str;

        console.log(
            "[evennia-shards] browser refresh detected; routing to saved " +
                "target: " + saved_base
        );

        // Delay slightly so Evennia's default WS has a chance to
        // establish (Evennia.connection exists) before we swap. The
        // swap reuses the existing shard_redirect handler — emit the
        // event to ourselves with the reconstructed URL.
        setTimeout(function () {
            Evennia.emitter.emit("shard_redirect", [refresh_url], {});
        }, 100);
    }
});
