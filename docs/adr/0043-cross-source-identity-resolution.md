# 0043. Cross-Source Identity Resolution (person-axis)

- Status: Accepted (Phase 25-B, epic [#566](https://github.com/ozzy-labs/opshub/issues/566))
- Date: 2026-06-14
- Deciders: opshub maintainers
- Related: [ADR-0002](0002-event-sourced-architecture.md) (LLM / non-deterministic decisions stay out of projections — the resolution *decision* is recorded as events, the projection is a pure function of them), [ADR-0010](0010-connector-contract.md) §改訂 (Phase 25-A author/sender normalisation — the `author_handle` / `author_connector` columns this ADR reduces), [ADR-0017](0017-knowledge-graph.md) §改訂 (`person:<id>` graph entity ref + `identifies` link type)

## Context

秘書化 v1 (epic #566) のコミットメント台帳 (25-C) は「誰に / 誰から」を双方向に追う。これには **同一人物を connector を跨いで 1 ノードに束ねる**仕組みが要る — 1 人の人間は Slack `U...` / email / GitHub `login` / Teams id など複数の author handle で現れる。

Phase 25-A (ADR-0010 §改訂) で全 SaaS connector の mapper が `author_handle` / `author_display` を正規化し `sources` 投影に author 列を載せた。25-B はこの正規化済 handle を **person aggregate** に reduce する最小基盤を置く (旗艦 25-C / catchup 25-E の土台)。

設計上の肝は **identity 解決の非決定性 (fuzzy 照合) を projection に入れない**こと (ADR-0002)。「この 2 handle は同一人物か」は exact / fuzzy の判断を含み、projection に入れると replay が壊れる。propose (ADR-0016) と同型で、**解決サービスが判断 → event 化 → 決定的 projection が materialise** する。

## Decision

`sources` の正規化済 author handle を **person 軸** に解決する。person は operator が触る操作層 (CLI) と、内部の不変 id (person ULID) を持つ。以下を pin する。

### (a) 二層の read model = `persons` + `person_identities`

- **`persons`** (migration 0035): `id` (person ULID, PK) / `display_name` (認識用、join key ではない) / `is_operator` (operator 自身を表す唯一の person) / `created_at` / `updated_at`。
- **`person_identities`** (migration 0036): `(connector, handle)` (PK = natural key) / `person_id` (FK → `persons.id`, `ON DELETE CASCADE`) / `display` / `confidence` / `linked_at`。`handle` は `sources.author_handle` の正規化済 join key (Slack `U...` / 小文字 email / GitHub login)。`(connector, handle)` を connector で名前空間分離するので、別 connector の同形 handle が衝突しない (ADR-0010 §Phase 25-A の `author_connector` 自己記述ルールと同じ)。

### (b) 4 event family (person aggregate)

| event | aggregate_id | 意味 |
|---|---|---|
| `PersonIdentified` | person ULID | 新 person を mint (`display_name` / `is_operator`) |
| `IdentityLinked` | person ULID | `(connector, handle)` identity を person に bind (`confidence` = `exact` / `manual`) |
| `IdentityMerged` | 生存 person ULID | `merged_person_id` の identity を生存 person に re-parent + merged person を tombstone |
| `IdentitySplit` | 分離元 person ULID | `(identity_connector, identity_handle)` を新 person (`new_person_id`) に detach |

base 契約は `domain/events/base.py` (frozen / `extra=forbid` / `event_type` Literal) に準拠。

### (c) 解決ポリシー = exact auto-merge / fuzzy HITL merge

`PersonResolutionService.resolve()` は `sources` author 列を走査し、未 bind な `(connector, handle)` ごとに person を決める。

- **exact auto-merge**:
  - email connector (`google_mail` / `ms365` / `google_calendar` / `google_workspace`) 間で **同一 email handle** は同一人物として既存 person に束ねる (Gmail + Outlook の同アドレス)。
  - operator 自身の handle (Phase 25-A の `is_authored_by_operator` で判定) は connector を跨いで **唯一の operator person** に束ねる (「operator も 1 person」)。同一 `(connector, handle)` の再観測は no-op。
- **fuzzy = HITL**: display name の類似など fuzzy signal は **auto-merge しない**。resolver は誤って束ねるより**過剰分割**する (安全側)。operator が `opshub person merge` で確定する。
- 解決は increment 的・冪等: 既知 handle は skip するので `resolve()` 再実行は何も追加しない。処理順は `(connector, handle)` の決定的 sort なので fresh DB 上の person id も run 間で安定。

### (d) operator も 1 person

operator 自身も 1 person ノードとして扱う (`is_operator = 1`)。督促・direction (25-C) が「自分」を一人称ノードとして引けるようにするため。env 設定済の operator identity (Phase 25-A `operator_*` config) を持つ handle が operator person に集約される。設定が無い場合は guess せず別 person のままにする (over-split 安全側)。

### (e) graph 統合 (ADR-0017 §改訂)

- entity ref scheme に `person:<id>` を加える。`projections/links.py` の entity type 列は free-form Text (closed enum 無し) なので **schema 変更不要** — `LinkService.related` / `trace` は `person:<id>` を透過的に扱う。
- identity link type `identifies` (`person:<id>` → `source:<id>` = この person がその source の author) を `LINK_TYPES_MVP` frozenset に昇格し、manual `link add --type identifies` が「推奨 enum 外」warning を出さないようにする。

### (f) 決定性 (ADR-0002)

fuzzy / exact の判断はサービスに閉じ、`PersonIdentified` / `IdentityLinked` / `IdentityMerged` / `IdentitySplit` として event log に記録する。`projections/persons.py` / `projections/person_identities.py` の reducer はこれら event の純関数なので、`projections rebuild` は同形の person graph に復元する。merge / split は両 table に跨る変更だが、**1 projection (`PersonsProjection`) の 1 apply 内で atomic** に適用し、projection 間の `person_id` FK 順序ハザードを避ける (merge は identity を先に re-parent してから person を delete、split は person を先に insert してから identity を re-point)。

### (g) CLI = `opshub person list | merge <a> <b> | split <connector>:<handle>`

- `list` は未 bind handle を incremental resolve してから person 一覧 (identity nested) を表示 (`--format table|json`)。冪等なので再実行可。
- `merge <a> <b>` は operator HITL merge。lexicographically 小さい id が生存 (引数順非依存で決定的)。
- `split <connector>:<handle>` は 1 identity を新 person に detach (過剰 merge の取り消し)。resolver は split しない (operator 専用)。

## Consequences

### Positive

1. **commitment ledger (25-C) の前提が揃う** — direction (`i_owe` / `owed_to_me`) も督促も「誰が」を必要とし、person 軸でそれを connector 横断に引ける。
2. **fuzzy 照合を projection から排除** — replay 決定性 (ADR-0002) を保ったまま identity 解決を導入。`projections rebuild` で person graph が完全再構築される。
3. **schema 最小** — 2 table + 1 FK + 2 index。graph entity type は free-form なので `links` schema は不変。
4. **over-split 安全側** — fuzzy auto-merge をしないので「別人を 1 人に誤統合」が起きない。誤分割は `merge` で安価に直せる (逆は難しい)。

### Negative / Trade-offs

1. **resolver は exact のみ自動** — display name しか手掛かりが無い同一人物は operator が手で `merge` する必要がある。
   - 緩和: `person list` で identity を並べて見せ、`merge <a> <b>` を 1 コマンドにする。fuzzy 候補の自動 surface は将来 (本 ADR scope 外)。
2. **`person_id` FK の cross-projection 順序ハザード** — merge は identity 先・person 後、split は person 先・identity 後と順序が逆。
   - 緩和: §決定 (f) のとおり cross-table mutation を 1 projection の 1 apply 内に閉じて atomic 化。
3. **email auto-merge は同一文字列のみ** — alias / 表記ゆれ (`a@x.com` vs `A@X.com` は正規化済だが `a@x.com` vs `a.b@x.com` は別) は束ねない。
   - 緩和: 正規化は Phase 25-A 側 (小文字化) に閉じる。それ以上の email 正規化は将来。

## Known Limitations / Future

- **embedding / 索引化なし** — v1 は signal 面のみ。person の vector 索引は将来。
- **fuzzy 候補の自動 surface なし** — `person list` は resolve 済の事実のみ表示。「この 2 人は同一かも」の提案は将来。
- **bi-temporal / as-of 想起なし** — 別テーマ。

## 関連

- [ADR-0002: Event-Sourced Architecture](0002-event-sourced-architecture.md) — fuzzy 判断を projection に入れず event 化する根拠
- [ADR-0010: Connector Contract](0010-connector-contract.md) — Phase 25-A author 正規化 (本 ADR が reduce する入力)
- [ADR-0017: Knowledge Graph](0017-knowledge-graph.md) — `person:<id>` entity ref + `identifies` link type (§改訂)
- [ADR-0016: Action Loop and Structured Output](0016-action-loop-and-structured-output.md) — 「サービスが判断 → event 化 → 決定的 projection」の同型パターン
- epic [#566](https://github.com/ozzy-labs/opshub/issues/566) Phase 25 / sub-issue [#567](https://github.com/ozzy-labs/opshub/issues/567)
