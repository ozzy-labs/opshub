# Principles (基本方針)

> Status: Draft (in active design). Last reviewed: 2026-05-17.

OpsHub の設計判断はすべて以下の方針に従う。各方針には可能な限り対応する ADR を紐づける。方針自体が変わる場合は、新しい ADR で superseded を明示する。

## 1. Local-first

Operational Memory の authoritative source はローカルに存在する。外部 SaaS は **source systems** であり、source of truth ではない。ネットワーク断・SaaS 障害・契約終了が起きても、過去の業務文脈は手元に残る。

## 2. Event-Sourced

state の authoritative layer は **append-only domain events**。projection (tables / markdown / vector index) は disposable であり、event 列から rebuildable。「何が起きたか」「なぜ起きたか」「誰が起こしたか」「どう変化したか」を保持する。

→ [ADR-0002: Event-Sourced Architecture](adr/0002-event-sourced-architecture.md)

## 3. Markdown is a Workspace Surface

markdown は source of truth ではなく、人間と agent が読み書きするための **workspace surface**。

- **generated/** 配下の markdown は projection の純粋関数で、disposable。
- **notes/** などの人手記述 markdown は `opshub note save` 等の CLI 経由で event 化される。
- 生成 markdown の直接編集は (上書きされるため) 推奨しない。

→ [ADR-0003: Markdown as Workspace Surface](adr/0003-markdown-as-workspace-surface.md)

## 4. Agent Runtime Boundary

Agent (Claude Code / Codex / Gemini / Copilot) は Operational DB を **直接変更しない**。すべての書き込みは以下を経由する。

- `opshub` CLI
- application service
- repository
- JSON patch proposal

これにより auditability / replayability / safety / validation / coordination を担保する。

→ [ADR-0004: Agent Runtime Boundary](adr/0004-agent-runtime-boundary.md)

## 5. Multi-Agent Neutral

Claude Code / Codex CLI / Gemini CLI / GitHub Copilot CLI を平等に support する。単一 vendor 依存を避け、prompt / 設定 / skill 配置も vendor-neutral を保つ。

## 6. External Content Minimization

外部 SaaS の full body (Slack history / mail body / 機密文書本文) は **保持しない**。Operational Memory に取り込むのは以下のみ。

- external IDs
- URLs
- summaries
- metadata
- extracted action items
- 最小引用 (minimal quotations)

→ [ADR-0005: External Content Minimization](adr/0005-external-content-minimization.md)

## 7. Connector Contract

Connector は以下の経路のみを行う。

```text
external metadata → source entity → source event → inbox item
```

Connector は task / decision / link を勝手に生成しない。それらは **triage を必ず通す**。

## 8. Replayability

projection・graph・markdown はすべて event 列の純粋関数。

- `opshub projections rebuild` を実行すれば、現在の projection は同一の結果になる。
- `opshub workspace generate --force` を実行すれば、現在の workspace markdown は同一の結果になる。

CI でこの不変条件を検証する。

## 9. Phased Delivery

| Phase | スコープ | Status |
|---|---|---|
| 1 | Foundation: event store + tasks + CLI + markdown 生成 + tests + CI | ✅ Complete (2026-05-17) |
| 2 | Coordination: inbox triage / decisions / locks / handoffs / work sessions / agent runs | ✅ Complete (2026-05-17) |
| 3 | Connectors: framework + GitHub (MVP) + workspace inbox file ingest | ✅ Complete (2026-05-17) |
| 4 | Semantic Layer: vector recall / semantic search / duplicate detection (MVP = Pluggable Embedder + sqlite-vec; briefing 自動生成は Phase 5) | ✅ Complete (2026-05-17) |
| 5 | Briefing layer: ADR-0015 + Pluggable LLM (Anthropic + OpenAI) + `opshub brief` + event-driven auto-embed (補助) | ✅ Complete (2026-05-17) |
| 6 | Action loop layer: ADR-0016 + Pluggable LLM structured output (Anthropic + OpenAI + Ollama) + Proposal domain (events + projection + service + `opshub propose` CLI、human-in-the-loop apply 必須) | ✅ Complete (2026-05-17) |
| 7 | Connectors Wave 2: Slack + Microsoft 365 + Box (3 SaaS connector を Phase 3 framework + ADR-0010 + ADR-0014 + ADR-0005 上で実装、epic #113) | ✅ Complete (2026-05-17) |
| 8 | Knowledge graph layer: ADR-0017 + `links` projection (migration 0016) + 4 自動抽出経路 (`ProposalApplied` / `BriefingGenerated.source_refs` / `ProposalRequested.briefing_id` / `SourceReferenced`) + manual link CRUD (`LinkCreated` / `LinkDeleted` events) + `LinkService` traversal (`related` / `trace` / `expand`) + `opshub link` + `opshub graph` CLI + `--expand-graph` integration (epic #128) | ✅ Complete (2026-05-17) |

各 phase で価値検証してから次へ進む。Phase をスキップしない。

## 10. Pythonic but Vendor-Neutral

Python 3.13+ / uv / Typer / SQLAlchemy Core / Pydantic v2 を採用。ただし agent 連携 / prompt / 設定スキーマは vendor-neutral に保つ。

→ [ADR-0001: Python Stack](adr/0001-python-stack.md)

## Open Questions

> Phase 8 完了時点で残る Open Question は §5 (Multi-machine sync) のみ — Phase 9 候補で着手予定。
> ADR-0015 §決定 (a) (Local LLM deferred) は Phase 6 A4 (Ollama) で closeout され、ADR-0016 §決定 (h) として記録された。

検討中の項目 (本ドキュメントの今後の更新対象、番号は旧 Open Q list を継承):

- **§5 Multi-machine sync** — operational memory を複数 host で共有する経路 (event log replication + projection rebuild on follower、または cloud-hosted sync server)。ADR-0002 (Event-Sourced Architecture) の append-only / replayable 不変条件と整合する設計は可能だが、conflict resolution (同 task に対する複数 host からの並行 update) と private data residency の評価が必要。Phase 9+ で別 ADR / 別 plan を検討。

## 確定済み (旧 Open Question)

> 旧 Open Q 番号 trace: 旧 Open Q #1 = LLM 利用方針 (ADR-0015 で本セクションに移動)、旧 Open Q #2 = Lock 粒度 (本セクションで解決)、旧 Open Q #3 = SaaS token 保管方式 (本セクションで解決)、旧 Open Q #4 = Local LLM backend (ADR-0016 §決定 (h) Ollama 採用で closeout、Phase 6 A4 で実装)。

- **LLM 利用方針** (旧 Open Q #1) → ADR-0015 で Pluggable LLM Protocol + Anthropic / OpenAI 具象 + prompt injection mitigation + API key 保管 (ADR-0014 再利用) を採択 (Phase 5 step A1 で確定、D1 で Validation セクションを追加)
- **Embedding モデル選定** → ADR-0012 で Pluggable Embedder 設計を採択。具体モデル選定は Phase 4 着手時 (ADR-0012 の Open Questions 1-2)
- **Task runner** → `just` 採用 (ADR-0001)
- **Lock の粒度設計** (旧 Open Q #2) → ADR-0013 で `task:<id>` / `project:<id>` / `global:` の 3 階層 + fail-fast conflict semantics を採択 (Phase 2 step 5 で実装)
- **SaaS token 保管方式** (旧 Open Q #3) → ADR-0014 で `keyring` library 経由の OS keychain を採択 (Phase 3 step A6 で実装)
- **Local LLM backend** (旧 Open Q #4) → ADR-0016 §決定 (h) で Ollama daemon (OpenAI 互換 endpoint) を採択 (Phase 6 step A4 で実装)。`llama.cpp` direct binding は Phase 6.x 持ち越し
