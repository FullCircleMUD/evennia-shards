# Design Documentation Index

The `docs/` folder is the project's technical wiki. This index is the entry point — every design document is listed below with a one-line description and a suggested reading order for common situations.

If you are an LLM agent picking up work in this repository, also read [`/CLAUDE.md`](../CLAUDE.md) for agent-specific instructions before diving in.

## Conventions

- **Naming.** Index-style files (this one, `CLAUDE.md`, `README.md`) use shouty UPPERCASE. Content design documents use kebab-case (`tenancy.md`, `phase-1-poc-plan.md`). The seed handover keeps its existing name (`evennia-shards-HANDOVER.md`) for traceability.
- **Adding a doc.** Create the file in `docs/` with a descriptive kebab-case filename, then add a one-line entry to the catalogue below in the appropriate section.

## Catalogue

### Meta

Documents about how the documentation itself is organised.

- **[open-questions.md](open-questions.md)** — Tracker for design questions raised in conversation but not yet resolved. Items are removed as they get folded into permanent docs.

### Architecture and design substrate

Long-lived documents capturing the project's intent, architecture, and design rationale.

- **[deployment-topology.md](deployment-topology.md)** — How a consumer game using this library is structured for development and production. The "same code, different config" property and its expression locally (terminals) and in production (platform services). Includes the four-case development phasing, the Unix view-gamedir recipe, and the macOS requirement for a non-Apple SQLite build (Apple's `libsqlite3` runs `sqlite3_initialize()` through libdispatch, which cannot survive `twistd`'s daemonizing fork — `evennia start` hangs silently without it).
- **[library-scope-and-mandates.md](library-scope-and-mandates.md)** — What the library provides, what it leaves to the consumer. The shipped design has no consumer-facing typeclass mandate: cross-shard movement is via the `cross_shard_move` primitive, and any room can be a cross-shard target without special marking.
- **[interoperability.md](interoperability.md)** — this library against every sibling library in `libraries/`: the relationship (optional integration or no coupling) and the considerations or explicit clearance for each. The three recurring constraints — off-thread ORM work, unscoped `ScriptDB`, unscoped router — are stated once at the top.
- **[script-confinement.md](script-confinement.md)** — declaring where a persistent script belongs — one shard (`owning_shard`) or a set of roles (`owning_roles`), never both — and how the library keeps it there. Why Evennia's role-blind boot walk otherwise lets the first process to start attach every marked script in the cluster, why both `_start_task` and `_unpause_task` are guarded, and why returning before touching `_paused_time` is what makes boot order irrelevant.
- **[consumer-constraints.md](consumer-constraints.md)** — What the library demands of consumer games. Hard constraints rooted in the first principle that any game object exists on exactly one shard, including the rule that every object insert must carry a shard or explicitly declare that it deliberately does not.
- **[shard-settings.md](shard-settings.md)** — The two settings (`SHARDS_ROLE`, `SHARD_ID`), how the library reads them, the defaults, and the rule that code reading them must use the `get_role()` / `get_shard_id()` accessors rather than raw `settings.X` reads. Also the row-level `shard_id` column and `"*"` sentinel, and **why global scripts run one instance per process** — `ScriptDB` is not tenant-scoped, so a single row ticks N times across N shards; which scripts that is safe for, and which need consumer-side gating.
- **[logging.md](logging.md)** — The single logging mechanism: every durable record goes through the `shard_log` helper into `shards.log`, co-located with Evennia's own logs. Why one file and one mechanism, the fixed `<timestamp> [LEVEL] <message>` line format, and the two divergences from the sibling shims — `trace=True` for tracebacks, and `security=True` for the redirect audit records that dual-write to Evennia's security log. Also what the library logs, what it deliberately doesn't, and why tests patch `shard_log` in the importing module rather than capturing a logger.
- **[game-time.md](game-time.md)** — Why Evennia's default game clock breaks under sharding, and the one setting that fixes it. `runtime()` accumulates in a per-process module global and is written back to a single `ServerConfig` row every 60 seconds — safe for the one Server process Evennia assumes, a last-write-wins race between several. Processes overwrite each other every minute, never re-read, and game time can move backwards. Covers the wall-clock alternative, what it costs, how to verify it, and the single-writer design for consumers who genuinely need downtime excluded.
- **[cross-shard-message-bus.md](cross-shard-message-bus.md)** — How shards communicate with each other: a Postgres `messages` table polled via a Twisted `LoopingCall`, with `kind`/JSONB extensibility, configurable per-kind timeouts, and a deliberate scope of "real-time inter-process messaging only — not persistent player-facing storage."
- **[tenancy.md](tenancy.md)** — How the library enforces the partition between shards at the Django/Evennia level: tenant-context-driven auto-filtering at the SQL layer via django-multitenant. Documents the installation process (`bootstrap_tenant_context()` + `install_tenancy_on_objectdb()`), the two-element tenant-list trick that gives global `"*"` rows visible from every shard, the cross-shard handoff write path (`qs.update` + in-memory sync), the `refresh_from_db` visibility guard, the load-evict pattern for cross-shard reads, and the `preserve_tenant_context` helper for cross-thread tenant propagation (the canonical integration point for sibling Evennia libraries that dispatch ORM work into worker threads). Also **the unstamped-INSERT guard**: why a `shard_id=NULL` row is refused on both INSERT paths, why the guard is INSERT-only, what it means for the unscoped router and for first boot on a fresh database, and `allow_unstamped_insert()` — the one sanctioned way through, when to reach for it, and when a guard hit means a lost tenant context instead.
- **[shard-aware-search.md](shard-aware-search.md)** — The `shard_aware_global_search` helper: a substitute for `caller.search(name, global_search=True)` that escapes the multitenant auto-filter to find matches on any shard. Returns either a loaded instance (local match) or pk + shard_id metadata (foreign match) so callers can route via cross-shard primitives.
- **[testing-setup.md](testing-setup.md)** — How unit tests are configured to run from the library root without a consumer gamedir: `tests/test_settings.py` + `runtests.py` + `BaseEvenniaTestCase`. Decouples the test suite from `examples/demo_game/`.

### Drafts (under review)

*(none — all drafts have been reviewed and either promoted to the main catalogue or deleted.)*

### Implementation plans

- **[ticket-auth-flow.md](ticket-auth-flow.md)** — The ticket-based authentication flow: how the router creates a ticket, redirects the client to a shard, and how the shard validates the token, authenticates the session, and puppets the character.

### Operational

- **[library-integration-risks.md](library-integration-risks.md)** — Where the library couples to Evennia internals, with each coupling described from two angles: what to diff on Evennia upgrade, and what consumer-side customisation would collide.

### Decisions and refinements

*(none yet)*

Future home of focused decision records as the design refines through implementation. Each refinement should land here as its own document, leaving the handover stable as a historical seed.

### Progress

- **[progress.md](progress.md)** — Running log of high-level milestones. Each entry points to the artefact (test result, design doc, code change) that is the evidence for that milestone.
- **[test-history/](test-history/)** — Captured test results, referenced from `progress.md` as evidence for testing milestones.

### Archive

[`archive/`](archive/) holds documents that have been superseded or are no longer expected to be needed, but are retained for historical reference. Not part of the active catalogue. Move docs here rather than deleting them when they fall out of relevance.

- **[archive/evennia-shards-HANDOVER.md](archive/evennia-shards-HANDOVER.md)** — The original brainstorm session that started this project. Captures the architectural sketch as first imagined: the three-mode design, the ticket-based redirect protocol, the `ShardGatewayMixin` primitive, the cache invariant, a phased PoC plan. The project's current decisions extend and refine this document; treat it as historical context, not as canonical project intent.
- **[archive/shard-isolation.md](archive/shard-isolation.md)** — The earlier four-chokepoint isolation design (`from_db` override, `pre_save` / `pre_delete` signals, `QuerySet.update()` override, `shard_writes_allowed_for` bypass). Kept as historical context for the design choice. Live design at [tenancy.md](tenancy.md).

## Reading paths

Different audiences should walk through the docs in different orders. Pick the path that fits.

### "I am new to the project — orient me."

1. [`/README.md`](../README.md) — what the project is and isn't.
2. [archive/evennia-shards-HANDOVER.md](archive/evennia-shards-HANDOVER.md) — historical brainstorm; useful for the architectural sketch as first imagined. *Not authoritative.* Current decisions extend and refine it.

### "I am an LLM agent picking up an implementation task."

1. [`/CLAUDE.md`](../CLAUDE.md) — agent-facing instructions and load-bearing principles.
2. This file ([INDEX.md](INDEX.md)) — locate the design docs relevant to your task.
3. The relevant architecture or implementation document(s).
4. [archive/evennia-shards-HANDOVER.md](archive/evennia-shards-HANDOVER.md) — for historical context only. Do not treat its decisions as canonical without checking against current settled design.

### "I want to understand the architecture in broad strokes."

1. [`/README.md`](../README.md) — overview and the three modes.
2. The "Core architecture: the three-mode design" and "The split-deployment model" sections of [archive/evennia-shards-HANDOVER.md](archive/evennia-shards-HANDOVER.md) — substantive architectural sketch from the original brainstorm; still a useful overview, with the caveat that specific decisions may have been refined since.

### "I want to know what's been built."

1. [progress.md](progress.md) — running log of milestones with links to evidence (test results, design docs, code changes). The most current view of what's shipped.
2. The architecture and how-it-works docs in this index — `tenancy.md`, `shard-settings.md`, `cross-shard-message-bus.md`, `ticket-auth-flow.md`, `library-integration-risks.md`. These were updated as features landed and reflect current behaviour.
3. [`/examples/`](../examples/) — three demo gamedirs (`demo_router`, `demo_shard0`, `demo_shard1`) that exercise the library end-to-end. See [`/examples/README.md`](../examples/README.md) for the run recipe.

## Originating documents (external)

Two FCM-internal design documents were the conversational inputs to the handover. They contain FCM-specific context and are **not** part of this repository, but are listed here for traceability:

- **`design/SCALING.md`** *(FCM repo)* — Full multi-shard architecture from FCM's perspective.
- **`design/WORLD_DEPLOYMENT.md`** *(FCM repo)* — World build/redeploy/hot-reload pipeline for FCM.

These informed but do not bind this library. FCM-specific content stays in FCM; this library is deliberately game-agnostic.
