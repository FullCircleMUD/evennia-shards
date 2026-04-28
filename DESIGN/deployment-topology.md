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
- Multi-shard deployments additionally share one Redis instance for cross-shard messaging.

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
| Shared message bus (multi-shard) | None needed | One Redis |

The shape is identical; the scale differs. Local dev is the same topology as production, not a simplified approximation.

## Why one repo, not N

Discussed and agreed: maintaining N repositories for N roles re-introduces the failure mode the architecture exists to prevent — divergent commits across services causing inconsistent behaviour at shard boundaries. One repo means one commit hash describes the system, one CI pipeline gates every deploy, and reverts are atomic.

This is the same reasoning Kubernetes applies to its one-image-many-pods model.

## Library development: the demo game

The library's repository contains a demo game at `examples/demo_game/`. It is the consumer pattern in miniature: a real Evennia game folder, generated with `evennia --init`, that depends on `evennia_shards` exactly as a real consumer game would.

Discussed and agreed:

- The demo's purpose is to drive library development (run it to test library changes).
- It is **not shipped** as part of the pip package — `[tool.setuptools.packages.find]` includes only `evennia_shards*`, excluding `examples/`, `tests/`, and `DESIGN/`.
- It is **not a starting template** that consumers should clone. Real consumer games live in their own separate repositories.
- Real consumer games depend on the library via `pip install evennia-shards` (once published) or `pip install git+https://github.com/.../evennia-shards.git`. They do not derive from or copy the demo.

## Development phasing

The library is built up across four cases, each a distinct deployment shape exercising a new capability:

1. **Monolith.** Library installed; `SHARDS_ROLE` setting exposed but defaulting to `monolith`; library is dormant; game runs as native Evennia.
2. **Split: router + 1 shard.** Auth/web/OOC on the router; the entire IC world on the shard. Exercises the redirect/ticket protocol end-to-end.
3a. **Multi-shard navigation.** Two or more shards, each owning part of the IC world. Exercises the cache invariant and cross-shard handoff via gateway rooms.
3b. **Multi-shard messaging.** Adds Redis-backed cross-shard tells, who, channels.

Each case strictly builds on the previous.

Case 1 is qualitatively different from the others: it is a prerequisite gate ("did installing the library break anything?") rather than a feature milestone. It is testable using Evennia's own test suite — running `evennia test evennia` from inside `examples/demo_game/` with `evennia_shards` installed should pass exactly as it does without the library. If Evennia's tests fail with the library present, the library is not genuinely transparent in monolith mode and that failure is the first thing to fix. This makes Case 1 a strong, automatable verification gate before any work on Cases 2/3a/3b begins.