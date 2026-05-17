# OpsHub v0.1.0

**Local-first operational memory + execution hub for humans and AI agents.**

This is the first public release of OpsHub. Eight implementation phases land
together as a single coherent CLI: an event-sourced SQLite store under your
home directory, a workspace markdown surface that humans and AI agents both
read and write, semantic recall, LLM-backed briefings and proposals, four SaaS
connectors, and a knowledge graph that ties it all back to provenance.

Everything runs locally. No state ships to a cloud service unless you point a
connector at one.

## Why this exists

AI agents are great at the next 30 minutes of work and bad at remembering what
they did yesterday. Humans are the opposite. OpsHub stores work state — tasks,
decisions, briefings, embeddings, knowledge graph links — in a single SQLite
event log under your home directory. AI agents and humans read and write the
same surface via a CLI, so context is preserved across sessions, across
agents, and across the human-agent boundary.

Concretely: when you switch from Claude Code to Codex CLI mid-task, both can
pull the same task, the same decisions, and the same briefing from
`~/.local/share/opshub/`. When tomorrow's you opens the workspace, the
markdown surface has the day's threads ready to triage.

## What landed in v0.1.0

This release packages eight implementation phases. CHANGELOG.md has the full
per-phase change list; the narrative below highlights what's interesting from
a user's perspective.

### Event-sourced core, derivable everything

All state changes flow through a single `events` table
([ADR-0002](https://github.com/ozzy-labs/opshub/blob/main/docs/adr/0002-event-sourced-architecture.md)).
Projections — `tasks`, `decisions`, `inbox_items`, `briefings`, `proposals`,
`links`, and ten more — are derived state. `opshub projections rebuild`
re-derives all of them from the event log, so if a projection goes wrong, it
doesn't take history with it.

### Pluggable LLM + Embedder backends

OpsHub doesn't pick your LLM vendor. Three Embedder backends ship in v0.1.0:

- **local** (sentence-transformers / bge-m3 — runs offline, ~500MB model)
- **OpenAI** (`text-embedding-3-small`)
- **Voyage** (`voyage-3`)

Three LLM backends ship:

- **Anthropic** (`claude-haiku-4-5`, default for `brief` / `propose`)
- **OpenAI** (`gpt-4o-mini`)
- **Ollama** (local `llama3.2:3b` — closes the "what if I want zero API
  spend?" question)

Switching is a config edit; existing data carries forward, and
`opshub embeddings rebuild` re-embeds under the new backend.

### Semantic recall + briefings + proposals

`opshub recall "認証の最近の決定"` performs a semantic search over every
task / decision / inbox item / source summary in the store, ranked by sqlite-
vec ANN. The default English / Japanese embedding (bge-m3) handles mixed-
language queries without per-language indexing.

`opshub brief "phase 5 progress"` asks the LLM to write a structured briefing
on a topic, citing concrete source references. External content is wrapped in
`<source>...</source>` delimiters and HTML-escaped before reaching the model
([ADR-0015](https://github.com/ozzy-labs/opshub/blob/main/docs/adr/0015-llm-usage-strategy.md)
§決定 (f)) so a malicious source can't smuggle instructions past the prompt
boundary.

`opshub propose generate "next steps"` lets the LLM propose new tasks /
decisions, but **never** creates them directly. A human runs
`opshub propose apply <proposal-id> <candidate-index>` to commit each
candidate one at a time
([ADR-0016](https://github.com/ozzy-labs/opshub/blob/main/docs/adr/0016-action-loop-and-structured-output.md)).
No auto-apply mode exists in the codebase.

### Four SaaS connectors

`opshub connector sync <name>` pulls work signals from:

- **GitHub** — issues, PRs, notifications
- **Slack** — channel messages with permalinks
- **Microsoft 365** — Calendar, OneDrive, Outlook (paste-code OAuth)
- **Box** — events API (paste-code OAuth)

All four obey ADR-0005 (External Content Min): source bodies are never
persisted. Each connector enforces a ≤200-char summary cap at ingest, so the
SQLite store never grows linearly with upstream content size.

### Knowledge graph that ties phases together

The `links` projection ([ADR-0017](https://github.com/ozzy-labs/opshub/blob/main/docs/adr/0017-knowledge-graph-layer.md))
records relationships between entities — `task references source`,
`briefing derives_from briefing`, `proposal sourced_from task`. Four
automatic extraction paths populate the graph as events flow in; manual
`opshub link add` covers the cases the heuristics miss.

`opshub graph trace task:<id> --depth 3` walks backward to show which sources,
briefings, and proposals contributed to a given task. The same graph powers
`--expand-graph` on `brief` and `propose generate`, which widens the LLM's
context to 1-hop neighbours of the topic without manual citation curation.

## Install

v0.1.x is distributed directly from this repository (PyPI publishing is
deferred — see [ADR-0001 §Updates](../docs/adr/0001-python-stack.md#updates)
for the rationale and migration path).

```bash
uv tool install git+https://github.com/ozzy-labs/opshub.git@v0.1.0
# or
pipx install "git+https://github.com/ozzy-labs/opshub.git@v0.1.0"
```

For LLM features:

```bash
uv tool install "opshub[llm-anthropic] @ git+https://github.com/ozzy-labs/opshub.git@v0.1.0"
opshub connector auth set llm:anthropic
```

The optional extras matrix is in the [README](https://github.com/ozzy-labs/opshub#optional-dependencies)
— each extras group is small and pulls in only what its name implies. The
default install is ~5 MB (including the base `sqlite-vec` extension promoted
from extras in Phase 8.x); the `local-embedding` extras pulls ~500 MB of model
weights and is opt-in.

## Quickstart

```bash
opshub init                                       # one-time DB + workspace setup
opshub task create "Try OpsHub"
opshub task list
opshub recall "things I forgot to do"             # if embeddings configured
opshub brief "what's on my plate"                 # if an LLM is configured
```

Full command reference: [README §Commands](https://github.com/ozzy-labs/opshub#commands).

## Numbers

- **17** Architecture Decision Records under
  [`docs/adr/`](https://github.com/ozzy-labs/opshub/tree/main/docs/adr)
- **16** Alembic migrations covering 13 projections
- **1533** tests passing + 9 skipped (extras-gated)
- **~140ms** measured `opshub --help` cold-start (budget: ≤300ms per
  ADR-0001)
- **3** LLM backends, **3** Embedder backends, **4** SaaS connectors

## What's next

[`docs/principles.md`](https://github.com/ozzy-labs/opshub/blob/main/docs/principles.md)
§Open Questions #5 is the last open architectural question: **multi-machine
sync**. That's the Phase 9 candidate.
[ADR-0017](https://github.com/ozzy-labs/opshub/blob/main/docs/adr/0017-knowledge-graph-layer.md)
§決定 (c) already technically validates the path of re-deriving projections
on a follower from the event log alone, so the work is sequencing rather than
research.

Per-phase follow-ups live in each ADR under `Phase N.x` deferrals:

- Phase 4.x — duplicate detection cost optimisation
- Phase 5.x — briefing cache + narrow-scope mode
- Phase 6.x — `llama.cpp` direct binding, proposal scoring
- Phase 7.x — connector-side automatic `SourceReferenced` emission
- Phase 8.x — graph visualisation web UI

## Upgrading

There is no upgrade path from a prior version — this is the first release.
For future upgrades, see [`docs/upgrading.md`](https://github.com/ozzy-labs/opshub/blob/main/docs/upgrading.md).

## Security

OpsHub is local-first and single-user by design. The threat model and
in-scope / out-of-scope list are in
[`SECURITY.md`](https://github.com/ozzy-labs/opshub/blob/main/SECURITY.md).
v0.1.0 has no known security issues at release.

## Acknowledgements

OpsHub was designed and built end-to-end with
[Claude Code](https://claude.com/claude-code) driving most of the
implementation. The architecture, ADR set, and `drive` workflow that
produced this release were themselves shaped through human + agent
collaboration in the same tool the project is built to support.

## License

MIT. See [LICENSE](https://github.com/ozzy-labs/opshub/blob/main/LICENSE).

## Full change log

See [`CHANGELOG.md`](https://github.com/ozzy-labs/opshub/blob/main/CHANGELOG.md)
for the detailed per-phase change list.
