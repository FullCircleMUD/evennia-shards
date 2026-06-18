# Documentation Structure

This project uses three distinct documentation surfaces. Each has a different audience, lifespan, and update cadence. Putting content in the wrong place is a slow-burning cost: the wrong people read it (or miss it), it goes stale, or it crowds out what the surface is actually for.

This document explains what belongs where, when to update each, and the conventions that keep them coherent.

## The three surfaces

### 1. `/README.md` — for humans landing on the repo

**Audience.** Someone who has just clicked through to the repository on GitHub (or wherever it is hosted). They may be a prospective user, a curious passer-by, or a future contributor. They have no prior context.

**Purpose.** Answer the questions a new visitor asks in the first ninety seconds:

- What is this project?
- Is it for me?
- What is its status — usable, in development, abandoned?
- How do I install or try it?
- Where do I learn more?

**Tone and depth.** Big-picture, marketing-adjacent without being marketing. Plain language. Skim-friendly. Should fit in a single browser tab without scrolling fatigue.

**What does NOT belong here.**

- Detailed architectural diagrams or component-level explanations. Link to `docs/` instead.
- Implementation specifics, internal API documentation, code-level conventions.
- Working notes, decision logs, refinements to the design.
- LLM agent instructions (those go in `CLAUDE.md`).

**Update cadence.** Stable. Edit the README when something user-facing changes: project status, top-level positioning, install steps, supported deployment shapes. Do not edit it for internal architectural refinements — those go in `docs/`.

### 2. `/CLAUDE.md` — for LLM agents working in the repo

**Audience.** Claude (and other LLM coding agents) picking up a task in this repository, with no memory of prior sessions.

**Purpose.** Give an agent the context it needs to make good decisions on this codebase specifically:

- What the project is, in one paragraph.
- Where to read first.
- The load-bearing architectural principles that constrain implementation choices.
- The "out of scope" list at a glance, so the agent does not propose features that have been deliberately excluded.
- Project-specific conventions that diverge from generic best practices.
- Pointers — not duplicated content — to the deeper documentation in `docs/`.

**Tone and depth.** Direct, prescriptive, context-dense. Written for an audience that will read every word and act on it. Bullet points and short imperative sentences over long prose.

**What does NOT belong here.**

- General advice that applies to any Python project.
- Reproduction of content already in `docs/` — link instead.
- User-facing marketing-style framing (that lives in the README).
- Implementation detail. CLAUDE.md should remain stable; let `docs/` churn instead.

**Update cadence.** Stable, but more reactive than the README. Update CLAUDE.md when:

- A new load-bearing principle emerges and needs to be enforced across all sessions.
- A repeated mistake by past sessions reveals missing context that should be hoisted up.
- The repository structure changes and the agent's mental map needs to follow.

Do not let CLAUDE.md grow into a wiki — its value comes from being short enough to be loaded and respected at the start of every session.

### 3. `docs/` — the technical wiki

**Audience.** Anyone working on or thinking about the project's architecture and implementation. Equally useful to humans (the project owner, future contributors) and LLM agents.

**Purpose.** The full technical record. Architecture, decisions, rationale, refinements, implementation plans, post-mortems. The place where *why* lives.

**Structure.**

- **[INDEX.md](INDEX.md)** — entry point. Lists every document with a one-line description, organised by category, with reading paths for common audiences.
- **Content documents** — one focused topic per file. Kebab-case filenames (`phase-1-poc-plan.md`, `cross-shard-messaging.md`).

**What belongs here.**

- Architectural overviews, design substrate documents (e.g. the seed handover).
- Decision records — what was chosen, what was rejected, *why*.
- Implementation plans for upcoming phases.
- Refinements that update or supersede earlier design choices (link forward and backward between documents).
- Diagrams, sequence walkthroughs, protocol specifications.
- Glossaries of project-specific terminology.

**What does NOT belong here.**

- LLM agent instructions (CLAUDE.md).
- User-facing project framing (README.md).
- Throwaway scratch notes — if it isn't worth indexing in INDEX.md, it probably isn't worth keeping.

**Update cadence.** Active. New documents get added as new design topics arise. Existing documents get updated when the design they describe is refined, but historical context is preserved (e.g. the handover stays as the seed substrate even as more focused docs supersede parts of it).

## Quick decision guide

When you have new information to write down, ask:

| Question | Answer ⇒ goes in |
|---|---|
| Will a non-developer visitor want to see this? | `README.md` |
| Is it an instruction or constraint specific to LLM agents? | `CLAUDE.md` |
| Is it architectural detail, a decision rationale, or an implementation plan? | `docs/` (and indexed in `INDEX.md`) |
| Is it useful for *current task only* and not future readers? | Don't write it down — keep it in the conversation. |

If a piece of content seems to belong in two places, prefer `docs/` and link to it from the others. The README and CLAUDE.md should stay short; `docs/` is where length is acceptable.

## Conventions for new design documents

- **Filename:** kebab-case, descriptive, stable (`cross-shard-handoff.md`, not `notes-2026-04.md`).
- **First line:** an `# H1 Title` matching the filename.
- **Second block:** a one-paragraph summary. This is what a future reader sees in their preview pane and what gets quoted in `INDEX.md`'s catalogue line.
- **Cross-references:** link to other design documents using relative paths (`[handover](evennia-shards-HANDOVER.md)`), and to root-level files with a leading slash (`[CLAUDE.md](../CLAUDE.md)`).
- **Index it.** Add a one-line entry to [INDEX.md](INDEX.md) under the appropriate section. An un-indexed document is invisible.
- **Capture the *why*.** A design document that says *what* without saying *why* will be obsoleted on the first contact with a strong opposing argument. The *why* is what makes it durable.
- **Don't delete; supersede.** If a document is superseded by a refinement, leave it in place and add a note at the top linking to the newer document. Historical context has value.

## Why three surfaces and not one

A single `README` would either be too long for new visitors or too shallow for working contributors. A single `CLAUDE.md` would either pull in agent-irrelevant marketing content or omit the implementation depth that contributors need. A `docs/` folder without a top-level entry point would be invisible to drive-by visitors and unhelpful to LLM agents that must decide what to read.

Three surfaces, each focused on one audience, linking to each other where helpful. The cost is some discipline around what goes where; the benefit is each surface stays useful to its audience for the long haul.
