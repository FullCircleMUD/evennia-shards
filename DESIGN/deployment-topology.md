# Deployment Topology

How a consumer game using this library is structured for development and production. This document captures decisions explicitly discussed. Open questions are flagged with `[TBD]` markers.

## Core property: same code, different config

Every process running the consumer's game runs identical Python code from one source folder (locally) or one git repo (in production). What makes a process behave as a "router" or a "shard0" or a "shard1" is the settings module it loads at startup, which in turn is selected by an environment variable. There is no per-role codebase, no per-role build, no per-role branch.

This is the architectural commitment that makes the library worth building: divergence between roles is expressed as configuration, not as separate engineering artifacts.

## Local development

In split mode, three terminals (one per role) run from the same game folder:

```
Terminal 1:  cd <game folder> && DJANGO_SETTINGS_MODULE=<router settings>  evennia start
Terminal 2:  cd <game folder> && DJANGO_SETTINGS_MODULE=<shard0 settings>  evennia start
Terminal 3:  cd <game folder> && DJANGO_SETTINGS_MODULE=<shard1 settings>  evennia start
```

All three processes:
- Read the same source files from the same directory.
- Share one database (SQLite locally, configured in the base settings).
- Run in parallel for the duration of the dev session — terminals stay open; nothing is switched between roles mid-session.

`DJANGO_SETTINGS_MODULE` scopes to a single command. It tells *that specific* `evennia start` invocation which settings file to read. Each process is locked into its role from startup until the operator stops it.

Monolith mode is just one terminal: `cd <game folder> && evennia start` with the base settings.

`[TBD — needs discussion: the canonical settings file structure and naming. Working examples in the handover use settings_router.py / settings_shard0.py, but whether this is the pattern the library encourages, mandates, or merely demonstrates is unresolved.]`

## Production deployment

The same shape, scaled up. Discussed in the context of Railway as the example platform:

- One git repository for the consumer's game.
- N+1 platform services (one router, N shards), all deployed from the same repository on the same branch.
- Each service has its own environment variable set: `SHARDS_ROLE`, `SHARD_ID` (where applicable), per-service URLs/ports.
- All services share one Postgres instance.
- Multi-shard deployments use the Postgres-backed message bus for cross-shard messaging (see [cross-shard-message-bus.md](cross-shard-message-bus.md)) — no infrastructure beyond the shared Postgres.

Redis was originally considered for the cross-shard message bus (the archived handover and earlier drafts referenced `channels_redis`). It was not adopted: the Postgres-table-polled `LoopingCall` design covers the same ground without adding a runtime dependency.

When a commit is pushed, the platform redeploys all services in parallel from the new commit. Code stays in lockstep across roles by definition — there is no "shard A is on a different commit than shard B" failure mode.

`[TBD — needs discussion: whether the library prescribes specific environment variable names beyond SHARDS_ROLE and SHARD_ID, and what the canonical set is.]`

`[TBD — needs discussion: how each service's public URL is communicated to the others (SHARD_MAP for the router, ROUTER_URL for shards, or a different mechanism).]`

## Mirror property: local and production share shape

| Aspect | Local dev | Production |
|---|---|---|
| Source on disk | One folder | One git repo |
| Running processes | 3 (terminals) | 3 (platform services) |
| What differentiates | `DJANGO_SETTINGS_MODULE` per terminal | Env vars per service |
| Shared database | One SQLite | One Postgres |
| Shared message bus (multi-shard) | Postgres-polled (shared with main DB) | Postgres-polled (no additional infra) |

The shape is identical; the scale differs. Local dev is the same topology as production, not a simplified approximation.

## HTTP webserver topology: one webserver, somewhere

The library assumes the consumer game has **exactly one HTTP webserver** in the deployment, hosting the webclient page, the website, the static-asset pipeline, and the Django admin. By default that webserver lives on the **router** process — turn it on with `WEBSERVER_ENABLED = True` in the router's settings, leave it off (`WEBSERVER_ENABLED = False`) on every shard.

**Shards never serve HTTP.** They exist to host player sessions; the only network ports they need are the WebSocket (now), and telnet/SSH (when those land). Running a full HTTP stack on every shard — reverse-proxy from Portal to Server, Django webclient view, static-asset serving, the AJAX webclient fallback, the `WEB_PLUGINS_MODULE` hook chain — is gratuitous when no browser ever loads a page from the shard.

**External-website case.** A consumer running their website on a separate service entirely (Next.js, static site host, separate Django, whatever) can flip `WEBSERVER_ENABLED = False` on the router as well. The library treats router and shard symmetrically: any process with `WEBSERVER_ENABLED = False` runs WebSocket-only. The library doesn't care where the consumer's website actually lives — only that exactly one place renders the webclient page and serves the static assets that page references.

### A note on Evennia's coupling

Achieving "WebSocket-only" mode required some unpicking on the library's part. Evennia 6.0.0 (and earlier) registers the webclient WebSocket service **inside** the HTTP webserver setup — specifically nested in the loop that builds the reverse-proxy in [`PortalServerFactory.register_webserver`](../../venv/Lib/site-packages/evennia/server/portal/service.py#L177). Setting `WEBSERVER_ENABLED = False` cleanly disables the HTTP stack but takes the WebSocket down with it.

The WebSocket has no architectural reason to be coupled to the HTTP webserver — it's a separate Twisted `TCPServer` on a separate port, listening for an entirely different protocol, sharing no state with the HTTP reverse-proxy, the AJAX webclient, or the Django views. The bundling appears to be incidental to the way `register_webserver` was originally laid out, not a deliberate design choice. (Compare the telnet and SSH protocols, which Evennia registers as top-level services on the Portal factory directly — exactly the level the WebSocket should be at.)

To work around this, the library provides a Portal-services plugin (`evennia_shards/portal_services.py`) that registers the WebSocket independently when `WEBSERVER_ENABLED = False`. The plugin is a no-op when the webserver *is* enabled (Evennia's normal flow registers the WS, doing it twice would EADDRINUSE). See [library-integration-risks.md](library-integration-risks.md#portal-services-plugin) for the full coupling write-up.

## Why one repo, not N

Discussed and agreed: maintaining N repositories for N roles re-introduces the failure mode the architecture exists to prevent — divergent commits across services causing inconsistent behaviour at shard boundaries. One repo means one commit hash describes the system, one CI pipeline gates every deploy, and reverts are atomic.

This is the same reasoning Kubernetes applies to its one-image-many-pods model.

## Library development: the demo gamedirs

The library's repository contains three demo gamedirs under `examples/` (`demo_router`, `demo_shard0`, `demo_shard1`) — one per role. They are the consumer pattern in miniature: real Evennia gamedirs (generated via `evennia --init`) that depend on `evennia_shards` exactly as a real consumer game would, with their game code shared via symlinks (`demo_shard0` is the source of truth; the other two link to its `commands/`, `typeclasses/`, `web/`, `world/`, `server/conf/`). See [`examples/README.md`](../examples/README.md) for the layout and run-three-processes recipe.

Discussed and agreed:

- The demos' purpose is to drive library development (run them to test library changes end-to-end).
- They are **not shipped** as part of the pip package — `[tool.setuptools.packages.find]` includes only `evennia_shards*`, excluding `examples/`, `tests/`, and `DESIGN/`.
- They are **not a starting template** that consumers should clone. Real consumer games live in their own separate repositories.
- Real consumer games depend on the library via `pip install evennia-shards` (once published) or `pip install git+https://github.com/.../evennia-shards.git`. They do not derive from or copy the demos.

## Development phasing (followed during build)

The library was built up across four cases, each a distinct deployment shape exercising a new capability. All four are functionally complete; see [progress.md](progress.md) for the running milestone log with evidence pointers.

1. **Monolith.** Library installed; `SHARDS_ROLE` setting exposed but defaulting to `monolith`; library is dormant; game runs as native Evennia.
2. **Split: router + 1 shard.** Auth/web/OOC on the router; the entire IC world on the shard. Exercises the ticket-based redirect protocol end-to-end.
3a. **Multi-shard navigation.** Two or more shards, each owning part of the IC world. Exercises the cache invariant and cross-shard handoff via the `cross_shard_character_move` primitive (atomic DB writes via the chokepoint bypass, recursive inventory move, idmapper eviction, per-session ticket redirect).
3b. **Multi-shard messaging.** Postgres-backed cross-shard message bus with player-facing delivery primitives (`obj_msg`, `account_msg`) and the `send_cross_shard_message` helper. Specialised consumer-level patterns (cross-shard tells, channel propagation) are deferred to `evennia_shards/contrib/`.

Each case strictly builds on the previous.

Case 1 is qualitatively different from the others: it is a prerequisite gate ("did installing the library break anything?") rather than a feature milestone. It is testable using Evennia's own test suite — running `evennia test evennia` with `evennia_shards` installed should pass exactly as it does without the library. If Evennia's tests fail with the library present, the library is not genuinely transparent in monolith mode and that failure is the first thing to fix. This made Case 1 a strong, automatable verification gate before work on Cases 2/3a/3b began.