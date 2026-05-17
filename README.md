# OpsHub

[![PyPI](https://img.shields.io/pypi/v/opshub.svg)](https://pypi.org/project/opshub/)
[![CI](https://github.com/ozzy-labs/opshub/actions/workflows/ci.yaml/badge.svg?branch=main)](https://github.com/ozzy-labs/opshub/actions/workflows/ci.yaml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/downloads/)

**Local-first operational memory + execution hub for humans and AI agents.**

*人間と AI エージェントのための、ローカルファーストな Operational Memory 兼 実行ハブ。*

OpsHub stores work state — tasks, decisions, briefings, embeddings, links — in
a single SQLite event log under your home directory. AI agents read and write
the same surface via a CLI, so context is preserved across sessions without
shipping state to a cloud service.

## Install

```bash
uv tool install opshub
# or
pipx install opshub
```

Optional extras (pulled only if needed):

```bash
uv tool install "opshub[vector,llm-anthropic,connectors-github]"
```

See [Optional dependencies](#optional-dependencies) below for the full extras matrix.

## Quickstart

```bash
opshub init                                           # one-time DB + workspace setup
opshub task create "Write blog post about OpsHub"     # create a task
opshub task list                                       # see open tasks

# Once the LLM backend is configured (next section):
opshub brief "current priorities"                      # LLM-summarised briefing
opshub propose generate "what's next?"                 # LLM-proposed next-actions
opshub propose apply <proposal-id> 0                  # operator-approved entity creation
```

All state lives under XDG directories; override via `OPSHUB_*` env vars
(e.g. `OPSHUB_STORAGE__DB_PATH=/custom/path.sqlite`).

## Configure an LLM backend (optional)

OpsHub is functional without any LLM — `task` / `decision` / `inbox` /
`connector sync` all work standalone. To enable `brief` / `propose`, configure
one of:

```bash
opshub connector auth set llm:anthropic       # Anthropic Claude (recommended)
opshub connector auth set llm:openai          # OpenAI
# Or for local-only:
ollama serve && ollama pull llama3.2:3b
# Then set [llm] backend = "ollama" in ~/.config/opshub/config.toml
```

See [`docs/principles.md`](docs/principles.md) §1 (Local-first) for the design
rationale.

## What's in OpsHub today

Phases 1–8 shipped (2026-05-17, v0.1.0):

| Phase | Layer | Highlights |
|---|---|---|
| 1 | Foundation | Event store, tasks, CLI, markdown workspace |
| 2 | Coordination | Inbox / decisions / sessions / locks / handoffs |
| 3 | Connectors framework | GitHub connector + workspace file ingest |
| 4 | Semantic recall | Pluggable Embedder (local / OpenAI / Voyage) + sqlite-vec + `recall` + duplicate detection |
| 5 | Briefing | Pluggable LLM (Anthropic / OpenAI) + `brief` + prompt injection mitigation |
| 6 | Action loop | Structured output + Ollama backend + `propose` (human-in-the-loop) |
| 7 | Connectors wave 2 | Slack + Microsoft 365 + Box |
| 8 | Knowledge graph | `links` projection + auto-extraction + `graph` + `--expand-graph` |

Next: **Phase 9 (Multi-machine sync)** — see [`docs/principles.md`](docs/principles.md)
§Open Questions #5. Longer phase-by-phase narrative lives in
[`docs/architecture.md`](docs/architecture.md) §9 (Phased Delivery).

## Commands

```bash
# Foundation (Phase 1)
opshub task create "draft phase 2 plan"
opshub task list --format md                          # formats: table / md / json

# Coordination (Phase 2)
opshub inbox add "triage the failing nightly build"
opshub inbox triage <id> --to-task "fix nightly build"
opshub inbox list --format md
opshub decision record "adopt sqlite-vec for Phase 4"
opshub decision list
opshub lock acquire task:<task-ulid>                  # ADR-0013 coordination lock
opshub lock release <lock-id>
opshub lock list
opshub session start --scope "phase-3 design"         # work session bracket
opshub agent run begin claude                         # auto-injected into agent runs
opshub agent run end <run-id> --summary "drafted ADR-0017"
opshub session end --summary "EOD wrap"
opshub handoff open --from agent:claude --to ozzy --topic "review"
opshub handoff close <handoff-id> --note "merged"

# Connectors (Phase 3 + Phase 7, ADR-0010 / ADR-0014)
opshub connector auth set github                      # store GitHub PAT in OS keychain
opshub connector sync github                          # incremental sync (OPSHUB_CONNECTOR_GITHUB_REPO=owner/repo)
opshub connector auth set connector:slack             # store Slack bot token in OS keychain
opshub connector sync slack                           # incremental sync ([connectors.slack] channels)
opshub connector auth set connector:ms365             # OAuth paste-code (Microsoft Graph Calendar / OneDrive / Outlook)
opshub connector sync ms365                           # incremental sync per endpoint
opshub connector auth set connector:box               # OAuth paste-code (Box Events API)
opshub connector sync box                             # incremental sync (Box stream_position cursor)
opshub connector list                                 # show registered connectors

# Workspace + projections
opshub workspace ingest                               # ingest workspace/inbox/*.md (Phase 3)
opshub workspace ingest --dry-run                     # scan only, no writes
opshub workspace generate                             # regenerate markdown workspace from projections
opshub projections rebuild                            # rebuild projections from the event store (idempotent)

# Semantic recall (Phase 4, ADR-0012)
opshub connector auth set embedder:openai             # store OpenAI API key in OS keychain
opshub embeddings rebuild                             # bulk-embed task/decision/inbox/source summaries
opshub embeddings status                              # show backend + per-entity-type embedded vs pending
opshub embeddings drain                               # retry pending embeddings (auto-embed hook backup)
opshub embeddings find-duplicates -t 0.92             # offline near-duplicate scan
opshub recall "認証の最近の決定"                       # semantic search across all entities

# Briefing (Phase 5, ADR-0015)
opshub connector auth set llm:anthropic               # store Anthropic API key in OS keychain
opshub brief "phase 5 progress"                       # LLM-backed briefing (markdown to stdout)
opshub brief "phase 5 progress" --save                # also persist under <workspace>/briefings/
opshub brief "phase 5 progress" --format json         # JSON with briefing_id / model / tokens / source_refs
opshub brief "phase 8 review" --expand-graph          # widen LLM context via 1-hop graph expansion

# Action loop (Phase 6, ADR-0016)
opshub propose generate "next steps"                  # LLM proposes task/decision candidates
opshub propose generate "next steps" --from-briefing <id>
opshub propose generate "next steps" --format json
opshub propose generate "next steps" --expand-graph   # 1-hop graph expansion for proposals
opshub propose list                                   # recent proposals (markdown table)
opshub propose list --state pending --limit 10        # filter: pending / applied / rejected
opshub propose apply <proposal-id> <candidate-index>  # operator approval → creates entities
opshub propose reject <proposal-id> <candidate-index> --reason "out of scope"

# Knowledge graph (Phase 8, ADR-0017)
opshub link add task:<task-id> source:<src-id> --type references
opshub link list --from task:<task-id>                # filter by --from / --to / --type
opshub link remove <link-id> --reason "wrong source"  # hard delete (emits LinkDeleted)
opshub graph related task:<task-id> --direction both  # 1-hop neighbours (md / json / dot)
opshub graph trace task:<task-id> --depth 3           # backward provenance walk (default 3, max 10)
opshub graph expand task:<task-id> --depth 2 --format dot
```

## Optional dependencies

| Extras | Purpose | Heavy? |
|---|---|---|
| `vector` | sqlite-vec for semantic recall | Small |
| `local-embedding` | sentence-transformers (bge-m3, ~500MB) | Heavy |
| `api-embedding-openai` / `api-embedding-voyage` | API embedder backends | Small |
| `llm-anthropic` / `llm-openai` | API LLM backends | Small |
| `llm-ollama` | Ollama daemon client | Small |
| `connectors-github` / `connectors-slack` / `connectors-msgraph` / `connectors-box` | SaaS connectors | Small |
| `secrets` | OS keyring backend | Small |
| `dev` | Test + lint toolchain | Medium |

## Documentation

- [`docs/principles.md`](docs/principles.md) — design principles (local-first, event-sourced, etc.)
- [`docs/architecture.md`](docs/architecture.md) — layered architecture overview
- [`docs/adr/`](docs/adr/README.md) — Architecture Decision Records
- [`docs/upgrading.md`](docs/upgrading.md) — version migration notes (when applicable)
- [`CHANGELOG.md`](CHANGELOG.md) — release history
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — contribution guidelines
- [`SECURITY.md`](SECURITY.md) — vulnerability disclosure

## License

MIT. See [LICENSE](LICENSE).
