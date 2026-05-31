# 0016. Action Loop and Structured Output

- Status: Accepted (revised 2026-05-31 for Phase 12 H1 draft unification)
- Date: 2026-05-17 (initial); 2026-05-30 (Phase 10 §決定 (i)+(j)+(k) revision: reply_draft candidate / triage / style-source recall); 2026-05-31 (Phase 12 H1 §決定 (l) revision: draft 系統一方針 + mode argument 射程 + Candidate discriminated union freeze)
- Deciders: ozzy

## Context

Phase 5 で導入した **Briefing layer** (`opshub brief "<topic>"`) は OpsHub を passive な operational memory readout として完成させた。Phase 6 ではその上に **Action loop layer** を載せる。具体的には `opshub propose generate "<topic>"` で LLM に「次にやるべき task / 残すべき decision」の candidate を structured output で提案させ、operator が `opshub propose apply <id> <index>` で明示承認した candidate のみを実 entity 化する。これにより OpsHub は「読み取り専用の memory」から「assistant」に進化する。本 ADR はその経路の **8 つの設計判断** を着手前に pin する。

第一の論点は **structured output の wire format** である。Briefing は free-form markdown を返せばよかったので Phase 5 の `LLMClient.complete(messages, *, max_tokens, ...)` で十分だった。一方 propose は LLM の出力を `Candidate.kind` + `Candidate.payload` という Pydantic schema に validate する必要があり、free-form markdown を regex で抽出する設計は brittle で、connector 追加 / model 変更ごとに parser を書き直す事態を招く。Phase 1 Embedder / Phase 5 LLMClient で確立した「Pluggable Protocol + 具象は SDK の native 機構を利用」の方針を踏襲し、Anthropic は `tool_use` content block、OpenAI / Ollama は `tools=` function calling (OpenAI 互換 chat completions API) を使う。

第二の論点は **schema の SSOT (Single Source of Truth)** である。Anthropic SDK は `anthropic.types.ToolParam`、OpenAI SDK は `tools=[{"type": "function", "function": {...}}]`、Ollama は OpenAI 互換 endpoint なので OpenAI と同形式。三者の wire format は微妙に異なるが、いずれも「JSON Schema」を中核に持つ。Pydantic v2 model の `model_json_schema()` を SSOT として、各 client が provider-native の tool schema 構造に wrap する設計を採る。これにより operator が Phase 6.x で新 candidate type を追加するときも Pydantic model に field を 1 つ足すだけで済む。

第三の論点は **human-in-the-loop の必然性** である。propose が生成する candidate は LLM の文字列を含み、operator が apply すると `task_table.description` / `decisions_table.text` 等の durable state に書き込まれる。LLM 生成テキストは prompt injection や hallucination の余地があり、ADR-0004 (Agent Runtime Boundary) の「agent / LLM の出力を OpsHub の durable state に書く前には信頼境界の review が要る」原則と直結する。本 ADR では **auto-apply mode を Phase 6 MVP に含めず、Phase 6.x 以降でも導入しない** ことを明示する。再評価には ADR-0004 を revisit する新 ADR が必要、というガードを置く。

第四の論点は **Local LLM 不在 (ADR-0015 §決定 (a) deferred) の closeout** である。ADR-0015 では `[llm-local]` extras を Phase 5.x に持ち越し、principles.md §1 (Local-first) との緊張を `disabled` default で暫定対応した。Phase 6 では Ollama を採用する。Ollama daemon の OpenAI 互換 endpoint (`<host>/v1/chat/completions`) は本 ADR §決定 (a) の `tools=` function calling を素のままサポートし、Anthropic / OpenAI と **同じ wire format で `complete_structured` を実装できる**。`llama.cpp` direct (python binding) は OS-specific binary install と ADR-0001 配布制約の問題が未解決のため Phase 6.x へ持ち越す。

第五の論点は **principles.md §1 (Local-first) と API LLM の整合** である。Ollama 導入で「Local-first のまま LLM Action loop を運用できる」経路が初めて開く。`[llm] backend = "ollama"` 設定下で `opshub propose generate` を実行するとき、外部 network egress は発生しない (localhost daemon への httpx call のみ)。ADR-0015 §Negative §1 で残していた「API backend 利用時の network 露出」を Ollama opt-in で **緩和** する。完全には消えない (operator が `anthropic` / `openai` を選んだ場合は Phase 5 と同じ外部呼び出しが発生する) ため、principles.md §Open Q #5 (Multi-machine sync) や同 §1 の緊張自体は本 ADR では closeout しない。Ollama 採択により ADR-0015 §決定 (a) deferred のみ closeout される。

第六の論点は **event-sourced trace の保全** である。ADR-0002 (Event-Sourced Architecture) は「event log は immutable、過去 event を rewrite しない」を原則とする。propose の Candidate schema は Phase 6.x で field 追加が想定される (e.g., `confidence: float` / `rationale_sources: list[str]`)。schema migration を **「過去 event の rewrite」で行うと event-sourced trace が壊れる** ため、`Candidate.schema_version: Literal["v1"]` field で version を pin し、Phase 6.x では `Literal["v1", "v2"]` に拡張、projector / apply 経路で version 分岐して両 version を inline で読む。in-place migration は **しない** ことを契約として置く。

## Decision

OpsHub の Action loop layer を **Pluggable structured-output 拡張 + 既存 entity service 経由の apply + human-in-the-loop 必須** として設計する。Phase 6 で `LLMClient.complete_structured` を Protocol に追加し、Anthropic / OpenAI / Ollama 3 backend で実装する。ADR-0015 §決定 (a) deferred (Local LLM) は本 ADR §決定 (h) で closeout する。

### (a) Structured output mechanism = provider-native tool calling

Anthropic / OpenAI / Ollama の **3 backend すべてで** provider-native の tool calling (function calling) を使う。

| Backend | 機構 | API |
|---|---|---|
| Anthropic | `tool_use` content block | `anthropic.messages.create(..., tools=[ToolParam(...)], tool_choice={"type": "tool", "name": "..."})` |
| OpenAI | `tools=[{"type": "function", ...}]` + `tool_choice={"type": "function", ...}` | `openai.chat.completions.create(..., tools=[...], tool_choice={...})` |
| Ollama | OpenAI 互換 `tools=` (chat completions API) | `httpx.post("<host>/v1/chat/completions", json={..., "tools": [...], "tool_choice": {...}})` |

Response 経路:

- Anthropic: `response.content[i].input` (block.type == `"tool_use"`) を JSON parse → Pydantic validate
- OpenAI: `response.choices[0].message.tool_calls[0].function.arguments` (str) を JSON parse → Pydantic validate
- Ollama: OpenAI 互換 endpoint なので OpenAI と同じ response shape

採用理由:

- 3 backend ともに SDK / 互換 API で **production-tested**。free-form markdown + regex 抽出よりはるかに堅牢
- Pydantic schema → JSON schema → provider-native tool definition の変換が機械的に書け、新 Candidate type 追加時に parser を書く必要が無い
- grammar / JSON-mode constrained decoding (一部 SDK で提供) は provider-specific で portability が劣る。tool calling は 3 backend で同一の概念モデル
- Ollama が **OpenAI 互換 endpoint** を提供することで、OpenAI 用の serializer / parser を 1 行も変えずに流用できる (`opshub.llm.openai_client.OpenAILLMClient` 相当のロジックを `OllamaLLMClient` が継承的に利用)

### (b) Schema SSOT = Pydantic v2 model + 共通変換 helper

candidate の schema 定義は **Pydantic v2 model を SSOT** とする。各 LLM client は SSOT を provider-native の tool schema に serialize する。

```python
# src/opshub/llm/schema.py (Phase 6 A2 で新設)
from typing import Any
from pydantic import BaseModel

def pydantic_to_tool_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic v2 model -> JSON schema dict for provider tool definition.

    Anthropic / OpenAI / Ollama がいずれも accept する shape。各 client は
    必要に応じて name / description / wrapper を被せる。
    """
    return model.model_json_schema()
```

response の処理は対称的に:

- 各 client は `tool_calls[0].arguments` (str | dict) を `json.loads` してから `Model.model_validate(parsed)` で **Pydantic instance** を構築
- caller (`ProposalService`) には **typed `BaseModel` instance** が戻る (untyped dict ではない)
- validation 失敗時は `LLMValidationError` (新規、`OpsHubError` subclass) で fail-fast。caller は `ProposalFailed` event に sanitised error を記録

採用理由:

- Pydantic は OpsHub 全体の validation 経路 (event payload / config / CLI 引数) で既に SSOT。propose だけ別の schema 機構を入れるのは整合性違反
- `model_json_schema()` は Pydantic v2 で stable API、Anthropic / OpenAI どちらの JSON Schema 方言にも適合
- 共通 helper を 1 箇所に置くことで、Anthropic SDK の `ToolParam` / OpenAI SDK の `chat.completions` tool dict 形式の差異は各 client 側の thin wrapper で吸収できる (テストも共通 helper を 1 度 pin すれば足りる)

### (c) Human-in-the-loop 必須、auto-apply は Phase 6.x 以降も禁止 (Phase 10 で外部書き戻し境界を追加)

propose の lifecycle は **generate → operator review → apply / reject** の 3 段とする。

> **Phase 10 改訂 (2026-05-30)**: §決定 (i) で追加される `reply_draft` candidate も本決定の HITL 境界の中で動く。**`reply_draft` の apply は durable state (proposal candidate state の `pending → applied` 遷移) を変更するのみで、外部 SaaS (Slack / Outlook / GitHub PR comment 等) への送信・投稿は一切行わない**。ADR-0010 改訂 (Phase 10 で write-back を当面 scope 外と明記) と組で contract を構成し、「reply_draft apply が外部 HTTP call を発しない」test pin を §決定 (k) で要件化する (Phase 10 step E2 で `tests/unit/services/test_proposal_service.py` または `tests/integration/test_phase10_reply_draft_no_external_writeback.py` で固定)。connector の `post` / `send` / `comment` メソッドが存在しないことの assert で「経路がそもそも無い」を契約化する。

- **`opshub propose generate`** は automated (LLM call のみ)
- **`opshub propose apply <id> <index>`** は **必ず operator-triggered**
- `opshub propose reject <id> <index>` も同様 (operator が明示 reject)
- **auto-apply mode は Phase 6 MVP に含めない**、かつ **Phase 6.x 以降でも導入しない**

「Phase 6.x 以降でも導入しない」は強い宣言である。再評価には:

- 本 ADR (ADR-0016) を Superseded by する **新 ADR**
- 同時に **ADR-0004 (Agent Runtime Boundary) を explicitly revisit する記述**

を必要とする。「flag を 1 つ足すだけ」「configurable にする」では緩めない。

採用理由:

- candidate は LLM 生成テキスト (`title` / `description` / `text` / `rationale` 等) を含み、apply すると `tasks` / `decisions` projection の **durable state** に書き込まれる
- LLM 生成テキストは hallucination / prompt injection の影響を直接受ける。Phase 5 で delimiter wrap + html.escape による防御は実装したが、**完全防御ではない** (ADR-0015 §決定 (f) で明示)
- 「信頼境界 = operator の review」を 1 ステップ挟むことで、injection / hallucination が durable state に到達する経路を遮断する。ADR-0004 §決定 1 (agent / LLM の write 経路は CLI 経由のみ) を Action loop 文脈で具体化したもの
- auto-apply は短期的には UX 改善に見えるが、長期では「自動で task / decision が量産される」状況になり、operator の **記憶の信頼性** が損なわれる。OpsHub の core value (operational memory として信用できる) と直接矛盾する

### (d) Idempotent apply: `(proposal_id, candidate_index)` natural key

apply の冪等性を **natural key で強制** する。

- `ProposalApplied(proposal_id, candidate_index, ...)` event の natural key は `(proposal_id, candidate_index)` の組
- projection (`proposals_table.candidate_states`) は `pending → applied | rejected` の 3 状態
- 既に `applied` / `rejected` な candidate に対する 2 回目の apply は **`OpsHubError("candidate already <state>")` を raise** して fail-fast
- silent no-op はしない。CLI レイヤで `OpsHubError` を catch して exit 1 + 人間可読 message

採用理由:

- transient CLI crash → re-run の冪等性は Phase 4 EmbeddingService re-run と同じ semantic で operator の mental model を統一できる
- silent no-op だと「apply が走ったのか走らなかったのか」を CLI 出力から判別できない。明示的 error の方が観測性が高い
- 「state を 1 度遷移させたら 2 度目は error」という制約は event-sourced 設計と整合する (state machine の transition rule を event 側で強制)
- 並行 race (同 candidate に同時 apply 2 回) はファイル lock + DB transaction で防がれるが、本決定はそれと独立に「明示 state guard」を service 層で持つ

### (e) Candidate types MVP = `task` + `decision` (Phase 10 で `reply_draft` 追加)

Phase 6 MVP では **`task` と `decision` の 2 種類のみ** を candidate type とする。

> **Phase 10 改訂 (2026-05-30)**: §決定 (i) で `reply_draft` を 3 つ目の candidate kind として追加する。**`task` と `decision` の 2 種類のみ** という Phase 6 MVP 制約は Phase 10 で `task` + `decision` + `reply_draft` の 3 種類に拡張される (`inbox_item` / `source` は依然として候補化しない方針が継続。connector-authored entity を LLM が「propose」する経路の信頼モデルは未確定)。schema versioning は §決定 (f) の `Literal["v1", "v2"]` パターンで対応し、`reply_draft` の `schema_version` は v2 を pin する (Phase 6 v1 candidate は rewrite せず、reader が両 version を inline で読む)。

```python
# src/opshub/domain/events/proposal.py (Phase 6 B1 で新設)
class TaskCandidatePayload(BaseModel):
    title: str
    description: str | None = None
    priority: Literal["low", "medium", "high"] | None = None

class DecisionCandidatePayload(BaseModel):
    text: str
    rationale: str | None = None

class Candidate(BaseModel):
    kind: Literal["task", "decision"]
    schema_version: Literal["v1"] = "v1"
    payload: TaskCandidatePayload | DecisionCandidatePayload  # discriminated union
```

`inbox_item` / `source` は **Phase 6.x で別 PR / 別 ADR** で追加する。

採用理由:

- Phase 1-2 で operator-authored entity type として確立した `task` と `decision` から始めるのが最も自然。validation 経路 (`TaskService.add_task` / `DecisionService.record_decision`) が既に存在し、re-use できる (§決定 (g))
- `inbox_item` / `source` は **connector-authored** entity type (Phase 3-3.x)。LLM が「inbox に追加すべき外部 item」を提案する経路は connector sync auto-detect と統合する必要があり、信頼モデルが異なる (LLM が「外部 SaaS 上に X が存在する」と主張する場合、real-time 検証が必要)
- MVP scope を絞ることで Phase 6 着手から完了までを 9 PR 程度に収め、Phase 6.x で `inbox_item` / `source` を別建てで設計できる

### (f) Schema versioning: literal field + 両 version 読み分け、in-place migration なし

`Candidate.schema_version: Literal["v1"]` field を **すべての candidate に必須**で持たせる。

Phase 6.x で field を追加するときの migration policy:

- 新 schema = `Literal["v1", "v2"]` に拡張
- 新 candidate は `schema_version="v2"` で生成される
- **既存 v1 candidate は rewrite しない** (event log immutability)
- projector / apply 経路で `match candidate.schema_version` 分岐し、両 version を inline で読む

採用理由:

- ADR-0002 (Event-Sourced Architecture) の根本原則: event log は immutable、過去 event の payload は rewrite しない (audit trail / replay 整合性)
- in-place migration を許すと「いつ migration が走ったか」を再度 audit する必要が出て、event log の意味が崩れる
- Pydantic discriminated union (`Literal[...]`) は mypy / pyright で両 version を **type-safe に網羅チェック** できる
- 両 version 読み分けロジックは increment し続ければ Phase 6.x.y.z でも管理可能 (v1 + v2 + ... の match arm が増えるだけ。data migration の transactional 複雑さは発生しない)

### (g) Apply path goes through existing services (validation 非重複化)

`ProposalService.apply(proposal_id, candidate_index)` は entity event を **直接 append しない**。必ず既存 service を経由する。

```python
# src/opshub/services/proposals/service.py (Phase 6 B3 で新設、概念)
def apply(self, proposal_id: str, candidate_index: int) -> tuple[str, str]:
    candidate = self._read_candidate(proposal_id, candidate_index)  # state guard も
    match candidate.kind:
        case "task":
            entity_id = self.task_service.add_task(
                title=candidate.payload.title,
                description=candidate.payload.description,
                # ... Pydantic / sanitise は TaskService 側で走る
            )
            applied_entity_type = "task"
        case "decision":
            entity_id = self.decision_service.record_decision(
                text=candidate.payload.text,
                rationale=candidate.payload.rationale,
            )
            applied_entity_type = "decision"
    self._append_proposal_applied(proposal_id, candidate_index, applied_entity_type, entity_id)
    return applied_entity_type, entity_id
```

採用理由:

- `TaskService.add_task` / `DecisionService.record_decision` には Phase 1-2 で確立した **Pydantic validation + sanitisation + UoW** が組み込まれており、これを bypass すると validation が分散する
- LLM 生成テキストを直接 `TaskCreated` event に書く経路は、ADR-0005 (External Content Minimization) の summary 制約と Phase 5 の prompt injection mitigation の **両方を bypass** することになる
- 「validation を 2 系統持たない」(SSOT 原則) は OpsHub 全体で繰り返し採られている方針 (e.g., Phase 4 で `Embedder` Protocol を経由しない direct API 呼び出しを禁止した)
- apply は **2 つの event を 1 transaction にまとめる**: ① 既存 service が emit する `TaskCreated` / `DecisionRecorded` event + ② `ProposalApplied` event。両 event は同じ UoW (`uow_factory`) 経由で 1 commit。これは Phase 1-5 で確立した uow pattern と整合

### (h) Local LLM backend = Ollama (ADR-0015 §決定 (a) deferred を closeout)

Phase 6 で **`OllamaLLMClient` を追加** する (Phase 6 A4)。これにより ADR-0015 §決定 (a) で Phase 5.x に持ち越していた Local LLM backend が closeout される。

| 実装 | 種別 | 依存 (extras) | Phase | Status |
|---|---|---|---|---|
| `AnthropicLLMClient` | API | `[llm-anthropic]` | 5 | Accepted (ADR-0015) |
| `OpenAILLMClient` | API | `[llm-openai]` | 5 | Accepted (ADR-0015) |
| `OllamaLLMClient` | local (daemon) | `[llm-ollama]` (`httpx>=0.27`) | **6** | **本 ADR で Accepted** |
| `LlamaCppLLMClient` (`llama.cpp` direct) | local (in-process) | `[llm-llamacpp]` (未定) | **6.x (deferred)** | Phase 6.x |

Ollama 採用詳細:

- **OpenAI 互換 endpoint 利用**: `<host>/v1/chat/completions` を `httpx` で呼ぶ。`tools=` function calling が利用可能で、`complete` / `complete_structured` を **同じ wire format** で実装できる (OpenAI 用 serializer を re-use)
- **API key 不要**: localhost daemon 前提のため `core/secrets` を経由しない。`OPSHUB_LLM_OLLAMA_API_KEY` は未定義
- **設定**: `[llm.ollama] host = "http://localhost:11434"` / `[llm.ollama] model = "llama3.2:3b"` を default。operator が `config.toml` で上書き可
- **fail-fast**: daemon 不在 (connection refused / 404 / 5xx) は `ConfigError("Ollama daemon not reachable at <host>; install ollama and run 'ollama pull llama3.2:3b'")` を即時 raise
- **principles.md §1 (Local-first)**: `[llm] backend = "ollama"` 設定下では外部 network egress が **発生しない** (localhost のみ)。これは Phase 5 までの principles.md §1 緊張を **部分緩和** する (`anthropic` / `openai` 選択時は依然 external)

`llama.cpp` direct (python binding) は **本 ADR で Phase 6.x に持ち越す** ことを明示する:

- `llama-cpp-python` は OS-specific binary install (build from source / wheel availability に幅) で、ADR-0001 の Python distribution constraint (`uv tool install opshub` で配布) を破る
- model file 同梱 (4-30GB) も ADR-0001 の core size 制約に抵触
- Ollama 経由で 90% の use case が covered。`llama.cpp` direct が必要なケース (daemon を立てない GPU offload、bare-metal embedded) は Phase 6.x で別 ADR + extras で判断

## Phase 10 改訂 (Sub-issue E、2026-05-30)

Phase 10 Sub-issue E (返信下書き生成) で本 ADR に 3 つの追加決定を pin する。返信下書きの生成・triage・文体源は Phase 6 で確立した propose lifecycle (generate → operator review → apply / reject) を新規 candidate kind として再利用するだけで成立するため、新 ADR を立てず本 ADR の §決定 (i)+(j)+(k) として吸収する。Phase 6 で pin した §決定 (a)〜(h) はすべて継続する (構造化出力 = tool calling / Pydantic v2 SSOT / HITL 必須 / idempotent apply / 既存 service 経由 / schema versioning / Ollama backend)。

### (i) `ReplyDraftCandidatePayload` (`kind="reply_draft"`、`reply_to_source_id/type` 必須、schema v2)

`reply_draft` を 3 つ目の candidate kind として追加する。`task` / `decision` (Phase 6 MVP) と同じ Pydantic discriminated union (`Candidate`) に組み込み、`schema_version: Literal["v2"]` を pin する。

```python
# src/opshub/domain/events/proposal.py (Phase 10 step E2 で追加)
class ReplyDraftCandidatePayload(BaseModel):
    kind: Literal["reply_draft"] = "reply_draft"
    schema_version: Literal["v2"] = "v2"
    reply_to_source_id: str = Field(min_length=26, max_length=26)  # ULID
    reply_to_source_type: str = Field(min_length=1, max_length=50)
    body: str = Field(min_length=1, max_length=8000)
    subject: str | None = Field(default=None, max_length=500)
```

要点:

- **`reply_to_source_id` / `reply_to_source_type` 必須**: reply_draft はどの source への返信なのかが本質。Phase 6 MVP の `task` / `decision` は単独で意味を持つが、返信下書きは必ず**返信元 source**を参照する。`reply_to_source_id` は `sources` projection の ULID、`reply_to_source_type` は `slack_message` / `ms365_outlook` / `box_event` 等の discriminator (`source_type`) を取る。値の整合性は apply 時に projection JOIN で検証する (存在しない source への reply_draft apply は OpsHubError)
- **`subject` は optional**: Slack / Outlook reply ではない / subject を分離しない channel (e.g. 1-on-1 DM) では `None`。caller / 表示層は title の一部として fallback 表示
- **`schema_version` = v2**: Phase 6 の `task` (v1) / `decision` (v1) と同じ union 内に共存する。reader は §決定 (f) のとおり両 version を inline で読む。Phase 6 で生成した v1 candidate を rewrite する経路は持たない (event log immutability、ADR-0002)
- **本文長 cap = 8000 chars**: 通常の Slack / メール返信長 (Phase 10 plan §7 で実機 sampling) + prompt cost に見合う安全側設定。Pydantic Field の `max_length` で機械強制し、超過は ProposalFailed 経路へ
- **外部書き戻し境界**: reply_draft の **apply は durable state (`proposals.candidate_states` を `pending → applied` に flip) のみ** で、SaaS への送信は行わない (ADR-0010 改訂 = §Phase 10 で write-back を当面 scope 外)。下書きは `proposals.candidates[i]` に保存され、operator が手で外部宛先 (Slack UI / Outlook 等) にコピペする。HITL 境界の test pin は §決定 (k) で詳述

### (j) Triage 3 分類 (`respond` / `notify` / `ignore`) を `propose generate` の structured field に載せる

`propose generate` の出力 schema に **triage classification** を追加し、LLM が「この source 群に対してエージェントとしてどう振る舞うべきか」を 3 値で返せるようにする。これは Executive AI Assistant (EAIA) で確立されている分類 (respond / notify / ignore) を opshub の structured output に取り込んだもの。

```python
# src/opshub/services/proposals/service.py の ProposalCandidatesSchema を拡張 (Phase 10 step E2)
class ProposalCandidatesSchema(BaseModel):
    candidates: list[Candidate] = Field(default_factory=lambda: [], max_length=20)
    triage: Literal["respond", "notify", "ignore"] | None = None
```

要点:

- **parent schema は `schema_version` を持たない**: schema versioning (§決定 (f)) は **per-candidate** で行う (`TaskCandidatePayload.schema_version = "v1"` / `DecisionCandidatePayload.schema_version = "v1"` / `ReplyDraftCandidatePayload.schema_version = "v2"`)。parent (`ProposalCandidatesSchema`) は candidate の discriminated union を運ぶ container にすぎず、parent 自体に独立した version は必要ない。Phase 6 v1 callers と Phase 10 callers は同じ parent shape を受け取り、union discriminator (`kind` + 各 candidate の `schema_version`) で「どの version の candidate が混ざっているか」を判定する
- **3 値の意味**: `respond` = LLM が「返信下書きを 1 件以上提案する」と判断 (reply_draft candidate を `candidates` に含めるべきだった) / `notify` = 返信不要だが operator に存在を知らせるべき (inbox_item 系の triage に近いが、Phase 10 では durable な inbox_item を auto 生成しない) / `ignore` = ノイズ (人間 operator が後で見直し)
- **`triage` は generate-time の prompt hint としてのみ使われ、persist されない (Phase 10 audit Round 2 で明示)**: LLM 出力の `ProposalCandidatesSchema.triage` フィールドは generate 時の structured-output validation で受け取る一過性の signal で、apply / persist 経路には流さない。`Proposal` dataclass や `ProposalGenerated` event には triage 相当の field を保持せず、`ProposalService.generate` は `parsed.triage` を読まずに `parsed.candidates` のみを下流に渡す。理由は (1) triage が persist されないことで「triage='respond' で auto-apply」のようなセキュリティ悪用経路を構造的に閉じる (HITL boundary 強化、§決定 (c) と整合)、(2) Phase 6 backward-compat (v1 candidates は triage 無し) が parent schema レベルで自然に成立、(3) downstream consumer (CLI / MCP / Skill) が triage を読まないため UX 上の implementation drift が起きない。test pin: `tests/unit/services/proposals/test_proposal_schema.py::test_proposal_service_does_not_persist_triage`
- **`Optional` (default `None`)**: Phase 6 callers との後方互換のため。LLM が triage を返さなかった場合は `None` で受け取り、generate 経路では同様に discard される
- **auto-apply 禁止の継続**: `triage = "ignore"` を見て candidate を自動破棄する経路、`triage = "respond"` で自動送信する経路はいずれも実装しない。§決定 (c) HITL 必須宣言を継承。triage が persist されない (本決定で明示) ことで「将来の `respond → auto-send` flag」が立つ余地そのものを潰している

### (k) 文体は静的プロンプトでなく recall した「自分が author の過去送信 event」を `<style_example>` 注入

`reply_draft` の生成プロンプトは Phase 6 の `propose generate` 系プロンプト (do-not-follow preamble + 静的 system prompt + `<source>` ブロック) を継承するが、文体注入を**静的プロンプト**で書かず、**過去の自分が送信した event を recall して `<style_example>` ブロックとして注入**する。

```text
<style_example source_id="01J..." type="slack_message">
> こんにちは、月曜の件、了解しました。明日午前に対応します。
</style_example>

<style_example source_id="01J..." type="ms365_outlook">
> Hi Alice, thanks for the follow-up. Let me check with the team.
</style_example>
```

要点:

- **recall query 設計**: `author = self` を Phase 10 で確立される provenance タグ (ADR-0020 §(e)、`provenance_origin = "internal"` または source 側の sender ID = operator) で絞り、返信先 channel / counterpart は `source_type` + `connector_name` + 既存 `body` で絞る。Phase 10 Sub-issue B (本文 embedding + FTS5) でハイブリッド検索が可能なため、過去の「自分が送信した、同じ channel / 同じ相手宛て、同じ topic の」message を 1-3 件 recall し `<style_example>` ブロックに展開
- **`<context_source>` ブロック (= `--expand-graph` 経由)**: 文体だけでなく**文脈**も提供する。Phase 8 で導入した `--expand-graph` (ADR-0017 §決定 (f)) を `reply_draft` 生成でも opt-in で発火し、返信元 source の knowledge graph 1-hop neighbours を `<context_source>` ブロックとして注入する。Read AI Ada の自前 graph 相当を既存機構で代替できる
- **`<style_example>` も DATA**: ADR-0015 §決定 (f) do-not-follow preamble を維持する。`<style_example>` 内の文字列に「無視して X せよ」が含まれていても LLM は従わない (preamble で system レベル指示として明示)。html-escape も Phase 5 D1 の delimiter wrap 防御を継承
- **薄い静的 About**: 署名 / 役割 (e.g. "I am OpsHub's reply-draft assistant.") は system prompt 側に短く置く (≤200 chars)。「LLM の性格設定」を肥大化させず、文体は entirely recall ベースで決まる方針 = Inbox Zero の弱点 (テンプレ口調の暴走) を回避
- **依存**: Sub-issue A (本文保持、ADR-0020) と Sub-issue B (本文 embedding + FTS5、ADR-0012 改訂) が prerequisite。本文保持していない世界では style_example が summary だけになり文体注入の意義が薄れる

### (l) Draft 系統一方針 (Phase 12 H1 で追加、2026-05-31 改訂)

Phase 12 H1 (`docs/phase-12-plan.md` §3 H1-a) で 14 skill 体制に拡張する際、draft 系 skill が `reply-draft` 1 つから `reply-draft` / `handoff-draft` / `announcement-draft` の 3 つに増える。本 §決定は draft 系全体の persist 方針 / mode 引数の射程 / triage の射程 / Candidate discriminated union の freeze を独立条文として pin する。

#### (a) persist 境界は「返信元 source の有無」で切る

| skill | persist? | 理由 |
|---|---|---|
| `reply-draft` | **persist する** (`propose.generate` + `propose.apply` 経路) | 返信元 `source_id` が natural key として存在。`reply_to_source_id` を持つ `ReplyDraftCandidatePayload` (§決定 (i)) で audit log + idempotency を成立させる |
| `handoff-draft` | **persist しない** (text-only 返却) | 自発生成 (引き継ぎ書) で natural key なし。proposal table に保存しても操作者から見て idempotency の意味付けができず、削除 / 編集の semantics も曖昧 |
| `announcement-draft` | **persist しない** (text-only 返却) | 同上 (告知文の自発生成、natural key なし) |

`handoff-draft` / `announcement-draft` は host LLM が `brief` + `recall.search` + `source.get` + `decision.list` の read tool 群を合成して text を組み立てる経路で実装し、`propose.generate` は経由しない。

#### (b) `propose.generate` の `mode` 引数の射程

Phase 12 H4 で `propose.generate` に `mode` 引数 (`inbox_triage` / `source_extract` / `meeting_followup`) が追加される予定。本 §決定で **`mode` 引数は persist 経路を持つ structured-output dispatch key に限定** することを pin する。

- 持つ: `reply_draft` (既存) / `inbox_triage` / `source_extract` / `meeting_followup` の 4 mode (いずれも persist する候補を生む)
- 持たない: `handoff_draft` / `announcement_draft` — text-only のため `propose.generate` を経由せず、host LLM が read tool 合成で組み立てる

将来 persist 需要が出た draft type は本 ADR §決定 (l) の (e) (schema versioning) で対応する。

#### (c) Triage は reply_draft 文脈のみ

§決定 (j) の 3 値 triage (`respond` / `notify` / `ignore`) は **draft 系全体ではなく reply_draft 専用 signal** として pin する。`handoff-draft` / `announcement-draft` / `inbox-triage` / `source-extract` / `meeting-followup` は triage を持たない (それぞれ独自の意味体系で運用する)。

§決定 (j) の文面が "draft" を主語にしているため将来 misread されるリスクがあり、本条文で射程を明確化する。

#### (d) Candidate discriminated union freeze

ADR-0016 §決定 (e) で定義した `Candidate = TaskCandidatePayload | DecisionCandidatePayload | ReplyDraftCandidatePayload` discriminated union を **3 kind で freeze** する。Phase 12 では新 candidate kind を追加しない (`HandoffDraftCandidatePayload` / `AnnouncementDraftCandidatePayload` 等は作らない)。

将来 persist 需要が顕在化した場合は §決定 (f) の schema versioning パターン (literal field + 両 version 読み分け + in-place migration なし) で対応する。`HandoffDraftCandidatePayload` を `kind="handoff_draft"` + `schema_version=2` などの形で追加し、新 ADR or 本 ADR の再改訂で正規化する経路を予約する。

#### (e) 理由

使用頻度の現実主義: handoff / announcement は週次〜月次の頻度で、reply は時間単位の頻度。persist + idempotency + audit のメリットが回収できる頻度に閾値がある。

schema 拡張コスト: 新 candidate kind を増やすたびに `Candidate` union 拡張 + projection migration + 各 service 層分岐の 3 セットを更新する。「あったら便利」レベルの draft type を増やすと metabolic load が増える。

将来の戻し道: §決定 (f) versioning パターンが既にあるため、persist 需要が顕在化したときに ADR 再改訂で素直に追加できる構造を残してある。

## Consequences

### Positive

1. **Action loop layer が単一の Pluggable 経路で開く** — `LLMClient.complete_structured` 1 method の追加で Anthropic / OpenAI / Ollama 3 backend を統一的に扱える。新 backend 追加 (例: Gemini) も Protocol 拡張のみで済む
2. **ADR-0015 §決定 (a) deferred の closeout** — Local LLM backend (Ollama) が Phase 6 で実装され、principles.md §1 (Local-first) との緊張が `[llm] backend = "ollama"` 設定下では完全に解消される
3. **human-in-the-loop の明示契約** — ADR-0004 (Agent Runtime Boundary) を Action loop 文脈で具体化。「LLM の出力を durable state に書く前に operator の review が必須」が docs / test / コードの 3 層で pin される
4. **schema-driven candidate types** — Pydantic v2 を SSOT にすることで、新 candidate type 追加が field 1 つ + Literal 1 値の拡張で済む。parser を書く必要が無い
5. **idempotent re-run** — `(proposal_id, candidate_index)` natural key + state guard で transient crash 経由の re-run が安全。Phase 4 EmbeddingService re-run と同じ semantic で operator mental model 統一
6. **event-sourced trace の保全** — `schema_version` literal で in-place migration を回避。Phase 6.x.y.z で field 拡張しても過去 candidate は immutable のまま残る
7. **validation の SSOT 化** — apply 経路が `TaskService.add_task` / `DecisionService.record_decision` を経由するため、Phase 1-2 で確立した validation / sanitisation が無条件に適用される

### Negative / Trade-offs

1. **`complete_structured` が `LLMClient` Protocol を膨らませる** — Phase 5 で freeze した Protocol に method 追加は freeze 違反ではない (拡張のみ可) が、Protocol 面積が増えることで「新 backend を実装する難度」が上がる
   - 緩和: 共通 helper (`pydantic_to_tool_schema`) を `opshub.llm.schema` に置き、各 client の実装差分を最小化する (Phase 6 A2 で実施)
2. **auto-apply を「Phase 6.x 以降も禁止」と強く宣言したことで future flexibility が縮む** — operator から「高信頼度 candidate は自動 apply したい」要望が来ても本 ADR を Superseded by する手続きが必要
   - 緩和: 「flag 1 つで緩める」では無く「新 ADR + ADR-0004 revisit」を要件化することで、OpsHub の core value (operational memory として信用できる) を意図的に守る。短期 UX より長期 trust を選んだ判断であることを明記
3. **Ollama daemon の起動 / port 競合 / cross-platform 差分は operator 責任** — `OllamaLLMClient` は daemon 不在を `ConfigError` で fail-fast するが、daemon の運用 (auto-start / port 設定 / model pull) は OpsHub が touch しない
   - 緩和: README + `opshub propose` の error message で `ollama pull llama3.2:3b` 等の具体手順を案内 (Phase 6 C1 で documentation)
4. **`llama.cpp` direct 不在で「daemon を立てたくない operator」を取り逃す** — Ollama daemon 起動を嫌う operator (一時的な検証 / CI / docker) は API backend に逃げざるを得ず、Local-first 原則と緊張する
   - 緩和: Phase 6.x で `LlamaCppLLMClient` を別 ADR で判断。優先度は (operator 需要 + Python binding 安定度 + 配布 footprint) で評価
5. **Pydantic schema → JSON schema 変換の細部で provider 差** — Pydantic v2 の `model_json_schema()` は OpenAPI 3.1 / JSON Schema 2020-12 系。Anthropic SDK / OpenAI SDK / Ollama がそれぞれ accept する schema dialect は微妙に異なる (`$ref` の扱い、`anyOf` vs `oneOf` 等)
   - 緩和: 共通 helper の test (Phase 6 A2) で 3 backend の代表 schema を round-trip 検証。差分は client 側 wrapper で吸収する余地を残す (Protocol は変えない)
6. **schema versioning で `match` arm が単調増加する** — Phase 6.x.y.z で field 拡張するたびに projector / apply の `match schema_version` arm が増える
   - 緩和: arm が 5-6 を超えたら schema 統合 (新 ADR + 新 Candidate model + 旧 model deprecate path) を検討する。in-place migration はしないという原則は固持

## Validation

Phase 6 sub-issue A-C の実装で本 ADR の決定 (a)-(h) は以下のとおり pin 済 (C1 closeout 時点):

- **(a) Structured output via tool calling** — 3 backend の `complete_structured` round-trip test が SDK / httpx を mock 経由で固定: Anthropic は `tests/unit/llm/test_anthropic_client.py` (`tool_use` content block の serialize + parse)、OpenAI は `tests/unit/llm/test_openai_client.py` (`tools=` function calling)、Ollama は `tests/unit/llm/test_ollama_client.py` (OpenAI 互換 `tools=`、`httpx.MockTransport` で daemon 完全 mock)。3 client いずれも Pydantic schema → JSON schema → provider-native tool definition → response parse → Pydantic instance の経路を検証。
- **(b) Schema SSOT = Pydantic v2** — `opshub.llm.schema.pydantic_to_tool_schema` の test (`tests/unit/llm/test_schema.py`) で Pydantic v2 model → JSON schema 変換と `name` / `description` / `parameters` の構造を pin。`ProposalService` が `ProposalCandidatesSchema` Pydantic model を `LLMClient.complete_structured(schema=...)` に渡すことは `tests/unit/services/test_proposal_service.py::test_generate_calls_llm_with_structured_schema` で固定 (stub の `structured_calls` に schema が記録されるパターン)。
- **(c) Human-in-the-loop 必須** — `opshub propose` の CLI surface 自体が pin: `tests/unit/cli/test_propose.py` で `generate` / `list` / `apply` / `reject` の 4 subcommand のみが存在し、`--auto-apply` フラグや `auto_apply` config field が **存在しない** ことを Typer app inspection で確認。`apply` は別 subcommand として operator-triggered。
- **(d) Idempotent apply** — `ProposalService.apply` を同 `(proposal_id, candidate_index)` で 2 回呼び 2 回目が `OpsHubError("candidate already applied")` を raise する unit test (`tests/unit/services/test_proposal_service.py::test_apply_already_applied_raises_opshub_error`) + 同 candidate に対する apply 後の reject も `OpsHubError("candidate already applied")` で fail (`tests/unit/services/test_proposal_service.py::test_apply_already_rejected_raises_opshub_error` 系)。CLI 経由の end-to-end は `tests/integration/test_phase6_propose_atomicity.py::test_apply_already_applied_candidate_reraises_no_duplicate_task` + `test_reject_already_applied_candidate_fails_without_event` で固定。
- **(e) Candidate types MVP = task + decision** — `Candidate` discriminated union (`TaskCandidatePayload | DecisionCandidatePayload`) の `kind: Literal["task" | "decision"]` を `tests/unit/domain/events/test_proposal.py` で pin (`"inbox_item"` / `"source"` 等は Pydantic validation で reject)。`ProposalService.apply` の dispatch が candidate kind に応じて `TaskService.create_task` / `DecisionService.record_decision` を呼び分けることは `tests/unit/services/test_proposal_service.py` (`test_apply_task_candidate_creates_task_and_records_applied_event` / `test_apply_decision_candidate_records_decision_and_applied_event`) で固定。
- **(f) Schema versioning** — `TaskCandidatePayload.schema_version: Literal["v1"]` / `DecisionCandidatePayload.schema_version: Literal["v1"]` の default + validation test は `tests/unit/domain/events/test_proposal.py` で pin (任意の文字列を受け付けないこと)。Phase 6.x で `Literal["v1", "v2"]` 拡張時の両 version 読み分け test は Phase 6.x で追加。
- **(g) Apply path through existing services** — `ProposalService.apply` 内部で `TaskService.create_task` が呼ばれ、`TaskCreated` event + `ProposalApplied` event の 2 件が durable に append されることは `tests/unit/services/test_proposal_service.py::test_apply_task_candidate_creates_task_and_records_applied_event` で固定。Validation の二重化禁止は同 test が「`ProposalApplied.applied_entity_id == TaskService.create_task の戻り値」を assert することで間接的に pin (`ProposalService` が `tasks_table` に直接 INSERT しないことの裏返し)。end-to-end CLI 経路は `tests/integration/test_phase6_lifecycle.py::test_propose_lifecycle_generate_apply_reject_through_cli` で `TaskCreated` event + `tasks` projection row + `ProposalApplied` event を同 ULID で連動確認。
- **(h) Ollama backend (ADR-0015 §決定 (a) deferred を closeout)** — `OllamaLLMClient` が `LLMClient` Protocol (`complete` + `complete_structured`) を satisfy することは `tests/unit/llm/test_ollama_client.py::test_satisfies_llm_client_protocol`、daemon URL に対する probe (`GET /api/tags`) が connection refused / 5xx で `ConfigError` を raise する fail-fast は同ファイル `test_init_raises_config_error_when_daemon_unreachable` で固定。`build_llm_client(LLMSettings(backend="ollama"))` factory branch は `tests/unit/llm/test_factory.py`。end-to-end (`[llm] backend = "ollama"` 設定下の `opshub propose generate` が localhost 外への network 呼び出しを行わないこと) は `tests/integration/test_phase6_ollama_lifecycle.py::test_propose_generate_via_ollama_backend_through_cli` で固定 (`httpx.MockTransport` で全 request を捕捉、host が `localhost` / `127.0.0.1` のみであることを assert)。

End-to-end の整合確認は `tests/integration/test_phase6_lifecycle.py` (mocked LLMClient + mocked Embedder で `opshub task create` → `opshub embeddings rebuild` → `opshub brief` → `opshub propose generate --from-briefing` → `opshub propose apply` (新 task 出現) → `opshub propose reject` → `opshub propose list` の連鎖) + `tests/integration/test_phase6_propose_atomicity.py` (LLM 失敗 / projector 失敗 / disabled backend / 既 applied candidate への再 apply / reject の 5 失敗経路の atomicity + exit code) + `tests/integration/test_phase6_ollama_lifecycle.py` (`httpx.MockTransport` で Ollama daemon 完全 mock した end-to-end) の 3 ファイルで Phase 6 C1 closeout PR に同梱。

## Known Limitations / Phase 6.x

本 ADR の決定で **MVP 範囲外** として明示的に残した項目と、Phase 6.x で追加検討すべき制約:

1. **`inbox_item` / `source` candidate types 不在** — §決定 (e) のとおり Phase 6 MVP は `task` + `decision` のみ。connector sync auto-detect 経由の inbox auto-categorize / source summarize-and-add は Phase 6.x で別 sub-issue
2. **`llama.cpp` direct (python binding) 不在** — §決定 (h) のとおり Ollama daemon 不要にしたい operator 向けの `LlamaCppLLMClient` は Phase 6.x。`llama-cpp-python` の OS-specific binary install / ADR-0001 配布制約との整合は Phase 6.x で別 ADR
3. **Multi-step proposal (action plan with ordering) 不在** — 「task A → task B → decision C」のような順序付き提案は MVP では flat list のみ。Phase 6.x で `ProposalGenerated.candidates_plan: list[CandidatePlanStep]` 拡張を検討
4. **Proposal scoring / confidence ranking 不在** — LLM が candidate に `confidence: float` / `rationale_sources: list[str]` を付与し UI で sort する仕組みは Phase 6.x。schema versioning (§決定 (f)) で `Literal["v1", "v2"]` に拡張する形で増分追加
5. **Auto-apply mode は Phase 6.x 以降も禁止** — §決定 (c) のとおり強い宣言。Phase 6.x で flag を「足したくなる」場面が来ても、本 ADR を Superseded by する新 ADR + ADR-0004 revisit が必要。flag 単体追加では緩めない

## Open Questions

Phase 6 内では確定しない (Phase 6.x / 7 以降で別 ADR / 別 plan):

1. **`inbox_item` / `source` candidate types の信頼モデル** — connector-authored entity を LLM が「propose」する経路は、real-time 検証 (SaaS 側に実 item があるか確認) を伴う必要がある。Phase 6.x で別設計
2. **`llama.cpp` direct binding の判断基準** — Ollama 経由で 90% covered のため、daemon 不要にする operator 需要 / Python binding の安定度 / 配布 footprint で評価する基準を Phase 6.x 着手時に定める
3. **Multi-step proposal (action plan with ordering)** — flat list で MVP は十分か、step 順序の expression が必要かを Phase 6 実運用後に評価
4. **Proposal scoring / confidence ranking** — LLM 生成 confidence の信頼性 + UI 表現 (`opshub propose list --min-confidence 0.8`) の有用性を Phase 6 実運用後に評価
5. **Auto-apply mode** — §決定 (c) で **明示的に禁止**。Phase 6.x 以降の再評価は **新 ADR + ADR-0004 revisit を必須**。本 ADR の Open Question としては「将来的に再評価する余地がある」ではなく「再評価には新 ADR が必要」という flag のみ記録

principles.md §Open Q #5 (Multi-machine sync) は本 ADR では closeout しない (Phase 6 でも未着手)。ADR-0015 §決定 (a) deferred のみが本 ADR §決定 (h) で closeout される。

## Alternatives Considered

### 1. Grammar / JSON-mode constrained decoding

Anthropic Sonnet 系 + OpenAI gpt-4o 系の一部 SDK は JSON-mode / grammar-constrained decoding を提供する。これを使えば tool calling を経由せず Pydantic schema を直接 satisfy する JSON が返る。

却下理由:

- 機能の availability が provider-specific (Anthropic は `response_format` を一部モデルでのみ、OpenAI は `response_format={"type": "json_schema"}` を gpt-4o 以降のみ)。Ollama daemon の OpenAI 互換 endpoint は JSON-mode を `response_format` で部分 support するが、tool calling の方が compatibility が広い
- 3 backend で **同じ wire format で実装する** ことが本 ADR §決定 (a) の柱。tool calling は 3 backend で同一概念モデル
- tool calling は「LLM が複数の tool から選択する」という semantics があり、将来 multi-step proposal (Phase 6.x) で複数 candidate type を 1 turn で生成する経路が開ける。JSON-mode は単一 schema 強制で柔軟性が低い

### 2. Free-form text + regex extraction

LLM に markdown で candidate を出力させ、Python の regex / parser で構造化する。

却下理由:

- brittle。LLM の出力フォーマット微変化 (heading 階層 / bullet 記号 / 改行位置) で parser が壊れる
- schema validation が二重化する (regex で抽出 → Pydantic で validate)。SSOT 原則 (本 ADR §決定 (b)) と矛盾
- prompt engineering で「必ず JSON で返せ」と指示しても provider / model によって遵守率が大きく異なる。tool calling は SDK 層で format を強制できる
- 知識 MCP `ai/practice/prompt-injection` で「自然言語の制約は破られる」が pin されている

### 3. Single-shot prompt with markdown candidates parsed by Python

briefing と同様に LLM に markdown を出力させ、`opshub propose` が markdown を「人間に見せて手で apply」する設計。CLI 側は parser を持たない。

却下理由:

- candidate を `(proposal_id, candidate_index)` で natural key 化できない (markdown には index が無い)。idempotent apply (§決定 (d)) が成立しない
- operator が markdown を手で `opshub task add` / `opshub decision record` に転記することになり、Action loop の自動化が薄まる (briefing で既にできる)
- `ProposalApplied` event を発火する経路が無くなり、proposals projection (Phase 6 B2) の意味も消える

### 4. Auto-apply mode (`opshub propose --auto-apply` または `[llm] auto_apply = true`)

「高信頼度 candidate は operator review を skip して自動 apply する」モード。

却下理由 (本 ADR §決定 (c) で詳述):

- ADR-0004 (Agent Runtime Boundary) §決定 1 (agent / LLM の write 経路は CLI 経由のみ) と矛盾する
- LLM 生成テキストが prompt injection / hallucination 経由で durable state に到達する経路を開ける
- OpsHub の core value (operational memory として信用できる) と直接矛盾。短期 UX より長期 trust を選ぶ
- 「Phase 6.x 以降も導入しない」と宣言。再評価には本 ADR を Superseded by する新 ADR + ADR-0004 revisit が必要

### 5. Apply 経路で entity event を直接 append する (TaskService 不経由)

`ProposalService.apply` が `TaskCreated` / `DecisionRecorded` event を直接 append する。

却下理由:

- Phase 1-2 で `TaskService` / `DecisionService` に組み込んだ validation / sanitisation が bypass される (§決定 (g))
- 「validation を 2 系統持たない」原則違反 (Phase 4 で `Embedder` Protocol bypass を禁止した先例)
- LLM 生成テキストが ADR-0005 (External Content Minimization) の summary 制約を経由せず durable state に書き込まれる経路を開く
- test stub も 2 系統必要になる (entity service 用 + proposal service 用)。SSOT で 1 系統に絞る方が観測性が高い

### 6. In-place migration of past v1 candidates to v2

Phase 6.x で `Candidate` field 拡張時、過去 v1 candidate を projector で v2 schema に rewrite して projection を統一する。

却下理由 (§決定 (f) で詳述):

- ADR-0002 (Event-Sourced Architecture) の event immutability 原則違反
- 「いつ migration が走ったか」を別途 audit する必要が出て、event log の意味が崩れる
- 両 version 読み分け (`match schema_version: case "v1" / case "v2"`) は Pydantic discriminated union で type-safe に表現できる。projection 側の複雑性は小さい

### 7. `llama.cpp` direct (python binding) を MVP に含める

`llama-cpp-python` を `[llm-llamacpp]` extras で MVP scope に含める。daemon 不要、in-process で local inference。

却下理由 (§決定 (h) で詳述):

- OS-specific binary install (build from source / pre-built wheel availability に幅) で ADR-0001 (`uv tool install opshub`) を破る
- model file 同梱 (4-30GB) も ADR-0001 の core size 制約に抵触
- Ollama で 90% covered。`llama.cpp` direct の need は Phase 6.x で別 ADR で評価

### 8. Reply-draft を新 ADR / 新 sub-system として独立させる (Phase 10 改訂で **却下**)

Phase 10 Sub-issue E で `reply_draft` を Phase 6 の propose の枠外に独立させ、新 ADR (例: ADR-0023 Reply Draft Generation) + 専用 service (`ReplyDraftService`) + 専用 projection + 専用 CLI (`opshub reply ...`) を切る案。

却下理由:

- Phase 6 で確立した propose lifecycle (generate → review → apply / reject) と triage / HITL / 既存 service 経由の apply / idempotent key / schema versioning が **そっくり再利用可能**。reply_draft は Candidate kind を 1 つ追加するだけで成立し、独立 sub-system はコード重複と CLI 表面の肥大化を招く
- 「下書きを **apply で durable state に書く** + **外部送信は ADR-0010 改訂で当面 scope 外**」境界は ADR-0016 §決定 (c) HITL の自然延長で、新 ADR で再定義する価値が薄い
- 独立 projection (`reply_drafts` テーブル) は `proposals.candidates[i]` の JSON で代替可能。Phase 6 の `proposals` projection と join しても `(proposal_id, candidate_index)` natural key で reply_draft の `pending / applied / rejected` 状態が表現できる
- propose 側に集約することで `opshub propose generate --reply-to <source_id>` 1 経路に統一でき、操作の mental model が小さい (Phase 10 Sub-issue D 秘書 Skill 表で `reply-draft skill → opshub propose generate --reply-to <source_id>` のマッピングと整合)

### 9. Triage を separate API / separate event にする (Phase 10 改訂で **却下**)

Triage 分類 (`respond` / `notify` / `ignore`) を `propose generate` の前段で別 LLM call として走らせ、`Triaged(source_id, classification)` 系の新 event + projection を作る案。

却下理由:

- 「triage は単独で意味を持つ durable state」だと auto-apply 禁止原則 (§決定 (c)) と緊張する。`triage = "ignore"` が durable に書かれると「ignore された source は次回以降の recall から外す」等の能動的処理に拡張する圧力が出て、HITL 境界が崩れる
- LLM call を 2 段 (triage → generate) に分けると cost が倍 (Phase 10 plan §1 緊張点 ③ の「能動性は Phase 10 で作らない」とも整合せず)
- triage 結果を `propose generate` の structured field として 1 回の LLM call で同時に得れば cost は変わらず、operator は triage hint を見て candidate を絞れる
- 「ノイズ source の自動破棄」は Phase 9 で確立した excludes 経路 (ADR-0019 + ADR-0020 §(b) で機構化) で取り込み前段に置くべきで、LLM triage は post-hoc な hint に留める設計責務の切り分け

## 関連

- [Principles 1 (Local-first)](../principles.md) — Ollama 採用で `[llm] backend = "ollama"` 経路では完全 local。`anthropic` / `openai` 選択時は依然 external (緊張は部分解消のみ)
- [Principles 5 (Multi-Agent Neutral)](../principles.md) — 3 backend (Anthropic / OpenAI / Ollama) で vendor 中立を継承
- [Principles 6 (External Content Minimization)](../principles.md) — apply 経路は既存 TaskService / DecisionService を経由するため summary 制約を継承
- [Principles 9 (Phased Delivery)](../principles.md) — Phase 6 MVP scope 絞り込みの根拠 (`task` + `decision` のみ、Ollama のみ、auto-apply 不在)
- [ADR-0001: Python Stack](0001-python-stack.md) — `[llm-ollama]` extras + `llama.cpp` direct deferred の配布制約根拠
- [ADR-0002: Event-Sourced Architecture](0002-event-sourced-architecture.md) — `schema_version` literal + in-place migration なしの根拠
- [ADR-0004: Agent Runtime Boundary](0004-agent-runtime-boundary.md) — human-in-the-loop 必須の根拠 (本 ADR §決定 (c) で Action loop 文脈に具体化)
- [ADR-0005: External Content Minimization](0005-external-content-minimization.md) — apply 経路で LLM 生成 text が summary 制約を経由する根拠
- [ADR-0009: Multi-Agent Neutrality](0009-multi-agent-neutrality.md) — 3 LLM backend 中立 (Anthropic / OpenAI / Ollama)
- [ADR-0012: Embedding Strategy](0012-embedding-strategy.md) — Pluggable backend pattern の先例、本 ADR の `complete_structured` 拡張もこれを踏襲
- [ADR-0014: SaaS Token Storage](0014-saas-token-storage.md) — Anthropic / OpenAI key は再利用、Ollama は API key 不要
- [ADR-0015: LLM Usage Strategy](0015-llm-usage-strategy.md) — §決定 (a) Local LLM deferred を本 ADR §決定 (h) で closeout
- [Phase 6 Plan §1 (確定済み事項) + §2.1 (sub-issue A)](../phase-6-plan.md)
- 知識 MCP: `ai/platform/anthropic-api` (`tool_use` content block 仕様) / `tools/openai-python` (chat completions tool calling) / 外部: Ollama OpenAI compatibility docs
