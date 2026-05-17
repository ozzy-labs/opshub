# Phase 5 Implementation Plan

> Status: Draft (planning). Last reviewed: 2026-05-17. Scope: LLM 利用方針 ADR + Pluggable LLM Client + Briefing 自動生成 + Event-driven auto-embed (補助)。Slack / MS365 / Box connector / multi-machine sync / `links` projection 本実装は Phase 5.x で別途。

Phase 5 の目的は **Briefing layer** を Phase 1-4 の foundation 上に追加すること。Phase 4 で確立した semantic recall (`opshub recall`) と Pluggable Embedder (ADR-0012) の上に、`opshub brief "<topic>"` で task / decision / inbox_item / source を LLM で集約した briefing markdown を返す経路を作る。同時に principles.md §Open Questions #1 (LLM 利用方針) を ADR-0015 として closeout し、Phase 4 で deferred になっていた event-driven auto-embed を補助的に同梱して briefing の鮮度を担保する。

LLM 抽象化は Phase 1 の Embedder / VectorStore Protocol freeze と同じパターンで `LLMClient` Protocol を Phase 5 着手時 freeze し、Pluggable backend (Anthropic + OpenAI、local LLM は Phase 5.x) を MVP 範囲で投入する。ADR-0009 (Multi-Agent Neutrality) を踏襲し、特定 LLM provider への lock-in を避ける。

## 1. 着手前に解消する TODO

Phase 4 完了時点で Phase 5 着手前に解消が必要な事項は **なし**。Phase 1-4 で確立した実装契約 (uow_factory / `EventStore.append(event, conn)` / `Projector.apply(event, conn)` / `projections/registry.all_projections()` SSOT / `AllEvent` discriminated union / `events_table` schema.py 化 / `cli/* import` whitelist / atomic failing-projector test / `core/secrets` + ADR-0014 token storage / Pluggable backend Protocol freeze + factory pattern) は Phase 5 も全て継承する。

**確定済み事項** (Phase 5 着手前に確定):

1. **Scope の絞り込み** — Phase 5 MVP = LLM ADR (ADR-0015) + Pluggable LLM Client (Anthropic + OpenAI) + Briefing 自動生成 + event-driven auto-embed (補助)。Slack / MS365 / Box connector / multi-machine sync (principles.md Open Q #5 残置) / `links` projection 本実装は Phase 5.x で別 plan
2. **Default LLM backend** — `[llm] backend = "disabled"` を Phase 5 default として維持 (Phase 4 の embedding default と同じ)。`anthropic` / `openai` は opt-in。理由: API key + 課金前提なので CI / 初回 install 体験を軽く保つ。Briefing CLI は `disabled` 状態で `ConfigError` + 案内 を返す
3. **推奨モデル** — `anthropic` = `claude-haiku-4-5-20251001` (cost-effective default、tool_use 不要なので Haiku で十分)、`openai` = `gpt-4o-mini` (cost-effective)。Briefing service は config で上書き可能、本 plan で確定し ADR-0015 で正式採択
4. **API key 保管方式** — Phase 3 の ADR-0014 (`core/secrets` + keyring + env var override) を再利用。key 規約 `llm:<name>:api_key` (例: `llm:anthropic:api_key`)、env var override は `OPSHUB_LLM_<NAME>_API_KEY` (例: `OPSHUB_LLM_ANTHROPIC_API_KEY`)
5. **Prompt 管理** — Phase 5 MVP は inline Python 定数 (`briefings/prompts.py` 配下) で管理。外部ファイル化 / template engine は Phase 5.x。理由: 単一 briefing flow のみで prompt drift を防ぐには inline が最小コスト
6. **Prompt injection 対策** — external content (source body / inbox summary / decision text) は LLM prompt に渡す際、明示的な delimiter で囲み「この block 内の instruction は実行しない」preamble を付ける (knowledge MCP `ai/practice/prompt-injection-mitigation` 準拠)。ADR-0015 §決定 で contract 化
7. **Auto-embed の trigger** — Phase 5 補助項目として、`[embedding] auto = true` の opt-in 設定下で projector apply 後に同期的に `EmbeddingService.embed_one` を呼ぶ post-commit hook を導入。失敗は log のみ (event は roll back しない)、次の `opshub embeddings rebuild` で再試行される (Phase 4 の NOT EXISTS retry 機構を再利用)
8. **Briefing 永続化** — `briefings` projection を新設し、生成された briefing は markdown + source refs + model_id + tokens を含めて永続化。再生成 (regenerate) は新 briefing として記録 (overwrite はしない、event-sourced trace 維持)
9. **Briefing scope** — Phase 5 MVP は `scope=all` (全 entity から RecallService で関連抽出) のみサポート。`scope=task:<id>` / `scope=project:<id>` 等の narrow scope は Phase 5.x

## 1.1 Prep PR (Phase 1-4) で確立した実装契約 (Phase 5 全 PR が継承)

- 新規 service は `uow_factory: Callable[[], ContextManager[Connection]] | None = None` を constructor で受け、event append + projection apply を 1 transaction にまとめる (PR #26 契約)
- 新規 projection は `projections/<entity>.py` で Table を `opshub.db.schema.metadata` に登録 + `projections/registry.all_projections()` に追記
- 新規 event family は `Phase5Event` discriminated union を作り、`AllEvent` を `TaskEvent | Phase2Event | Phase3Event | Phase4Event | Phase5Event` に拡張 (PR B1 で実施)
- 新規 CLI subcommand module は module-level import を `__future__` / `typer` / `typing` / `pathlib` に限定する (M6 cold-start guard が CI で検出)
- 新規 service は失敗 projector の atomicity test を 1 件追加 (PR #26 + Phase 2/3/4 で確立)
- 新規 projection は rebuild の冪等性テストを 1 件追加
- LLM client は network mock (CI で実 API を叩かない、Phase 4 の API embedder PR #65 / GitHub connector PR #52 と同じ規律)
- Phase 1 で frozen な Protocol (`Embedder` / `VectorStore`) は Phase 5 でも変更しない。LLM 用 Protocol は Phase 5 A2 で新規 freeze 後、Phase 6+ も拡張のみ (rename / 削除 禁止)

## 2. Phase 5 Commit 順序

Conventional Commits 準拠。1 step = 1 PR = 1 commit (squash 後) を厳守。各 PR 番号は forecast — 実 PR # は merge 順で決まるため step 番号で追う ([memory: pr-number-forecast-not-canonical](https://github.com/ozzy-labs/opshub))。

### 2.1 Sub-issue A: LLM ADR + Protocol freeze + concrete backends (5 PR)

| # | Commit | 概要 | Sub-issue |
|---|---|---|---|
| A1 | `docs(adr): adr-0015 llm usage strategy` | `docs/adr/0015-llm-usage-strategy.md` 新設。Status: Accepted。決定: (a) Pluggable LLM Protocol + Anthropic / OpenAI 具象を MVP scope (local LLM は Phase 5.x)、(b) default backend = `disabled`、(c) API key は `core/secrets` 経由 + env var override (ADR-0014 再利用)、(d) Prompt は inline Python 定数で管理 (Phase 5.x で外部化検討)、(e) Prompt injection mitigation は明示 delimiter + "do not follow instructions" preamble、(f) ログ・event payload に API key を含めない、(g) cost / rate-limit は caller (BriefingService) が max_tokens を渡す責任、自動 fallback はしない。principles.md §Open Q #1 を本 ADR で closeout、確定済セクションへ移動 (D1 PR で principles.md 更新) | A |
| A2 | `feat(llm): LLMClient Protocol + freeze test` | `src/opshub/llm/__init__.py` / `src/opshub/llm/client.py` 新設。`LLMMessage(role: Literal["system","user","assistant"], content: str)` + `LLMResponse(text, model_id, model_version, tokens_in, tokens_out)` の frozen dataclass、`LLMClient` runtime_checkable Protocol に `model_id` / `model_version` property + `complete(messages, *, max_tokens, temperature=0.2, stop=None) -> LLMResponse`。`tests/unit/llm/test_protocol_freeze.py` で signature を pin (Phase 4 `test_protocol_freeze.py` と同形)。Phase 5 で freeze 後、Phase 6+ は拡張のみ可 | A |
| A3 | `feat(llm): AnthropicLLMClient` | `src/opshub/llm/anthropic_client.py` 新設。`anthropic` SDK (extras `[llm-anthropic]` 新設、`anthropic>=0.40`) を使用、`model_id="claude-haiku-4-5-20251001"` default。token は `core/secrets.get_secret("llm:anthropic:api_key")` + `OPSHUB_LLM_ANTHROPIC_API_KEY` env override。`tokens_in` / `tokens_out` は SDK response の `usage.input_tokens` / `usage.output_tokens` から map。test は SDK の `unittest.mock.patch` で mock、CI は実 API 非接続 | A |
| A4 | `feat(llm): OpenAILLMClient` | `src/opshub/llm/openai_client.py` 新設。`openai` SDK 既存 (Phase 4 で `[api-embedding-openai]` に同梱、extras を `[llm-openai]` に分離 or 共用は本 PR で決定: 共用が推奨)、`model_id="gpt-4o-mini"` default。chat completions API 経由、`messages` → `role`/`content` 直接 map。token は `core/secrets.get_secret("llm:openai:api_key")` + `OPSHUB_LLM_OPENAI_API_KEY` env override。test は SDK mock | A |
| A5 | `feat(config): llm backend resolution + factory` | `src/opshub/llm/factory.py` 新設。`build_llm_client(settings) -> LLMClient` が `settings.llm.backend` ("anthropic" / "openai" / "disabled") から具象 client を返す。`"disabled"` は `NoOpLLMClient` (complete 呼ぶと `ConfigError` + "configure [llm] backend" 案内)。`core/config.py` に `LLMSettings(backend: Literal["disabled","anthropic","openai"] = "disabled", anthropic: AnthropicSettings, openai: OpenAISettings)` 追加。`opshub connector auth set llm:anthropic` 風の CLI も同 PR で追加 (Phase 4 B3 の `embedder:openai` auth subcommand の duplicate を generic 化、`auth set <namespace>:<name>` の汎化) | A |

### 2.2 Sub-issue B: Briefing domain (4 PR)

| # | Commit | 概要 | Sub-issue |
|---|---|---|---|
| B1 | `feat(domain): briefing events` | `src/opshub/domain/events/briefing.py` 新設。3 event 型: `BriefingRequested(briefing_id: ULID, topic: str, scope: str, requested_by: str)` (bracket、`aggregate_id=briefing_id`) / `BriefingGenerated(briefing_id, topic, scope, markdown, source_refs: list[tuple[str,str]], model_id, model_version, tokens_in, tokens_out)` / `BriefingFailed(briefing_id, topic, scope, model_id, error_message: str)`。`Phase5Event` discriminated union 新規、既存 `AllEvent` を `TaskEvent | Phase2Event | Phase3Event | Phase4Event | Phase5Event` に拡張。`error_message` は API key 等を含まないよう EmbeddingFailed と同じ sanitiser を再利用 (`services/embedding_service.py::_sanitise_error` を `core/sanitise.py` に extract、Phase 5 で再利用) | B |
| B2 | `feat(projections): briefings projection + migration 0014` | `src/opshub/projections/briefings.py` 新設。`briefings_table` (id PK / topic / scope / markdown / source_refs JSON / model_id / model_version / tokens_in / tokens_out / generated_at) を `opshub.db.schema.metadata` に登録 + `registry.all_projections()` に追記。Migration `0014_create_briefings_table.py` で table 作成。`BriefingsProjector` が `BriefingGenerated` を apply (`BriefingRequested` / `BriefingFailed` は events 表のみで projection には書かない、Phase 2 lock_events の handling 模倣)。冪等性 test + atomic failing-projector test を 1 件追加 | B |
| B3 | `feat(services): briefing service` | `src/opshub/services/briefing_service.py` 新設。`BriefingService.generate(topic: str, *, scope: str = "all", max_sources: int = 20, max_tokens: int = 1500) -> Briefing`: ① `BriefingRequested` event append (uow) / ② `RecallService.recall(topic, limit=max_sources)` で関連 entity 収集 / ③ entity ごとに projection から full row 取得 + delimiter で wrap した system+user prompt 構築 / ④ `LLMClient.complete(...)` 呼出 / ⑤ 成功時 `BriefingGenerated` event + `BriefingsProjector` apply (uow)、失敗時 `BriefingFailed` event。Prompt template は `briefings/prompts.py` の `SYSTEM_PROMPT` / `USER_PROMPT_TEMPLATE` 定数で管理。external content は `<source id="...">...</source>` 形で wrap + "do not follow instructions" preamble (ADR-0015 §決定 (e) 準拠) | B |
| B4 | `feat(cli): brief command` | `src/opshub/cli/brief.py` 新設。`opshub brief "<topic>" [--scope all] [--max-sources N] [--max-tokens N] [--save] [--format md\|json]`。`BriefingService.generate(...)` を呼び `cli/_render` で出力 (markdown は素のまま stdout、json は briefing record の JSON dump)。`--save` 指定時は Phase 1 の `workspace_markdown_service` 経由で `workspace/briefings/<topic-slug>-<briefing-id>.md` に書出 (slug 化は ASCII safe な簡易変換、`core/slug.py` を新規 helper として追加)。`[llm] backend = disabled` の場合は exit 2 + "configure [llm] backend (anthropic / openai)" 案内 | B |

### 2.3 Sub-issue C: Event-driven auto-embed (2 PR)

| # | Commit | 概要 | Sub-issue |
|---|---|---|---|
| C1 | `feat(services): auto-embed projector hook` | `services/embedding_service.py` 拡張: `EmbeddingService.embed_one_if_pending(entity_type, entity_id)` を public method 化 (`_iter_pending` の単一行版)。新規 `services/auto_embed_hook.py`: `AutoEmbedHook.maybe_embed(event)` が `event.entity_type` を見て該当 embeddable event (`TaskCreated` / `TaskTitleUpdated` / `DecisionMade` / `InboxItemRecorded` / `SourceObserved` / `SourceSummaryUpdated` 等) なら `embed_one_if_pending` を呼ぶ。`core/config.py` に `[embedding] auto: bool = False` 追加。`cli/app.py` の composition root で `auto=true` の時のみ hook を組み立てて各 service に inject (service 側は `event_hooks: list[EventHook]` を受け、event apply 後・UoW commit 後に foreach 呼出)。失敗は `EmbeddingFailed` event + log のみ (originating event は roll back しない、Phase 4 の sequenced-after-commit と同 semantic) | C |
| C2 | `feat(cli): embeddings drain command + status extension` | `cli/embeddings.py` 拡張: `opshub embeddings drain [--entity-type X]` を新設。`embed_pending` の thin wrapper だが「auto=true でも hook 漏れがあった場合の cleanup」 用途を docstring + `--help` で明示。`opshub embeddings status` を拡張し `auto: enabled/disabled` + `pending entities by type` を表示。auto=true 時に hook が登録されているかを confirm するための diagnostic line も追加 | C |

### 2.4 Sub-issue D: Phase 5 closeout (1 PR)

| # | Commit | 概要 | Sub-issue |
|---|---|---|---|
| D1 | `test: phase 5 end-to-end + docs` | `tests/integration/test_phase5_lifecycle.py`: mocked LLMClient + mocked Embedder で `opshub task add` → `opshub embeddings rebuild` (or auto-embed) → `opshub brief "<topic>"` → `briefings` projection に row が入る → `opshub brief --save` で workspace markdown 生成、までの連鎖を CLI 経由で検証。`tests/integration/test_phase5_briefing_atomicity.py`: LLM 失敗時に `BriefingFailed` event が記録され `briefings` projection は変更されないことを test。README / AGENTS.md / CLAUDE.md / docs/principles.md / docs/architecture.md / docs/repository-structure.md / `docs/decisions-log.md` に Phase 5 完了状態反映 (principles §9 で Phase 5 = ✅ Complete、§Open Q #1 を §確定済み に移動、architecture §2.7 (新規) Briefing layer 追記)。ADR-0015 は本 PR で Accepted のまま (A1 で初回 Accepted、D1 で Validation セクション追加) | D |

= 合計 **12 PR** (A 5 + B 4 + C 2 + D 1)。

**Wave 構成** (DAG):

```text
Wave 1: A1 (ADR-0015) → 1 並列 (sequential foundation、direction を確定)
Wave 2: A2 (LLM Protocol freeze) + B1 (briefing events) + C1 (auto-embed hook、独立) → 3 並列
Wave 3: A3 (Anthropic) + A4 (OpenAI) + B2 (briefings projection、B1 依存) + C2 (drain CLI、C1 依存) → 4 並列 (drive 並列度上限)
Wave 4: A5 (LLM factory、A2 + A3 + A4 依存)
Wave 5: B3 (BriefingService、A5 + B1 + B2 依存)
Wave 6: B4 (brief CLI、B3 依存)
Wave 7: D1 (closeout、全 sub-issue 依存)
```

= 7 wave。Wave 3 が 4 並列で drive `min(4, wave 内タスク数)` 上限に張り付く。

## 3. 各 Sub-issue の Definition of Done

### Sub-issue A — LLM ADR + Protocol freeze + concrete backends

- [ ] ADR-0015 が Accepted + decisions-log.md に entry 追加
- [ ] `LLMClient` Protocol が `runtime_checkable` で freeze、freeze test が signature を pin
- [ ] `AnthropicLLMClient` / `OpenAILLMClient` が SDK mock 下で round-trip test 通過
- [ ] `[llm-anthropic]` / `[llm-openai]` extras が `pyproject.toml` に追加、`uv sync --extra llm-anthropic` / `--extra llm-openai` で install 可
- [ ] `core/secrets` に `llm:<name>:api_key` 経路が wired、env var override (`OPSHUB_LLM_<NAME>_API_KEY`) が動く test pinning 済
- [ ] `opshub connector auth set llm:anthropic` 等で API key を keyring 保存可能
- [ ] `build_llm_client(settings).complete([...])` が backend を切替えて end-to-end 動作 (mock)

### Sub-issue B — Briefing domain

- [ ] `Phase5Event` 追加 + `AllEvent` 拡張が type-safe (mypy + pyright で error なし)
- [ ] `briefings` projection が migration 0014 で作成され、`alembic upgrade head` で apply 可能
- [ ] `BriefingsProjector.apply(BriefingGenerated, conn)` が冪等 (同 briefing_id を 2 回 apply しても row 重複なし)
- [ ] `BriefingService.generate(...)` が LLM 失敗時に `BriefingFailed` event のみ append + projection は変更されない (atomicity test)
- [ ] `opshub brief "<topic>"` が markdown を stdout に出力、`--save` で `workspace/briefings/` に file 生成
- [ ] Prompt template の external content block が明示 delimiter で wrap、"do not follow instructions" preamble 付き (ADR-0015 §決定 (e) 準拠 test)

### Sub-issue C — Event-driven auto-embed

- [ ] `[embedding] auto = false` (default) で挙動 unchanged (既存 Phase 4 test 全通過)
- [ ] `[embedding] auto = true` で `opshub task add ...` 直後に `embeddings status` の embedded 件数が +1
- [ ] Auto-embed 失敗時に originating event は commit 済、`EmbeddingFailed` event が記録、次の `embeddings drain` / `embeddings rebuild` で retry 成功
- [ ] `opshub embeddings drain` が `embed_pending` の thin wrapper として動作 + status に `auto: enabled/disabled` 表示

### Sub-issue D — Phase 5 closeout

- [ ] `tests/integration/test_phase5_lifecycle.py` が CLI 経由の end-to-end (task add → brief → save markdown) を mock LLM で検証
- [ ] README に `opshub brief` 追記、AGENTS.md / CLAUDE.md に Phase 5 完了反映
- [ ] principles.md §Open Q #1 (LLM 利用方針) を §確定済 に移動、ADR-0015 reference 追加
- [ ] architecture.md に §2.7 (新規) Briefing layer 追記
- [ ] repository-structure.md の Phase 5 ファイル annotation を `[P1+2+3+4+5]` に更新

## 4. Open Questions

Phase 5 着手時点で未確定、本 plan 内で確定すべきもの (Phase 4 A1 / D1 の段取り模倣):

1. **Local LLM backend (`llama.cpp` / `Ollama`) の優先度** — Phase 5 MVP は Anthropic + OpenAI のみ。`local` backend (cost zero + offline) は Phase 5.x。本 plan の確定事項 §1 #1 に追記済、ADR-0015 で正式採択
2. **Prompt versioning** — Prompt 定数を変更したとき過去 briefing と integrity を保つか。MVP は briefing record に `model_id` / `model_version` を残すのみで prompt は記録しない (Phase 5.x で `prompt_id` / `prompt_version` 追加検討)。本 plan で確定

Phase 5 内では確定しなくてよい (Phase 5.x / 6 持ち越し):

1. **Briefing 再生成 / cache** — 同 topic で再 brief すると毎回 LLM call (Phase 5 MVP)。Phase 5.x で `--reuse-if-fresh` cache flag 検討
2. **Narrow scope briefing** — `scope=task:<id>` / `scope=project:<id>` は Phase 5.x (RecallService の scope filter 拡張が前提)
3. **Multi-machine sync** — principles.md §Open Q #5 は引き続き未確定。Phase 5.x もしくは別 Phase で着手
4. **Slack / MS365 / Box connector** — Phase 5.x で別 plan (Phase 3 framework + ADR-0010 contract 再利用)
5. **`links` projection 本実装** — Phase 5.x、graph queries CLI が必要になったら起こす

## 5. Phase 5.x / 6 outlook

Phase 5 完了直後の候補:

- **Connectors Wave 2** (Slack / Microsoft 365 / Box、Phase 3.x 名残)
- **Local LLM backend** (`llama.cpp` / `Ollama`、ADR-0015 §決定 (a) で Phase 5.x 明示)
- **Briefing cache + narrow scope** (`--reuse-if-fresh` + `scope=task:<id>` / `scope=project:<id>`)
- **`links` projection 本実装** (`SourceReferenced` 消費 + graph queries CLI)
- **Multi-machine sync** (principles.md Open Q #5: litestream / Turso / export-import)
- **Briefing → action loop** (briefing 中で「次やるべき task」を LLM が抽出し `opshub task add` を提案する、principles.md §9 Phase 6 候補)

Phase 5.x / 6 着手時に連動して見直すべき docs: principles.md §1 (Local-first、LLM API 依存とのバランス) / §6 (External Content Min、LLM への外部 body 露出範囲) / ADR-0005 / ADR-0009 (Multi-Agent Neutrality、LLM 抽象化との整合) / ADR-0015 (本 phase で新設、Validation を D1 で追記)。
