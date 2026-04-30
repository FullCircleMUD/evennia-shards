/*
 * evennia-shards: Shard Redirect Plugin
 *
 * Handles the "shard_redirect" OOB command sent by the server when a player
 * needs to be moved to a different instance (router -> shard or shard -> router).
 *
 * The server sends:
 *   ["shard_redirect", ["http://host:port/webclient?ticket=TOKEN"], {}]
 *
 * The plugin navigates the browser to the target URL. The target instance's
 * middleware handles ticket injection into the WebSocket connection.
 */
$(document).ready(function () {
    Evennia.emitter.on("shard_redirect", function (args, kwargs) {
        var target_url = args[0];
        if (!target_url) {
            console.error("[evennia-shards] shard_redirect: no URL provided");
            return;
        }
        console.log("[evennia-shards] Redirecting to: " + target_url);
        window.location.href = target_url;
    });
});
