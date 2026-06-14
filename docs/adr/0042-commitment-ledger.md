# 0042. Commitment Ledger (two-way, LLM-mined, manual scan)

- Status: Accepted (Phase 25-C, epic [#566](https://github.com/ozzy-labs/opshub/issues/566))
- Date: 2026-06-14
- Deciders: opshub maintainers
- Related: [ADR-0002](0002-event-sourced-architecture.md) (LLM / non-deterministic decisions stay out of projections — the extracted commitment is recorded as a `CommitmentExtracted` event, the ledger projection is a pure function of the event log), [ADR-0016](0016-action-loop-and-structured-output.md) (`propose` is the同型 pattern — service calls the LLM outside the UoW, persists the result as an event, a deterministic projection materialises it; no auto-apply), [ADR-0010](0010-connector-contract.md) §Phase 25-A 改訂 (`author_handle` / operator self-identity `is_authored_by_operator` — the direction signal this ADR consumes; write-back ban — the督促 boundary this ADR inherits), [ADR-0043](0043-cross-source-identity-resolution.md) (`person:<id>` counterparty refs — the「誰に / 誰から」this ledger attributes commitments to), [ADR-0015](0015-llm-usage-strategy.md) (Pluggable LLM + fail-loud `ConfigError` when no backend is configured)

## Context

秘書化 v1 (epic #566) の旗艦。opshub の読み取り面 (`search` / `recall` / `brief` / `slack.demand.list`) は「何があったか」を引くところで止まっており、秘書エージェントの本業 **「言った約束を追い、頼んだ件を督促する」** を直接支える機構がない。

固有価値は **opshub が operator 自身の送信側メッセージも取り込んでいる**こと。他の inbox ツールは受信トレイしか見えず、「自分が誰に何を約束したか」を追えない。opshub は Phase 25-A (ADR-0010 §改訂) で全 connector の author を正規化し `sources` に author 列を載せ、operator self-identity (`is_authored_by_operator`) を per-connector に持たせた。これにより **送信側 (`i_owe`) と受信側 (`owed_to_me`) を双方向に追う**素材が揃った。

設計上の肝は **コミットメント抽出は「金曜までに送ります」を LLM で読む非決定的処理**であり、**projection に LLM 出力を入れてはいけない**こと (replay の決定性が壊れる、ADR-0002)。採用パターンは `propose` (`services/proposals/service.py`) と同型: 抽出サービスが LLM を `complete_structured(schema=...)` で呼び (**UoW の外**)、結果を `CommitmentExtracted` event として永続化 → `commitments` projection が台帳を決定的に materialise する。

## Decision

operator が既に取り込んだ source body を LLM で読み、**双方向コミットメント台帳**を materialise する。以下を pin する。

### (a) 抽出トリガ = 手動スキャン (オンデマンド増分)

`opshub commitment scan` 実行時のみ、前回スキャン以降の新規 source を LLM 抽出する。

- **非常駐原則 (ADR-0004 form-A) 維持** — sync 時の自動抽出 / 候補事前承認は非採用 (v1 確認後に再検討)。operator が LLM コストを握る。
- **閲覧 (`commitment.list`) は LLM 不要** — 台帳の読み取りは projection の純粋読み出しで、追加の LLM コールを伴わない。
- **誤検出は `commitment dismiss` で除外** — 抽出は false positive を許容し、operator が HITL で捨てる。
- **LLM 未設定時は `ConfigError` (fail-loud)** — `[llm] backend = disabled` で scan を呼ぶと clean な `ConfigError` を返す (ADR-0015、silent な空抽出をしない)。

### (b) 増分のための scan cursor を新規移植

非 connector の checkpoint 機構は存在しなかった (唯一 `source_service.py` の `connector_cursors`)。これを範に `commitment_scan_cursor` を **event-sourced で新設**する。

- singleton key = `"commitment_scan"` (install 全体で 1 cursor、scan は per-connector でなく全 connector の `sources` を sweep する)。
- scan は `ConnectorSync{Started,Completed,Failed}` と **symmetric な 3-event bracket** で囲む:
  - `CommitmentScanStarted` — resume-from の watermark を記録 (前回 completed scan の high-water、初回は `None`)。
  - `CommitmentScanCompleted` — watermark を「この run が抽出した最後の source id」に前進。次の scan はそこから resume し、抽出済 source を再読しない (LLM コストを再払いしない)。
  - `CommitmentScanFailed` — cursor の **no-op** (watermark は最後の completed scan に留まり、次の手動 scan が同じ未抽出 source を再試行)。診断用にのみ記録 (`error_message` は構築前に `sanitise_error_message` で sanitise 済)。
- `scan` は長時間 CLI 進捗対象 (ADR-0026)。

### (c) 抽出 event = `CommitmentExtracted` (per-commitment、`source_ref` 冪等)

- `aggregate_id` は commitment 自身の ULID (scan service が mint)。
- **冪等 re-extraction の natural key は `(source_id, source_type)` の `source_ref`** — projection はこれで upsert するので、抽出済 source を再 scan しても行を in-place 上書きし、duplicate を生まない (`inbox_items` の `source_ref` invariant と同型、ADR-0010 §不変条件 7)。
- フィールド: `direction` (`i_owe` / `owed_to_me`) / `counterparty` (`person:<id>` graph ref または `None`) / `due` (LLM が body から抽出した ISO-8601 風 free-form text または `None`) / `text` (1 行サマリ) / `confidence` (`high` / `medium` / `low`) + cost trace (`model_id` / `tokens_in` / `tokens_out`、`ProposalGenerated` と symmetric)。

### (d) direction = operator self-id signal × LLM 本文読み

`direction` は Phase 25-A の `is_authored_by_operator` signal で決める:

- source の author が operator 自身 → `i_owe` (自分が負った約束)。
- それ以外 → `owed_to_me` (相手に頼まれて自分が待っている件、または相手が自分に約束した件)。

LLM の本文読みが combine され、約束らしさ自体の判定も LLM が下す。`counterparty` は author の resolved person (Phase 25-B) を `person:<id>` で紐付け、resolve 不能なら `None` (un-attributed でも commitment 自体は追う)。

### (e) 状態遷移 = operator HITL のみ (resolve / dismiss / reopen)

台帳は **読み取り signal**。督促 (外部送信) は行わない (ADR-0010 write-back ban / no-auto-apply 継承)。状態遷移は operator の明示操作のみ:

- `CommitmentResolved` — 約束を done に (`state` → `resolved`、行は audit のため残る)。
- `CommitmentDismissed` — 抽出を false positive と判断 (`state` → `dismissed`、default open 台帳から外れるが audit のため retain、optional `reason`)。
- `CommitmentReopened` — 誤って resolve / dismiss した約束を `state` → `open` に戻す。

各遷移は projection 層で冪等 (replay no-op)、service 層は duplicate transition で fail-fast する。

### (f) 督促境界 (HITL 継承)

ledger は signal 面に閉じる。**外部への reminder / nudge は一切送らない** (ADR-0010 §禁止事項 7)。「期日超過の約束がある」を `commitment.list` / `next-actions` / `personal-brief` が surface するところまでが本 ADR の scope。外部送信督促 (HITL write-back) は別 Phase。

### (g) 決定性 (ADR-0002)

LLM の *判断* (commitment があるか? どの direction か? いつ due か?) は scan service が下し、`CommitmentExtracted` として event log に記録、その後 `projections/commitments.py` の reducer が決定的に materialise する。**projection 内で LLM コールは走らない**ので、`projections rebuild` は event log を byte-identical な台帳に replay する。scan cursor も `projections/commitment_scan_cursor.py` が started/completed pair を `connector_cursors` と同じく決定的に thread する (`rebuild` は cursor を reset しない = 同値復元、ADR-0038 §Context の Slack cursor と同論理)。

### (h) CLI = `opshub commitment scan | list | resolve | dismiss | reopen`

```text
opshub commitment scan [--since <source-id>] [--max-sources N]
opshub commitment list [--direction i-owe|owed-to-me] [--open] [--person <id>] [--format table|json]
opshub commitment resolve|dismiss|reopen <commitment_id> [--reason <text>]
```

- `scan` は手動増分 (LLM 抽出)。`list` は projection 純読み出し (LLM 不要)。`resolve` / `dismiss` / `reopen` は HITL 状態遷移。
- `due` は free-form text としてそのまま surface する (LLM が partial date を返しうるため、比較可能 date として扱わない)。期日超過 (`overdue`) 判定は表示層の責務で、`due` を比較可能と解釈できた行に対してのみ立てる。

### (i) MCP surface (ADR-0022 §決定 (h))

- **read +1**: `commitment.list` (台帳 read-only、`direction` / `state` / `person` filter、LLM 不要)。`due_before` filter は **意図的に出さない** (`due` は LLM が読んだ free-form text で比較可能 date でない)。
- **write/HITL +3** (commitment 系): `commitment.scan` (open-world LLM 抽出、`[llm] backend = disabled` → `ConfigError`) / `commitment.resolve` / `commitment.dismiss` (closed-world destructive state transition over local SQLite、`propose.apply` の non-destructive carve-out には**含めない**)。
- count は ADR-0022 §決定 (h) で pin (person 系 / catchup と合算で 19 → 27 tools)。

## Consequences

### Positive

1. **秘書の本業を直接支える** — 「自分が負った約束」「相手に頼んで待っている件」を双方向に surface でき、`next-actions` / `personal-brief` の priority signal になる (`slack.demand.list` 追加と同型)。
2. **送信側データの固有価値を活かす** — operator の sent 側 (`i_owe`) を追えるのは inbox-only ツールにない opshub の差別化。
3. **replay 決定性を保つ** — LLM 抽出を `CommitmentExtracted` event に閉じ、projection は純関数。`projections rebuild` で台帳が完全再構築される (ADR-0002)。
4. **LLM コストを operator が握る** — 手動スキャン + 閲覧 LLM 不要なので、常時 LLM 課金が発生しない (form-A 維持)。
5. **HITL 境界を継承** — 督促の外部送信なし、状態遷移は operator 明示。誤検出は `dismiss` で安価に捨てられる。

### Negative / Trade-offs

1. **抽出は非決定的 (LLM 依存)** — 同じ source でも model / prompt 次第で抽出結果が揺れる。
   - 緩和: 抽出を event 化し source_ref で冪等 upsert するので、再 scan が duplicate を生まない。`confidence` を surface し low-confidence を operator が triage できる。
2. **`due` は free-form text** — 「金曜まで」が比較可能 date に解決できないことがある。
   - 緩和: `due` を verbatim 表示し、`overdue` 判定は解決できた行にのみ立てる。比較可能 date への正規化は将来。
3. **手動スキャン = 自動追従しない** — operator が `scan` を回さない限り新しい約束は台帳に乗らない。
   - 緩和: form-A 原則の意図的な選択 (LLM コスト掌握)。sync 時自動抽出は v1 確認後に再検討。
4. **scan cursor 移植のコスト** — 非 connector の checkpoint 機構を新設した。
   - 緩和: `connector_cursors` を範にし、event bracket も `ConnectorSync{Started,Completed,Failed}` と symmetric にして既存パターンに揃えた。

## Known Limitations / Future

- **embedding / 索引化なし** — v1 は signal 面のみ。commitment の vector 索引 / `recall` / `search` 索引化は将来。
- **`workspace generate` での commitment markdown 描画なし** — 将来。
- **外部送信督促なし** — HITL write-back は別 Phase。
- **sync 時自動抽出 / 候補事前承認なし** — v1 確認後に再検討。

## 関連

- [ADR-0002: Event-Sourced Architecture](0002-event-sourced-architecture.md) — LLM 抽出を projection に入れず event 化する根拠
- [ADR-0016: Action Loop and Structured Output](0016-action-loop-and-structured-output.md) — `propose` の同型パターン (サービスが LLM → event 化 → 決定的 projection、no-auto-apply)
- [ADR-0010: Connector Contract](0010-connector-contract.md) — Phase 25-A author 正規化 + operator self-identity (direction signal) / write-back ban (督促境界)
- [ADR-0043: Cross-Source Identity Resolution](0043-cross-source-identity-resolution.md) — `person:<id>` counterparty 解決 (誰に / 誰から)
- [ADR-0015: LLM Usage Strategy](0015-llm-usage-strategy.md) — Pluggable LLM + 未設定時 `ConfigError`
- [ADR-0022: MCP Server Surface](0022-mcp-server-surface.md) §決定 (h) — `commitment.list` / `commitment.scan` / `commitment.resolve` / `commitment.dismiss` の MCP contract
- epic [#566](https://github.com/ozzy-labs/opshub/issues/566) Phase 25 / sub-issue [#568](https://github.com/ozzy-labs/opshub/issues/568)
