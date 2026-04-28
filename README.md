# evennia-shards

> Making split deployment a config option in Evennia.

A drop-in extension to [Evennia](https://www.evennia.com/) that adds optional split deployment and horizontal sharding via configuration alone. Install it and the game runs as vanilla Evennia. Flip a config setting and the same code runs as a split deployment (auth process separate from game process). Flip another, and it runs as full multi-shard.

> **Status: pre-PoC.** The repository scaffold exists but library code is not yet written. Not usable for any game yet. See [DESIGN/INDEX.md](DESIGN/INDEX.md) for the design wiki; the original brainstorm at [DESIGN/archive/evennia-shards-HANDOVER.md](DESIGN/archive/evennia-shards-HANDOVER.md#phased-poc-plan) sketches a phased PoC roadmap (archived as historical context, not authoritative).

## What this is

A small, additive enhancement library. Three modes are selected per Evennia process via a single config setting:

- **`monolith`** *(default)* — single process does everything. The library is dormant; you get vanilla Evennia.
- **`router`** — auth front door. Owns `AccountDB`, runs login and the OOC menu, redirects players to a shard on `@ic`.
- **`shard`** — game world. Loads its zones, accepts ticket-based session attaches from the router.

A consumer game's existing room typeclasses can opt in to being shard-boundary points by mixing in `ShardGatewayMixin`. The library does not impose its own `Room` base class.

## What this is *not*

- Not a fork of Evennia. The library imports from upstream Evennia; Evennia stays untouched.
- Not a parallel Evennia distribution.
- Not a "rewrite your stack to scale" project.
- Not a multi-region, multi-database, or multi-datacenter design.
- Not a solution for *"what if we had millions of players"* — that is explicitly deferred.

The design is scoped to the **single-Postgres era**: from one Evennia process today through however many shards run against a single, vertically scaled Postgres. See [the archived handover](DESIGN/archive/evennia-shards-HANDOVER.md#project-identity-and-positioning) for the original positioning statement and out-of-scope list.

## Quick start

> *Library code is not yet written. Once Phase 1 lands, this section will document install + run.*

The intended developer setup, once code exists:

```bash
# Create a virtualenv and install Evennia
python -m venv venv
source venv/Scripts/activate    # on Windows; use venv/bin/activate elsewhere
pip install evennia

# Clone and install evennia-shards in editable mode
git clone https://github.com/<owner>/evennia-shards.git
cd evennia-shards
pip install -e .

# Run the demo game (which lives inside this repo)
cd examples/demo_game
evennia migrate
evennia start
```

For consumer games installing the library as a dependency (rather than developing it), `pip install evennia-shards` will be the eventual install path once the package is published.

## Documentation

All technical documentation lives in [DESIGN/](DESIGN/). Start at [DESIGN/INDEX.md](DESIGN/INDEX.md) for the doc map and reading paths.

Notable entry points:

- **[DESIGN/INDEX.md](DESIGN/INDEX.md)** — map of all design documents.
- **[DESIGN/documentation-structure.md](DESIGN/documentation-structure.md)** — what belongs in CLAUDE.md vs README.md vs DESIGN/, and conventions for new design docs.
- **[DESIGN/archive/evennia-shards-HANDOVER.md](DESIGN/archive/evennia-shards-HANDOVER.md)** — the original brainstorm session that started this project. Archived as historical context; the project's current decisions extend and refine it.
- **[CLAUDE.md](CLAUDE.md)** — instructions for LLM agents working in this repo.

## Project relationships

This library was extracted from scaling work originally done for the [FullCircleMUD (FCM)](https://github.com/) project. FCM will adopt this library as a dependency once functional. The library is deliberately game-agnostic; FCM-specific concerns stay in FCM. See [the origin section of the archived handover](DESIGN/archive/evennia-shards-HANDOVER.md#origin-why-this-is-a-separate-project) for the original rationale.

## License

BSD 3-Clause — see [LICENSE](LICENSE). Same family as Evennia's license.

## Contributing

The project is in an early design-and-scaffold phase and not yet open to outside contributions. Once Phase 1 is complete and the API is exercised by at least one consumer game (FCM), contribution guidelines will be added.
