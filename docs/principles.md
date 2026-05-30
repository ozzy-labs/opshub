# Principles (基本方針)

> Status: Draft (in active design). Last reviewed: 2026-05-31.

OpsHub の設計判断はすべて以下の方針に従う。各方針には可能な限り対応する ADR を紐づける。方針自体が変わる場合は、新しい ADR で superseded を明示する。

## 1. Local-first

Operational Memory の authoritative source はローカルに存在する。外部 SaaS は **source systems** であり、source of truth ではない。ネットワーク断・SaaS 障害・契約終了が起きても、過去の業務文脈は手元に残る。

Phase 10 で本文をローカル保持する設計 ([ADR-0020](adr/0020-full-local-content-retention.md)) に移行したことで、この方針はより強化された。要約だけを残す旧設計 (ADR-0005) では SaaS 側で本文が消えるとローカルの要約から元文脈を辿れないという「真の意味では local-first でない」抜け穴があった。本文を手元に持つことで、オフライン継続性・上流削除耐性・監査可能性が揃う (代償＝保存時暗号化と provenance タグの責務、§6 参照)。

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
- **MCP server (Phase 10、`opshub mcp serve` stdio)** — エージェント host が ① コアを叩く正式な経路 ([ADR-0022](adr/0022-mcp-server-surface.md))

これにより auditability / replayability / safety / validation / coordination を担保する。

Phase 10 で **三層モデル (人間 → 秘書エージェント → opshub コマンド)** が確立した ([ADR-0004 改訂](adr/0004-agent-runtime-boundary.md))。opshub は **形A** ＝ MCP サーバ (口) ＋ Agent Skills (手順書) のみを提供し、頭脳 (LLM 推論ループ) は外部エージェント host (Claude Code 等) が担う。opshub 自身はエージェント runtime / 常駐プロセス / 人格を持たない。詳細は [docs/secretary-agent.md](secretary-agent.md) と [ADR-0004](adr/0004-agent-runtime-boundary.md) を参照。

→ [ADR-0004: Agent Runtime Boundary](adr/0004-agent-runtime-boundary.md) / [ADR-0022: MCP Server Surface](adr/0022-mcp-server-surface.md)

## 5. Multi-Agent Neutral

Claude Code / Codex CLI / Gemini CLI / GitHub Copilot CLI を平等に support する。単一 vendor 依存を避け、prompt / 設定 / skill 配置も vendor-neutral を保つ。

## 6. Full Local Content Retention (Phase 10 改訂)

外部 SaaS の **本文を含めてローカル保持する**。Phase 1-9 までの「要約のみ保持」方針 (旧 §6 + ADR-0005) は秘書エージェント・プラットフォーム化 ([ADR-0020](adr/0020-full-local-content-retention.md)) で supersede された。要約のみだと再要約・横断検索・返信下書きの文体再現といった秘書ユースケースで上流再取得を強いられ、SaaS 側で本文が消えた瞬間に文脈が失われる ＝ §1 Local-first との矛盾だった。

Phase 10 以降は次を保持する:

- 外部 SaaS の本文 (Slack message body / GitHub issue/PR body / Outlook 本文 / Box ファイル抽出テキスト 等、connector が取れた範囲)
- external IDs / URLs / metadata
- 要約・抽出された action items
- **provenance タグ** (`provenance_origin` ＝ external/internal、`provenance_trust` ＝ trusted/untrusted、[ADR-0020](adr/0020-full-local-content-retention.md) §(e))

保持に伴う安全策はセットで組み込む:

| 安全策 | 仕組み |
|---|---|
| 取り込み除外 (excludes) | `~/.config/opshub/excludes.yaml` で channel / sender / repo / path を除外 ([ADR-0020](adr/0020-full-local-content-retention.md) §(b)) |
| 保存時暗号化 | SQLCipher で DB 丸ごと AES-256、鍵は OS keychain (`opshub.toml` の `[storage] encryption = true` で opt-in、[ADR-0021](adr/0021-encryption-at-rest.md)) |
| 認証情報の本文からの分離 | SaaS トークンは `core/secrets` + keyring 経由 ([ADR-0014](adr/0014-saas-token-storage.md))、本文 / event payload には混入させない |
| provenance タグ | 外部由来の本文は `origin="external"` + `trust="untrusted"` で取り込み、LLM は指示でなく参照素材として扱う ([ADR-0020](adr/0020-full-local-content-retention.md) §(e) + [ADR-0015](adr/0015-llm-usage-strategy.md) §決定 (f)) |

「本文をローカルに置く方が安全」かどうかは threat model 次第。**single-user / single-host / OS-level access control の前提下**では、SaaS 側で消されてからも手元に残るほうが local-first の不変条件と整合する。本文をクラウド側に置いたままにする「最小化」方針は、秘書がオフラインで応答できない・SaaS 障害で履歴を読めない ＝ §1 違反になる。詳細な脅威モデル議論は [ADR-0020](adr/0020-full-local-content-retention.md) §Negative / [SECURITY.md](../SECURITY.md) を参照。

→ [ADR-0020: Full Local Content Retention](adr/0020-full-local-content-retention.md) (supersedes [ADR-0005](adr/0005-external-content-minimization.md)) / [ADR-0021: Encryption at Rest](adr/0021-encryption-at-rest.md)

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
| 9 | Local-filesystem-backed Connector Layer: ADR-0019 + `sources.fingerprint` 列 (migration 0017) + `box_drive` connector (Box Drive デスクトップクライアント経由のローカル FS scan、scanner + mapper + connector + settings) + `core/platform.py` (WSL2 / macOS 判定 helper) + `opshub connector sync box_drive` 経路 (epic #187) | ✅ Complete (2026-05-23) |
| 10 | Secretary Agent Platform: ADR-0020 (full local content retention、ADR-0005 supersede) + ADR-0021 (encryption at rest、SQLCipher + keyring) + ADR-0022 (MCP server surface、stdio + policy-as-data + redact + OTel naming) + ADR-0004 改訂 (形A: opshub は MCP + Agent Skills のみ提供、runtime なし) + ADR-0016 改訂 (`ReplyDraftCandidatePayload`) + ADR-0017 改訂 (`reply_draft_replies_to` / `referenced_in_reply_draft` link types) + ADR-0010 改訂 (write-back 明示禁止) + 本文ベース embedding + SQLite FTS5 + `opshub search` CLI + `opshub mcp serve` CLI + 秘書 5 Skills + `tools/skill_scan.py` (epic #203) | ✅ Complete (2026-05-31) |

各 phase で価値検証してから次へ進む。Phase をスキップしない。

## 10. Pythonic but Vendor-Neutral

Python 3.13+ / uv / Typer / SQLAlchemy Core / Pydantic v2 を採用。ただし agent 連携 / prompt / 設定スキーマは vendor-neutral に保つ。

→ [ADR-0001: Python Stack](adr/0001-python-stack.md)

## Open Questions

> Phase 10 完了時点で残る Open Question は §5 (Multi-machine sync) と §能動性 (常駐 / cron) のみ。
> ADR-0015 §決定 (a) (Local LLM deferred) は Phase 6 A4 (Ollama) で closeout され、ADR-0016 §決定 (h) として記録された。

検討中の項目 (本ドキュメントの今後の更新対象、番号は旧 Open Q list を継承):

- **§5 Multi-machine sync** — operational memory を複数 host で共有する経路 (event log replication + projection rebuild on follower、または cloud-hosted sync server)。ADR-0002 (Event-Sourced Architecture) の append-only / replayable 不変条件と整合する設計は可能だが、conflict resolution (同 task に対する複数 host からの並行 update) と private data residency の評価が必要。Phase 12+ 候補 (Phase 9 = Local-filesystem-backed Connector Layer、Phase 10 = Secretary Agent Platform 完了、Phase 11 = MS Office 深掘り予定)。Phase 9 ADR-0019 が「FS-backed connector が 1 host 1 mount を前提に scan する」を pin したことで、multi-machine sync を「FS scan の host 跨ぎ」で代替する選択肢は self-defeating (Box Drive 自体が SaaS 経由の同期を行っているため二重同期になる) と確認済。Phase 10 で本文をローカル保持 + 暗号化に移行したため、本物の multi-machine sync は **暗号化 DB の event log replication 経路** を前提に別 ADR / 別 plan を要する。
- **能動性 (常駐 / cron / 通知)** — Phase 10 は「リクエスト駆動 (ユーザーがセッションで聞いたときに秘書が応答)」に絞り、能動的な push / 定期実行は実装しない ([ADR-0004](adr/0004-agent-runtime-boundary.md) 形A、Phase 10 plan §1 #5)。将来段階案は [`phase-10-plan.md`](phase-10-plan.md) §9 に記載 (段階 0 = task に期限、段階 1 = cron 委譲の冪等コマンド、段階 2 = 記憶キュレーション、段階 3 = 通知、段階 4 = 常駐 + filewatch)。core に常駐 daemon は持たせず、トリガは OS-level scheduler (cron / systemd timer / launchd / Win タスク) に外出しする方針が確定済。

## 確定済み (旧 Open Question)

> 旧 Open Q 番号 trace: 旧 Open Q #1 = LLM 利用方針 (ADR-0015 で本セクションに移動)、旧 Open Q #2 = Lock 粒度 (本セクションで解決)、旧 Open Q #3 = SaaS token 保管方式 (本セクションで解決)、旧 Open Q #4 = Local LLM backend (ADR-0016 §決定 (h) Ollama 採用で closeout、Phase 6 A4 で実装)。

- **LLM 利用方針** (旧 Open Q #1) → ADR-0015 で Pluggable LLM Protocol + Anthropic / OpenAI 具象 + prompt injection mitigation + API key 保管 (ADR-0014 再利用) を採択 (Phase 5 step A1 で確定、D1 で Validation セクションを追加)
- **Embedding モデル選定** → ADR-0012 で Pluggable Embedder 設計を採択。具体モデル選定は Phase 4 着手時 (ADR-0012 の Open Questions 1-2)
- **Task runner** → `just` 採用 (ADR-0001)
- **Lock の粒度設計** (旧 Open Q #2) → ADR-0013 で `task:<id>` / `project:<id>` / `global:` の 3 階層 + fail-fast conflict semantics を採択 (Phase 2 step 5 で実装)
- **SaaS token 保管方式** (旧 Open Q #3) → ADR-0014 で `keyring` library 経由の OS keychain を採択 (Phase 3 step A6 で実装)
- **Local LLM backend** (旧 Open Q #4) → ADR-0016 §決定 (h) で Ollama daemon (OpenAI 互換 endpoint) を採択 (Phase 6 step A4 で実装)。`llama.cpp` direct binding は Phase 6.x 持ち越し
- **FS-backed connector pattern** → ADR-0019 で Box Drive デスクトップクライアント経由のローカル FS scan を採択 (Phase 9 で実装)。`Connector` Protocol は変えず auth layer を OS-level 認証への依存に置換、`os.stat()` metadata のみ参照 (`open()` 禁止 不変条件)、Identity = `rel_path` (path-as-id)、Diff detection = `sources.fingerprint` 列 (`f"{size}:{mtime_ns}"`)、削除追跡なし (stale row は Phase 9.x 候補)、Operator precondition (`mountvol` / `wsl --shutdown`) は opshub 範囲外 (`docs/box-drive-setup.md`)、Watch mode は Phase 9.x 持ち越し。Phase 7 `box` connector (`source_type="box_event"`) と Phase 9 `box_drive` connector (`source_type="box_drive_file"`) は二重取り込み許容
