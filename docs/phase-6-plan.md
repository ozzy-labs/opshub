# Phase 6 Implementation Plan

> Status: Draft (planning). Last reviewed: 2026-05-17. Scope: Action loop layer = LLMClient Protocol extension (structured output / tool_use) + Proposal domain (`opshub propose` で LLM が candidate task/decision を提案 → operator 承認で apply) + Local LLM backend (Ollama)。`llama.cpp` direct binding / multi-machine sync / Connectors Wave 2 / `links` projection 本実装は Phase 6.x で別途。

Phase 6 の目的は **Action loop layer** を Phase 1-5 の foundation 上に追加すること。Phase 5 で briefing (passive memory readout) が動いたので、次は LLM に「次やるべき task / 残すべき decision」の candidate を structured output で提案させ、operator が承認したものを `opshub task add` / `opshub decision record` 相当の path で apply する経路を作る。これにより OpsHub は passive な operational memory から **active な assistant** に進化する。

同時に ADR-0015 §決定 (a) で Phase 5.x 持ち越しになっていた **Local LLM backend (Ollama 経由)** を本 phase で実装し、principles.md §1 (Local-first) と LLM 利用の tension を緩和する。Ollama は OpenAI 互換 chat completions API + tool_use を備えているため、Phase 5 で確立した `LLMClient` Protocol + Phase 6 で追加する structured output 拡張をそのまま流用できる。`llama.cpp` direct (python binding) は依存サイズ + 配布性の懸念から Phase 6.x 持ち越し。

Phase 5 で freeze 済みの `LLMClient` Protocol は **拡張のみ可** (rename / 削除禁止)。本 phase で `complete_structured(messages, schema, ...) -> StructuredResponse` を追加し、freeze test を拡張する。

## 1. 着手前に解消する TODO

Phase 5 完了時点で Phase 6 着手前に解消が必要な事項は **なし**。Phase 1-5 で確立した実装契約 (uow_factory / `EventStore.append(event, conn)` / `Projector.apply(event, conn)` / `projections/registry.all_projections()` SSOT / `AllEvent` discriminated union / `cli/* import` whitelist / atomic failing-projector test / `core/secrets` + ADR-0014 token storage / Pluggable backend Protocol freeze + factory pattern / `core/sanitise.sanitise_error_message` / prompt injection mitigation (delimiter wrap + html.escape)) は Phase 6 も全て継承する。

**確定済み事項** (Phase 6 着手前に確定):

1. **Scope の絞り込み** — Phase 6 MVP = ADR-0016 (Action loop / structured output) + LLMClient Protocol 拡張 (`complete_structured`) + Anthropic / OpenAI 具象拡張 + Ollama backend 新設 + Proposal domain (events + projection + service + CLI)。`llama.cpp` direct binding / multi-machine sync / Connectors Wave 2 / `links` projection 本実装は Phase 6.x で別 plan
2. **Default LLM backend** — `[llm] backend = "disabled"` の Phase 5 default を維持。Ollama は `[llm.backend] = "ollama"` で opt-in、daemon URL (default `http://localhost:11434`) を `[llm.ollama] host` で設定可能
3. **推奨 Local model** — `ollama` backend default は `llama3.2:3b` (2GB、CPU でも動作、briefing / propose の品質確認は本 phase 着手時に Validation セクションで報告)。operator が `[llm.ollama] model` で上書き可能
4. **Structured output 機構** — Anthropic は `tool_use` content block、OpenAI / Ollama は `tools=` function calling (OpenAI 互換 chat completions API)。schema は Pydantic v2 model を SSOT とし、各 client が JSON schema に serialize して LLM に渡す。response の `tool_calls` / `tool_use` block を JSON parse → Pydantic validate で構造化
5. **Candidate types (MVP)** — `task` と `decision` の 2 種類のみ。`inbox_item` / `source` candidate は Phase 6.x で `connector sync` 経由の auto-detect と統合する際に再設計
6. **Human-in-the-loop 必須** (ADR-0004 整合) — proposal 生成は automated、apply は **必ず operator-triggered**。`opshub propose apply <id> <index>` で operator が明示承認した candidate のみ実 entity 化される。auto-apply は Phase 6.x でも導入しない (ADR-0016 §決定 で明示)
7. **Idempotent apply** — 同 `(proposal_id, candidate_index)` に対する 2 回目の apply は no-op + diagnostic (Phase 4 EmbeddingService の re-run 冪等性と同 semantic)。`ProposalApplied` event の `aggregate_id = proposal_id` で natural key 強制
8. **Apply 経路** — `ProposalService.apply(proposal_id, candidate_index)` は既存の `TaskService.add_task` / `DecisionService.record_decision` を呼び出す。LLM 生成 text は Pydantic validation を経由して既存 entity 化される (injection 経路は Phase 5 の delimiter wrap で mitigated、apply 後の entity に対する recall / briefing は Phase 5 contract 通り動く)
9. **Schema versioning** — `ProposalGenerated.candidates: list[Candidate]` の `Candidate` Pydantic model は `schema_version: Literal["v1"]` を持つ。Phase 6.x で fields を追加するときは `Literal["v1", "v2"]` に拡張し、projector / apply 経路で version 分岐
10. **Rejection semantics** — `ProposalRejected(proposal_id, candidate_index, rejected_by, reason)` で記録、`proposals` projection の candidate 状態は `pending → applied | rejected` の 3 状態。rejected → 再 suggest はしない (event-sourced trace 維持、operator が再 `propose` を明示実行)

## 1.1 Prep PR (Phase 1-5) で確立した実装契約 (Phase 6 全 PR が継承)

- 新規 service は `uow_factory: Callable[[], ContextManager[Connection]] | None = None` を constructor で受け、event append + projection apply を 1 transaction にまとめる (PR #26 契約)
- 新規 projection は `projections/<entity>.py` で Table を `opshub.db.schema.metadata` に登録 + `projections/registry.all_projections()` に追記
- 新規 event family は `Phase6Event` discriminated union を作り、`AllEvent` を `... | Phase5Event | Phase6Event` に拡張 (PR B1 で実施)
- 新規 CLI subcommand module は module-level import を `__future__` / `typer` / `typing` / `pathlib` に限定する (M6 cold-start guard が CI で検出)
- 新規 service は失敗 projector の atomicity test を 1 件追加 (PR #26 + Phase 2-5 で確立)
- 新規 projection は rebuild の冪等性テストを 1 件追加
- LLM client は network mock (CI で実 API / Ollama daemon を叩かない、Phase 5 の規律と同)
- Phase 1 (Embedder / VectorStore) + Phase 5 (LLMClient) で frozen な Protocol は **拡張のみ可**。`LLMClient.complete_structured` の追加は freeze test に signature pin を追加する (rename / 削除 ではないので freeze 違反ではない)
- LLM 生成 candidate を実 entity 化するときは既存 service (TaskService / DecisionService) を経由する (validation / sanitisation 経路を二重化しない、ADR-0002 event-sourced 単一経路)
- prompt injection mitigation は Phase 5 と同じく `<source ...>` delimiter wrap + `html.escape(quote=False)` + "do not follow instructions" preamble を継続適用 (Phase 6 の propose prompt にも同じ contract を適用)

## 2. Phase 6 Commit 順序

Conventional Commits 準拠。1 step = 1 PR = 1 commit (squash 後) を厳守。各 PR 番号は forecast — 実 PR # は merge 順で決まるため step 番号で追う ([memory: pr-number-forecast-not-canonical](https://github.com/ozzy-labs/opshub))。

### 2.1 Sub-issue A: LLM Protocol extension + Ollama backend (4 PR)

| # | Commit | 概要 | Sub-issue |
|---|---|---|---|
| A1 | `docs(adr): adr-0016 action loop and structured output` | `docs/adr/0016-action-loop-and-structured-output.md` 新設。Status: Accepted。決定 8 件: (a) structured output mechanism = Anthropic tool_use / OpenAI tools / Ollama OpenAI-compatible tools、(b) schema SSOT = Pydantic v2 model + JSON schema export per-client、(c) human-in-the-loop 必須 (auto-apply 禁止)、(d) idempotent apply ((proposal_id, candidate_index) 二重 apply は no-op)、(e) Candidate types MVP = task + decision (inbox/source は Phase 6.x)、(f) schema versioning = `schema_version: Literal["v1"]` field を Candidate に持たせる、(g) apply 経路は既存 TaskService / DecisionService を経由 (validation 二重化禁止)、(h) Local LLM (Ollama) を本 phase で導入し ADR-0015 §決定 (a) deferred を closeout。Validation セクションは C1 closeout PR で追記。decisions-log.md に entry 追加 | A |
| A2 | `feat(llm): extend LLMClient with complete_structured` | `src/opshub/llm/client.py` 拡張: `LLMClient` Protocol に `complete_structured(messages, *, schema: type[BaseModel], max_tokens, temperature=0.2) -> StructuredResponse` method を追加。`StructuredResponse(parsed: BaseModel, model_id, model_version, tokens_in, tokens_out)` を frozen dataclass で新設。`tests/unit/llm/test_protocol_freeze.py` に signature pin を追加 (既存 `complete` signature は touch しない、追加のみが Phase 5 freeze 契約)。Pydantic model → JSON schema 変換 helper (`opshub.llm.schema.pydantic_to_tool_schema`) も同 PR で追加 (Anthropic / OpenAI / Ollama が共通利用) | A |
| A3 | `feat(llm): structured output for Anthropic + OpenAI` | `src/opshub/llm/anthropic_client.py` + `src/opshub/llm/openai_client.py` 拡張: `complete_structured` を実装。Anthropic は `tool_use` content block を抽出 → JSON parse → Pydantic validate、OpenAI は `response.choices[0].message.tool_calls[0].function.arguments` を JSON parse → Pydantic validate。共通 helper を `opshub.llm.schema` に置き両 client から call。test は SDK mock で構造化応答を返し、parse + validate を検証。tool_use 不在 / JSON parse 失敗 / Pydantic validation 失敗の error 経路も pin | A |
| A4 | `feat(llm): OllamaLLMClient (local backend)` | `src/opshub/llm/ollama_client.py` 新設。Ollama daemon の OpenAI 互換 endpoint (`<host>/v1/chat/completions`) を `httpx` 経由で叩く。`[llm-ollama]` extras (`httpx>=0.27`、既存ならば不要) を `pyproject.toml` に追加、`uv lock` 更新。`OllamaLLMClient(model_id="llama3.2:3b", host="http://localhost:11434", ...)` が `LLMClient` Protocol (`complete` + `complete_structured` 両方) を satisfy。API key は不要 (local だから、`core/secrets` 経由しない)。host は `[llm.ollama] host` で設定可能。daemon 不在時は `ConfigError("Ollama daemon not reachable at <host>; install ollama and run 'ollama pull llama3.2:3b'")` を fail-fast で raise。`build_llm_client(settings)` factory に `"ollama"` branch を追加 (`opshub.llm.factory`)。`core/config.py` の `LLMSettings.backend: Literal["disabled", "anthropic", "openai", "ollama"]` に extend。test は `httpx_mock` (extras 既存 or `respx`) で daemon mock | A |

### 2.2 Sub-issue B: Proposal domain (4 PR)

| # | Commit | 概要 | Sub-issue |
|---|---|---|---|
| B1 | `feat(domain): proposal events` | `src/opshub/domain/events/proposal.py` 新設。5 event 型: `ProposalRequested(proposal_id, topic, scope, briefing_id: str | None, requested_by)` (bracket、`aggregate_id=proposal_id`) / `ProposalGenerated(proposal_id, topic, scope, candidates: list[Candidate], model_id, model_version, tokens_in, tokens_out)` / `ProposalApplied(proposal_id, candidate_index, applied_entity_type: Literal["task","decision"], applied_entity_id, applied_by)` / `ProposalRejected(proposal_id, candidate_index, rejected_by, reason: str | None)` / `ProposalFailed(proposal_id, topic, scope, model_id, error_message)`。`Candidate` Pydantic model は `kind: Literal["task","decision"]` + `schema_version: Literal["v1"]` + kind 別 payload (`TaskCandidatePayload(title, description?, priority?)` / `DecisionCandidatePayload(text, rationale?)`) の discriminated union。`Phase6Event` discriminated union 新規、`AllEvent` を `... | Phase6Event` に拡張。`ProposalFailed.error_message` は `core.sanitise.sanitise_error_message` 経由で記録 (Phase 5 B1 で extract した helper を再利用) | B |
| B2 | `feat(projections): proposals projection + migration 0015` | `src/opshub/projections/proposals.py` 新設。`proposals_table` (id PK / topic / scope / briefing_id nullable / candidates JSON / candidate_states JSON (`list["pending" \| "applied" \| "rejected"]`) / model_id / model_version / tokens_in / tokens_out / generated_at) を `metadata` に登録 + `registry.all_projections()` に追記。Migration `0015_create_proposals_table.py` (revision `0015`, down_revision `0014`)。`ProposalsProjector.apply(event, conn)`: `ProposalGenerated` で INSERT OR REPLACE / `ProposalApplied` で candidate_states[idx]=applied / `ProposalRejected` で candidate_states[idx]=rejected / `ProposalRequested` + `ProposalFailed` は events 表のみ。冪等性 test + atomic failing-projector test を 1 件追加 | B |
| B3 | `feat(services): proposal service` | `src/opshub/services/proposals/__init__.py` + `src/opshub/services/proposals/service.py` + `src/opshub/services/proposals/prompts.py` 新設。`ProposalService.generate(topic, *, scope="all", from_briefing_id=None, max_candidates=5, max_tokens=2000) -> Proposal`: ① `ProposalRequested` event (uow) ② RecallService で context 抽出 (or briefing markdown を直接 LLM に渡す from_briefing_id 経路) ③ `<source>` delimiter wrap + html.escape (Phase 5 D1 follow-up と同 contract) ④ `LLMClient.complete_structured(messages, schema=ProposalCandidates, ...)` 呼出 ⑤ 成功時 `ProposalGenerated` event + projector apply (1 uow)、失敗時 `ProposalFailed` event (sanitised)。`ProposalService.apply(proposal_id, candidate_index) -> tuple[str, str]`: ① projection から candidate を読み state=pending を確認 (idempotent guard) ② kind に応じて `TaskService.add_task(...)` or `DecisionService.record_decision(...)` を呼び新 entity 作成 ③ `ProposalApplied` event + projector apply (1 uow、新 entity 生成は別 service が別 event を出すので結合的に 2 event 1 commit chain)。`reject(proposal_id, candidate_index, reason=None)` も対称的に。`apply` の idempotency: 既に applied/rejected の候補に再 apply → `OpsHubError("candidate already <state>")` で fail-fast | B |
| B4 | `feat(cli): propose command` | `src/opshub/cli/propose.py` 新設。サブコマンド 4 つ: `opshub propose generate "<topic>" [--from-briefing <id>] [--max-candidates N] [--max-tokens N] [--format md\|json]` / `opshub propose list [--state pending\|applied\|rejected] [--limit N]` / `opshub propose apply <proposal-id> <candidate-index>` / `opshub propose reject <proposal-id> <candidate-index> [--reason "..."]`。`generate` は ProposalService.generate を呼び candidate 一覧を render (`cli/_render.render_proposal_md` / `render_proposal_json` を新設)、operator が next-step として `apply` / `reject` を実行できるよう candidate index + 一行 summary を出力。`disabled` backend は exit 2。M6 cold-start guard 順守 (module-level import は `__future__` / `typer` / `typing` / `pathlib` のみ) | B |

### 2.3 Sub-issue C: Phase 6 closeout (1 PR)

| # | Commit | 概要 | Sub-issue |
|---|---|---|---|
| C1 | `test: phase 6 end-to-end + docs` | `tests/integration/test_phase6_lifecycle.py`: mocked LLMClient で `opshub task add` → `opshub embeddings rebuild` → `opshub brief "<topic>"` → `opshub propose generate --from-briefing <id>` → candidate list 出力 → `opshub propose apply <id> 0` で新 task 生成 → `opshub task list` で確認、までの連鎖を CLI 経由で検証。`tests/integration/test_phase6_propose_atomicity.py`: LLM 失敗 / projector 失敗 / disabled backend / 既 applied candidate への再 apply、4 経路の atomicity + exit code。`tests/integration/test_phase6_ollama_lifecycle.py` (optional): Ollama daemon mock 経由で `[llm] backend = "ollama"` 設定下の propose flow が動くことを pin (httpx_mock で API 完全 mock、daemon 実起動はしない)。docs 更新: README に `opshub propose` 4 サブコマンド追記、AGENTS.md / CLAUDE.md / principles.md (§9 Phase 6 = ✅ Complete、§Open Q #5 (Multi-machine sync) のみ残置を明記) / architecture.md §2.8 (新規) Action loop layer 追記 / repository-structure.md の Phase 6 ファイル annotation を `[P6]` / 既存 modified ファイルは `[P1+2+3+4+5+6]` に / decisions-log.md に Phase 6 entry。ADR-0015 §決定 (a) deferred を closeout (Ollama 実装済を Known Limitations から削除、Validation §(a) に Ollama 追記)、ADR-0016 に Validation セクション追記 | C |

= 合計 **9 PR** (A 4 + B 4 + C 1)。

**Wave 構成** (DAG):

```text
Wave 1: A1 (ADR-0016) → 1 並列 (sequential foundation、direction 確定)
Wave 2: A2 (LLM Protocol 拡張) + B1 (proposal events) → 2 並列 (互いに独立)
Wave 3: A3 (Anthropic + OpenAI structured) + A4 (Ollama backend) + B2 (proposals projection、B1 依存) → 3 並列
Wave 4: B3 (ProposalService、A2 + B1 + B2 依存。A3 / A4 はどちらか 1 つ merge 済なら start 可、両方 merge 済が望ましい)
Wave 5: B4 (propose CLI、B3 依存)
Wave 6: C1 (closeout、全 sub-issue 依存)
```

= 6 wave。Wave 3 が 3 並列で最大。Phase 5 (7 wave / 12 PR) より小さい構成だが、Phase 6 は LLM Protocol が既に Phase 5 で freeze 済のため新規 foundation 作業が薄い。

## 3. 各 Sub-issue の Definition of Done

### Sub-issue A — LLM Protocol extension + Ollama backend

- [ ] ADR-0016 が Accepted + decisions-log.md に entry 追加
- [ ] `LLMClient.complete_structured` が Protocol に追加され `runtime_checkable` を維持、freeze test が新旧両 signature を pin
- [ ] `AnthropicLLMClient.complete_structured` / `OpenAILLMClient.complete_structured` が SDK mock 下で round-trip test 通過 (Pydantic schema → JSON schema → tool_use/tools → response parse → Pydantic instance)
- [ ] `OllamaLLMClient` が `LLMClient` Protocol (両 method) を satisfy、httpx_mock で round-trip test
- [ ] `[llm-ollama]` extras が `pyproject.toml` に追加、`uv sync --extra llm-ollama` で install 可
- [ ] `build_llm_client(settings)` factory が `"ollama"` branch を持ち、daemon 不在時に `ConfigError` を fail-fast で raise
- [ ] `core/config.py` `LLMSettings.backend` が `Literal["disabled", "anthropic", "openai", "ollama"]` に拡張済

### Sub-issue B — Proposal domain

- [ ] `Phase6Event` 追加 + `AllEvent` 拡張が type-safe (mypy + pyright で error なし)
- [ ] `proposals` projection が migration 0015 で作成され `alembic upgrade head` で apply 可能
- [ ] `ProposalsProjector.apply(ProposalGenerated, conn)` が冪等
- [ ] `ProposalService.generate(...)` が LLM 失敗時 `ProposalFailed` のみ append + projection 変更なし (atomicity test)
- [ ] `ProposalService.apply(...)` が既存 TaskService / DecisionService を経由して新 entity 化、`ProposalApplied` event を append、candidate_state が `applied` に遷移
- [ ] `apply` の idempotency: 既 applied/rejected な candidate に再 apply → `OpsHubError("candidate already <state>")`
- [ ] `opshub propose generate / list / apply / reject` 4 subcommands が動作
- [ ] Prompt template の external content block が delimiter wrap + html.escape + preamble 付き (Phase 5 D1 follow-up と同 contract、本 phase の generate prompt にも適用)

### Sub-issue C — Phase 6 closeout

- [ ] `tests/integration/test_phase6_lifecycle.py` が CLI 経由 e2e (task add → embed → brief → propose generate → propose apply → 新 task 出現) を mock LLM で検証
- [ ] `tests/integration/test_phase6_propose_atomicity.py` が 4 失敗経路の atomicity 検証
- [ ] (optional) `tests/integration/test_phase6_ollama_lifecycle.py` が httpx_mock で Ollama 経路 e2e
- [ ] README に `opshub propose` 4 subcommands 追記
- [ ] AGENTS.md / CLAUDE.md に Phase 6 完了反映
- [ ] principles.md §9 Phase 6 = ✅ Complete、§Open Q (残置 = Multi-machine sync のみ) 明示
- [ ] architecture.md §2.8 (新規) Action loop layer 追記
- [ ] repository-structure.md の Phase 6 file annotation
- [ ] decisions-log.md に Phase 6 entry
- [ ] ADR-0015 §決定 (a) deferred → Validation §(a) Ollama 追記 + Known Limitations から Local LLM 削除
- [ ] ADR-0016 に Validation 追記 (本 phase で実装した test ファイルへの reference)

## 4. Open Questions

Phase 6 着手時点で未確定、本 plan 内で確定すべきもの:

1. **`llama.cpp` direct (python binding) を MVP に含めるか** — Ollama 経由で 90% の use case が covered + `llama-cpp-python` は OS-specific binary + heavy install。Phase 6 MVP は Ollama のみ、`llama.cpp` direct は Phase 6.x。本 plan §1 #1 に追記済、ADR-0016 で正式採択
2. **Proposal apply 経路の event chain 観測性** — `ProposalApplied` event と `TaskCreated` event の link は projection 上の `applied_entity_id` で表現する (graph 的 link ではなく、apply→created の trace は projection JOIN で reconstructable)。本 plan で確定、`links` projection 本実装 (Phase 6.x candidate) と統合の余地は残す
3. **Schema versioning の migration path** — Phase 6.x で `Candidate.schema_version: Literal["v1", "v2"]` に拡張する際、既存 v1 candidates を projector で v2 に migrate するか、両 version を読み分けるか。本 plan では「両 version を読み分ける、migration は不要」を default、ADR-0016 で詳細 pin

Phase 6 内では確定しなくてよい (Phase 6.x / 7 持ち越し):

1. **Auto-apply mode** — `opshub propose --auto-apply` のような high-confidence candidate を自動 apply するモードは Phase 6.x も導入しない (ADR-0016 §決定 (c) human-in-the-loop 原則)。再評価は Phase 7+
2. **`inbox_item` / `source` candidate types** — Phase 6 MVP は task + decision のみ。inbox auto-categorize や source summarize-and-add は Phase 6.x
3. **Multi-step proposal (action plan)** — 「task A → task B → decision C」のような順序付き提案は Phase 6.x。MVP は flat list
4. **Local LLM のモデル選定 + 品質 validation** — Phase 6 MVP は `llama3.2:3b` を default、品質比較 (Anthropic Haiku vs OpenAI mini vs Ollama llama3.2:3b) は ADR-0016 Validation セクションで実機検証結果を簡記、深い benchmark は Phase 6.x
5. **`llama.cpp` direct binding** — Phase 6.x で別 PR / 別 ADR
6. **Multi-machine sync** — principles.md §Open Q #5、本 phase でも未着手

## 5. Phase 6.x / 7 outlook

Phase 6 完了直後の候補:

- **`inbox_item` / `source` candidate types** (Phase 6 MVP の不足分)
- **`llama.cpp` direct backend** (Ollama 不要にしたい operator 向け、Phase 6.x)
- **Connectors Wave 2** (Slack / Microsoft 365 / Box、Phase 3.x 名残)
- **`links` projection 本実装** (`SourceReferenced` + `ProposalApplied → TaskCreated` 等の graph queries CLI)
- **Multi-machine sync** (principles.md §Open Q #5)
- **Briefing cache + narrow scope** (Phase 5.x、briefing reuse)
- **Proposal scoring / ranking** (LLM が candidate に confidence score を付与、UI で sort)
- **`opshub brief --propose` 一気通貫 CLI** (brief + propose を 1 コマンドで)

Phase 6.x / 7 着手時に連動して見直すべき docs: principles.md §1 (Local-first、Ollama 採用で部分緩和) / §6 (External Content Min、apply 経路で LLM 生成 text が entity 化される議論) / ADR-0004 (Agent Runtime Boundary、human-in-the-loop 原則の継続強化) / ADR-0009 (Multi-Agent Neutrality、Ollama 含めた 3 backend で確認) / ADR-0015 (本 phase で local backend 追加して closeout) / ADR-0016 (本 phase で新設、Validation を C1 で追記)。
