# 0010. Connector Contract

- Status: Accepted (revised 2026-06-07 for Phase 21 Sub-issue A)
- Date: 2026-05-17 (initial); 2026-05-30 (Phase 10 §Write-back scope clarification: 当面 scope 外); 2026-05-31 (Phase 11 改訂: Teams 追加 + 本文抽出契約 + delta-link cursor + User Token principal); 2026-05-31 (Phase 13 改訂: Google Workspace 追加 + Drive `changes.list` cursor + TTL fallback + Workspace export 本文抽出契約 + Google Refresh Token principal = MS365 / Box pattern 明文化); 2026-05-31 (Phase 14 改訂: Gmail + Google Calendar 追加 + delta-cursor 型 connector 全般 への TTL fallback 一般化 + Outlook 流本文抽出契約を Gmail / Calendar に拡張); 2026-06-07 (Phase 21 改訂: web connector 追加 + delta API なし connector の fingerprint 変更検知契約を web に適用 + crawler 非該当 posture); 2026-06-13 (issue #522 追記: 責務 5 の inbox enqueue が `source_ref` で冪等化される invariant を §不変条件 7 に明文化 — PR #529 が「ADR-0002 / ADR-0010 の延長」と位置づけた決定の記録漏れを是正)
- Deciders: ozzy
- Related: [ADR-0036](0036-slack-sync-date-floor.md) — Slack sync の date floor (`[connectors.slack] sync_since` / per-channel `since`) は本 contract の cursor checkpoint の上に `oldest = max(cursor, floor)` で乗る (cursor authoritative、既存 sync 済み channel に無影響); [ADR-0037](0037-browser-read-layer-playwright.md) — Phase 21 で新設する web connector (Playwright browser read 層) は本 contract の Connector Protocol + 責務 1-6 + 禁止事項 1-7 をそのまま適用する (§Phase 21 改訂 (n)-(o)); [ADR-0041](0041-slack-multi-workspace.md) — Phase 24 で Slack connector の cursor schema (per-alias nest) と `external_id` 規約 (`f"{team_id}:{channel_id}:{ts}"` 3-token re-key) を改める。connector instance は `name = "slack"` 単一のまま (multi-instance 化は不採用)、本 contract の Connector Protocol + 責務 1-6 + 禁止事項 1-7 は不変

## Context

Phase 3 で GitHub / Slack / Microsoft 365 / Box 等の Connector を実装する際、Connector が以下のことを「できる」状態にすると、OpsHub の整合性が崩れる。

1. **Task を Connector が自動生成する** — 外部 SaaS のイベントすべてが Task になり、人間の triage 判断を bypass する。誤判定タスクが大量発生
2. **Decision / Link を Connector が直接書く** — 「これは決定事項」「これは関連」の判定は本来 triage / 人間 / agent の責務。Connector が判定すると context を持たないまま誤った関係性が記録される
3. **Projection を Connector が直接更新** — Event Store を経由しないため audit / replay 性が壊れる (ADR-0002 違反)
4. **Event を Connector が任意定義** — semantic event 命名規約 (ADR-0002) から外れ、event の意味が散逸する

これらを防ぐために Connector の責務と禁止事項を明確化する必要がある。

## Decision

Connector は次の経路のみを実行する。

```text
external metadata fetch
  → source entity の作成・更新
  → source event の append (Event Store 経由、Application Service 呼び出し)
  → inbox item の作成
```

Connector の **責務**:

1. external API 呼び出し (差分 fetch)
2. source normalization (vendor 固有 field を OpsHub の Source Entity schema に変換)
3. cursor checkpointing (`connector_cursors` テーブル更新)
4. `SourceObserved` / `SourceReferenced` 等の source event を Application Service 経由で append
5. inbox item を Application Service 経由で作成 (triage 用キューに投入)
6. body 本文の minimization (ADR-0005 参照、summary / extracted action items のみ保持) — **Phase 10 で撤回**: ADR-0020 で「本文を取り込んで `SourceObserved.body` に載せる」方針に転換した。本項は ADR-0005 supersede 後は §不変条件 6 (下記) に置き換わる

Connector の **不変条件** (epic #470 / issue #481 で追加):

**不変条件 6. `SourceObserved.body` は必ず非空文字列** — Phase 10 当時 Optional だった `body` は epic #470 (pre-userbase compat shim cleanup) で `str = Field(min_length=1)` に格上げされた (ADR-0020 §(d') 参照)。metadata-only / stat-only path (`box_drive` の `content_extraction=False` / Google Workspace `google_workspace_file` catch-all / MS365 OneDrive metadata / Box web-API events / GitHub notifications 等) は `body = summary` を emit することで契約を満たす。summary を持たない event 形は連続して `title` を fallback として使う (e.g. Slack の `channel_join` event)。`SourceObserved` 構築時に Pydantic `ValidationError` で fail-fast し、`NULL` write は projection 層でも `sources.body NOT NULL` 制約で拒否される (migration `0030_enforce_sources_body_not_null`)

**不変条件 7. inbox enqueue は `source_ref` で冪等** (issue [#522](https://github.com/ozzy-labs/opshub/issues/522)) — 責務 5 の inbox item 作成は `SourceService.observe` が `SourceObserved` と atomic に append する `ItemEnqueued` (`source_ref = f"{connector_name}:{external_id}"`) を通る。同一 source の再観測は ADR-0002 §"every observation is a new event" に従い毎回新 ULID の `ItemEnqueued` を生むため、`inbox` projection は partial unique index `uq_inbox_items_source_ref` (`WHERE source_ref IS NOT NULL`) + reducer の `ON CONFLICT(source_ref) DO NOTHING` で **`source_ref` ごとに 1 inbox 行** へ収束させる (first-observation wins; `iter_all` が `(recorded_at, id)` 順で replay するため rebuild 決定的)。これにより cursor rewind / reset / backfill ([ADR-0038](0038-slack-sync-gap-backfill.md)) や delta API なし connector (`box_drive` / `web`) の fingerprint 変更による**意図的再観測**でも inbox 行は二重化しない (旧 #339 cascade と同型の膨張を断つ)。triaged 済み source の再観測は `DO NOTHING` が解決行を温存し再オープンしない (fingerprint 変更による「再 triage」が要るなら別 signal で扱う、本不変条件の副作用にはしない)。`source_ref IS NULL` の手動 / source-less enqueue (`InboxService.enqueue` / workspace front-matter 無記載) は自然キーを持たず partial index 対象外で one-row-per-event を保つ。これは §責務 4 の `SourceObserved` を `sources` projection が `(connector_name, external_id)` で冪等化するのと**対称**で、append-only な event log から「再観測しても 1 source = 1 inbox 行」を両 projection で成立させる。実装は `src/opshub/projections/inbox.py` + migration `0031_add_inbox_items_source_ref_unique_index`

Connector の **禁止事項**:

1. **Task / Decision / Link を直接生成しない** — これらは triage / 人間 / agent の責務
2. **Projection table を直接更新しない** — 必ず Event Store 経由
3. **Event を Application Service を経由せず append しない**
4. **Vendor 固有 event 名を勝手に定義しない** — `domain/events/source.py` で集中管理
5. **Full body を Operational Memory に書き込まない** — ADR-0005 違反 (**Phase 10 で撤回**、ADR-0020 で本文ローカル保持に転換。本項は ADR-0005 supersede 後は無効。本文取り込みは `SourceObserved.body` + `provenance_origin` / `provenance_trust` 経由で行う)。see §禁止事項 7 for the Phase 10 HITL boundary (write-back)
6. **Lock を取得せず長時間 Task の状態を変更する操作を発火しない** — 必要なら Application Service が lock を取る
7. **外部 SaaS への書き戻し (write-back) を実装しない** — **Phase 10 改訂で明示**。`post` / `send` / `comment` / `reply` 等の SaaS API への書き込みメソッドを connector に実装しない。Sub-issue E (返信下書き生成) は **下書き提示まで** で完結し、外送信は operator が手で行う (ADR-0016 §決定 (c) HITL 必須の延長)。詳細は §Phase 10 改訂を参照

triage を経て Task / Decision / Link が生成される流れ:

```text
[inbox_items] → triage (人間 or agent) → TaskCreated / DecisionRecorded / LinkCreated events
              └─ Application Service 経由
```

## Consequences

### Positive

1. **誤検知タスクの抑止** — 外部 SaaS の全イベントが Task 化されない
2. **Audit 整合性** — すべての書き込みが Event Store 経由
3. **Connector の単純化** — 責務が「fetch + normalize + event 化」に限定される
4. **Replayability** — Connector を後から差し替えても Event Store は壊れない
5. **テスト容易性** — Connector は service mock で完結する

### Negative / Trade-offs

1. **triage 操作の頻度が増える** — Connector が直接 Task 化しないため、人間 / agent が triage を回す必要
2. **automated triage への発展余地** — Phase 4 で agent が自動 triage する場合も、必ず Application Service 経由で event を発行する設計を維持
3. **Connector 実装ボイラプレート** — 各 Connector が service 呼び出しを書く分、行数が増える (Connector base 抽象クラスで吸収可)

## 軽減策

1. **`connectors/base.py` に共通基底クラス** — `fetch_changes` / `normalize` / `emit_events` の 3 メソッドだけ実装すれば Connector が完成する形に
2. **triage skill / CLI を充実** — `opshub inbox triage` で複数 inbox item を一括処理できるように
3. **自動 triage rule** — Phase 4 で「特定 channel の特定 sender → 自動 Task 化」のような rule を YAML で書ける機構を検討 (ただし rule の評価結果も Application Service 経由で event 化)

## Alternatives Considered

### 1. Connector が Task を直接生成する

却下理由:

- 全 SaaS イベントが Task 化すると tasks テーブルが膨張
- 重要度判定を Connector が持つことになり、ドメインロジックの責務分散
- 削除が困難 (event 化されているため)

### 2. Connector が「重要そうな」イベントだけ Task 化する

却下理由:

- 「重要そうな」を Connector が判断するためのロジックが必要
- 判断基準が SaaS / 用途で異なり、Connector 実装が肥大化
- triage という機能の存在意義が薄れる

### 3. Inbox item を経由せず直接 Triaged Event Stream に流す

却下理由:

- 人間 / agent が「未処理」と「処理済み」を区別する手段が必要
- inbox という抽象が無いと「今すぐ見るべきもの」が判別不能
- inbox は projection なので柔軟に表示形式を変えられる利点も失う

### 4. Connector が複数の Application Service にまたがって書き込みを行う

却下理由:

- Connector の責務が肥大化
- 異常時の rollback / lock 管理が困難
- 採用案 (1 connector → 1 service 呼び出し / 1 event 単位) の方が atomic 性を保てる

## Validation

Phase 3 sub-issue B (PR #51-55) で GitHub connector を本 contract に沿って実装し、`Connector` Protocol + `ConnectorContext` + `SourceService` 経由の event 連鎖 + cursor 永続化 + fail-fast (`ConnectorSyncFailed`) までを end-to-end で検証した (`tests/integration/test_github_connector_lifecycle.py` および sub-issue D の `tests/integration/test_phase3_lifecycle.py::test_github_connector_to_inbox_e2e`)。具体的に確認できたのは以下:

- Connector は Task / Decision / Link を一切生成しない (生成パスは `inbox triage` のみ)
- Projection 更新は `SourceService.observe` 経由でのみ発生 (Connector は SQL を直接叩かない)
- Event 名は `SourceObserved` / `ConnectorSyncStarted` / `ConnectorSyncCompleted` / `ConnectorSyncFailed` に集中管理され、vendor 固有 event は出現しない
- ADR-0005 整合 (summary / external_id / url のみ保持、full body は保持しない)
- 失敗時は `ConnectorSyncFailed` を append して fail-fast、token / PII を event log に漏らさない

### Phase 7 (Connectors Wave 2) validation

Phase 3 で導入した Connector Contract は Phase 7 で 3 新規 connector (Slack / Microsoft 365 / Box) の実装を経て validated:

- **Auth + token storage** — 3 connector とも `core/secrets` + keyring 経路を再利用 (ADR-0014 contract、`tests/unit/connectors/<name>/test_auth.py`)
- **Fetcher + cursor** — 3 connector とも `connector_cursors` projection で cursor 管理 (MS365 は 3 cursor key、Slack は per-channel JSON、Box は単一 stream_position)
- **Mapper + source_type** — 各 connector が distinct な `source_type` で `sources` projection に persist (`slack_message` / `ms365_calendar` / `ms365_onedrive` / `ms365_outlook` / `box_event`)
- **ADR-0005 (External Content Min)** — 全 connector で `summary` ≤ 200 chars enforce (test pin: `tests/integration/test_phase7_lifecycle.py` および per-connector unit test)
- **Rate-limit handling** — exponential backoff 1s/2s/4s max 3 retries、最終失敗で `ConnectorSyncFailed` event (test pin per connector: `tests/integration/test_phase7_connector_atomicity.py`)
- **CI mock 規律** — 全 connector test は SDK / HTTP fully mocked、実 API 非接続

Contract の signature 変更は本 phase で発生せず (Phase 3 で確定済の `Connector` Protocol が 3 つの新 connector で適合)、ADR-0010 は Phase 7 で touch せず Phase 7.x 以降の Additional connectors / common OAuth helper refactor で再評価する。

## Phase 10 改訂 (Sub-issue E、2026-05-30)

Phase 10 Sub-issue E (返信下書き生成、ADR-0016 §決定 (i)) で connector contract に **write-back 非対応** を明示的な禁止事項として追記する (§禁止事項 7)。

### 背景

Phase 3-9 の connector はすべて **fetch (差分取り込み) only** で実装されており、SaaS API への書き込み (Slack message 送信 / Outlook メール送信 / GitHub PR コメント等) を行うメソッドは存在しない。Phase 10 で `reply_draft` candidate kind (ADR-0016 §決定 (i)) を導入し、LLM が「返信下書き」を生成可能になることで、「下書きをそのまま自動送信する経路を作るべきか」という設計圧力が新たに生じる。

### 決定

**Phase 10 では SaaS への書き戻し / 投稿 / 送信を実装しない**。reply_draft candidate の apply は `proposals.candidate_states` を `pending → applied` に flip するだけで完結し、生成された下書き本文は `proposals.candidates[i].body` に durable に保存される。operator が手で SaaS UI (Slack / Outlook 等) にコピペして送信する。

理由:

1. **Phase 10 緊張点 ③ の決定** (phase-10-plan.md §1.6): 「返信下書きの生成は作る、外部書き戻しは当面作らない」を Phase 10 着手時に確定済。本 ADR §禁止事項 7 はその実装層への落とし込み
2. **HITL 境界の保全** (ADR-0016 §決定 (c)): LLM 生成テキストが durable state に書かれる前に operator review を 1 段挟む原則を、SaaS 送信に対しても適用 (= 送信前にも operator が確認する経路を保つ)。auto-send は prompt injection / hallucination が外部に伝播する経路を開く
3. **構造的に経路が存在しないことの保証**: connector に `post` / `send` / `comment` / `reply` メソッドを実装しないことで「経路がそもそも無い」状態を維持する。CLI / MCP tool / Skill が誤って auto-send 経路を踏むリスクがゼロ
4. **test pin の根拠**: Phase 10 step E2 で `tests/integration/test_phase10_reply_draft_no_external_writeback.py` (または unit test 相当) として、`opshub.connectors.*` モジュール内に `post` / `send` / `comment` / `reply` / `create_comment` 等のメソッドが存在しないことを assert する test を設置する。これは ADR-0016 §決定 (c) の HITL 境界を **コードベース上で機械的に保証** するための contract test

### Phase 11 以降の再評価条件

外部書き戻しを将来導入する場合は:

1. 本 ADR を Superseded by する新 ADR
2. ADR-0004 (Agent Runtime Boundary) を explicitly revisit
3. ADR-0016 §決定 (c) HITL 必須宣言と整合する設計 (auto-send 禁止、送信前 operator 確認、送信履歴の event-sourced 記録) を新 ADR で pin

これらすべてが揃った場合のみ。flag 1 つで緩める / 個別 connector が独自に実装する経路は許可しない (Phase 6 ADR-0016 §決定 (c) の auto-apply 禁止と同じ強度の宣言)。

## Phase 11 改訂 (Sub-issue F1、2026-05-31)

Phase 11 (epic #233) で Teams 新コネクタ + Word/Excel/PowerPoint 文書抽出 + Outlook 本文 deep retention を導入するにあたり、本 ADR を改訂し以下 4 点を追加する (Phase 10 改訂節 §禁止事項 7 は **保持**、本節は **加算改訂**)。

### Phase 11 改訂 (a) — Teams 新コネクタを契約対象に追加

Phase 11 Sub-issue F5 (#238) で **`connectors/teams/` connector** を新設し、本 ADR の `Connector` Protocol + 責務 1-6 + 禁止事項 1-7 をそのまま適用する。Microsoft Graph delta query 経由で chat messages を fetch し、`source_type="teams_message"` で `sources` projection に persist する。

- **Protocol signature 変更なし** — Phase 3 で確定し Phase 7 (Slack / MS365 / Box) + Phase 9 (box_drive) + Phase 11 (teams / onedrive_drive) で適合済の Connector Protocol を再利用
- **本 ADR の禁止事項 1-7 すべて適用** — Task / Decision / Link 直接生成禁止 / projection 直接更新禁止 / Application Service 経由必須 / vendor 固有 event 名禁止 / write-back ban (§禁止事項 7) を Teams にも継承
- **Slack ADR-0018 / 既存 ms365 connector パターンに揃える** — auth principal (Phase 11 改訂 (d) を参照) / fetcher / mapper / connector 4 module 構成

### Phase 11 改訂 (b) — 本文抽出契約: 連続 stat → 抽出 → SourceObserved with body

Phase 11 Sub-issue F4 (#237、box_drive / onedrive_drive) で local-FS-backed connector が Office 文書 (`.docx` / `.xlsx` / `.pptx`) の本文を取り込むにあたり、本 ADR §責務 1-2 (external API fetch + source normalization) と §責務 6 (body の minimization) を Office 文書経路に **延伸** する形で本文抽出契約を pin する。

本文抽出契約の流れ:

```text
external metadata fetch (FS scan: os.scandir → entry.stat())
  → diff detection (fingerprint = f"{size}:{mtime_ns}" との比較、ADR-0019 §決定 (d))
  → 拡張子マッチ + content_extraction = true の場合のみ
    → 抽出 (core/document_extract.extract(path) 経由、markitdown 単独経路、ADR-0025 §決定 (a))
    → fail-safe (例外時は body=None、ADR-0025 §決定 (c))
  → SourceObserved with (body + provenance_origin + provenance_trust) を Application Service 経由で append
```

抽出層の不変条件:

1. **抽出経路は ADR-0025 §決定 (a) の markitdown 1 本** — connector が直接 `python-docx` / `openpyxl` / `python-pptx` を import / 呼び出すことを禁止 (`core/document_extract.py` 1 module に集中化)
2. **size 上限 / text 上限 / cells 上限は ADR-0025 §決定 (b)(e) で pin** — connector ごとに独自上限を上書きしない (`opshub.toml` operator override は許容)
3. **抽出失敗は fail-safe で SourceObserved 発行継続** — ADR-0025 §決定 (c) の `body=None` + warning log + summary 注記契約を全 local-FS connector で共通
4. **source_type は ADR-0025 §決定 (d) の 3 種** — connector 名で source_type を分岐しない (box_drive と onedrive_drive のどちらも `word_document` / `excel_spreadsheet` / `powerpoint_slide_deck` を共通使用)
5. **ADR-0019 §決定 (b') opt-in 例外節と整合** — `content_extraction = true` の opt-in 設定下でのみ抽出経路が起動、default false で従来挙動 (本文なし) を維持

text-only 本文取り込み (Slack / Outlook / Teams chat) は markitdown 経由を **要さない**。これらは vendor SDK / Graph API から得た plain text / HTML を mapper が直接 `SourceObserved.body` に載せる経路で、`core/document_extract.py` は介在しない。本契約 §(b) は **バイナリ文書 (`.docx` / `.xlsx` / `.pptx`) からの本文抽出にのみ** 適用される。

### Phase 11 改訂 (c) — delta-link cursor + 失効時 full-pass fallback 義務

Phase 11 Sub-issue F5 (#238) Teams connector + 既存 Outlook / OneDrive (Phase 7 ms365 connector) で Microsoft Graph delta query を cursor として使う connector に、**delta-link cursor + 失効時 full-pass fallback** を契約として明示する。

delta-link cursor の運用:

1. **正常時** — `/me/chats/getAllMessages?$deltatoken=<token>` 等の Graph delta query で差分のみ取得、`@odata.deltaLink` を `connector_cursors.cursor` に opaque string で永続化
2. **TTL 失効時** — Graph API が `410 Gone` / `invalidatedDeltaToken` 系エラーを返した場合、**自動 fallback**:
   1. `WARNING` log: `event="connector.delta_link.expired"`, `connector=<name>`, `since=<original_delta_link>` (delta link 本体は token を含み得るため sanitised)
   2. **full-pass** モードで直近 N 日 (`fallback_window_days`、default `30`、`opshub.toml` 上書き可) を fetch、各 message を SourceObserved として append (重複は SourceObserved の dedup key で吸収、append-only の自然な挙動)
   3. fallback 完了時に **新しい delta link を取得** し `connector_cursors.cursor` を更新 (次回 sync から差分 mode に復帰)
3. **fallback 自体が失敗** — Graph API への接続失敗 / 認証エラー / 全件取得中の throttling 等で fallback も完遂しない場合、`ConnectorSyncFailed` event を append して fail-fast (本 ADR §責務 4 と整合)

`fallback_window_days` の設定例:

```toml
[connectors.teams]
fallback_window_days = 30  # default 30; 0 = disable fallback (非推奨)
```

採用理由:

- **Graph delta query の TTL 失効を構造的に吸収** — Microsoft Graph の delta link は documented TTL (公式は 30 日前後だが実値は変動) を持ち、long-tail で失効する。失効を手当てしないと operator が sync を再起動するまで「Teams chat が取り込まれない」状態が継続する
- **fallback で抜けを最小化** — 直近 N 日 full-pass で「失効中に発生したメッセージ」を最大限拾い直す
- **重複は append-only で吸収** — SourceObserved の dedup は projection 側で `external_id` (vendor message id) によって行われるため、fallback で同 message が再 append されても projection 側で 1 row に収束する。対で append される `ItemEnqueued` も `source_ref` 冪等 (§不変条件 7、issue #522) で inbox 側 1 行へ収束するため、fallback の再 append は inbox を二重化しない (本 ADR §責務 4 / 不変条件 7 と整合)
- **fallback_window_days 上書きで運用調整** — 1 年以上の長期 outage 後の re-onboarding 等で `fallback_window_days = 365` 等の一時設定が可能

本契約 §(c) は Graph delta query を持つ全 connector (Phase 7 ms365 outlook / onedrive / Phase 11 teams) に適用される。Phase 7 既存 connector への適用は Phase 11 で **forward-compat** に追加 (既存 cursor 値は opaque string として扱われ、TTL 失効を検知した時点で fallback が起動する、breaking change なし)。

Phase 18 改訂: `fallback_window_days` の `opshub.toml` 上書きは [ADR-0032](0032-runtime-toml-config-loading.md) の TOML 読込経路で実装される。

### Phase 11 改訂 (d) — Teams User Token principal (ADR-0014 keyring 経由、Bot Token は alternative)

Phase 11 Sub-issue F5 (#238) Teams connector の認証 principal を **User Token** に確定する。Slack ADR-0018 (`xoxp-` user token を採用、bot token は却下) と同パターン。

Teams User Token の運用:

1. **取得経路** — Azure Portal で App Registration を作成し `Chat.Read` / `ChannelMessage.Read.All` 等の delegated permissions を operator が consent → MSAL device code flow / interactive flow で User Token を取得
2. **保管経路** — ADR-0014 (SaaS Token Storage) の keyring 経路を再利用、key 規約は **単一 slot** `connector:teams:token` (`src/opshub/connectors/teams/auth.py` の `TEAMS_TOKEN_SECRET_KEY`)。User Token / 将来の Bot Token alternative は principal-neutral にこの 1 slot を共有する (Slack ADR-0018 と同パターン: user token と bot token を slot 分割せず operator が実際に保管している token を 1 slot で受ける)。env override は `OPSHUB_CONNECTOR_TEAMS_TOKEN` (CI / 緊急用)。refresh token を別 slot に保管しないため refresh はアプリ層では行わず、token 失効時は operator が `opshub connector auth set connector:teams` で再投入する経路に揃える
3. **scope** — minimum-required scope を `docs/teams-setup.md` (F6 で新設) に列挙、operator が consent screen で広 scope を許諾しないよう案内
4. **refresh** — Phase 11 F5 時点では single-slot 保管 (上記 2 と整合) のため、refresh token を keyring に別 slot で保管せず、token 失効時は `ConfigError` / Graph `401 InvalidAuthenticationToken` を契機に operator が `opshub connector auth set connector:teams` で新 token を再投入する経路に揃える。将来 MSAL の `acquire_token_silent` を取り込む場合は本 ADR を改訂し refresh token 用 slot を追加する
5. **Bot Token は alternative** — 一部企業環境で User Token consent が拒否される / app registration が許可されない場合、Bot Token (Application permissions) で代替する経路を `docs/teams-setup.md` に記載。ただし default は User Token

採用理由 (Slack ADR-0018 と同根拠):

- **operator 1 名スケールが OpsHub の前提** — Bot は team-level identity で「個人アシスタント」境界に合わない、User Token なら operator の own context (自身が参加している channels のみ) を自然に表現
- **書き戻し非対応 (Phase 10 改訂 §禁止事項 7) との整合** — User Token は read scope のみ要求すれば足り、write scope を持たないことで「経路の不在」を keyring 設定段階から強制可能
- **Slack / Outlook / OneDrive と principal パターンが揃う** — Phase 7-11 で全 SaaS connector が User Token principal に揃い、operator のメンタルモデルが 1 つ (consent → keyring 保管 → refresh)
- **app registration ハードルが高い環境への退路** — Bot Token alternative を docs に明記することで、企業 IT policy で User Token consent が阻まれる operator にも経路を残す (Phase 11 では Bot Token 経路の test pin は不要、code path を Optional に予約)

採用理由が成立する根拠は ADR-0018 §Decision と全く同じため詳細は ADR-0018 を参照。本 ADR §Phase 11 改訂 (d) は ADR-0018 の Slack User Token 確定を **Teams にも適用する確認** にとどまる。

## Phase 13 改訂 (Sub-issue G1、2026-05-31)

Phase 13 (epic #274) で Google Workspace 新コネクタ (Docs / Slides / Sheets) を導入するにあたり、本 ADR を改訂し以下 4 点を追加する (Phase 10 改訂節 §禁止事項 7 / Phase 11 改訂節 (a)-(d) は **保持**、本節は **加算改訂**)。

### Phase 13 改訂 (e) — Google Workspace 新コネクタを契約対象に追加

Phase 13 Sub-issue G3 (#277) で **`connectors/google_workspace/` connector** を新設し、本 ADR の `Connector` Protocol + 責務 1-6 + 禁止事項 1-7 をそのまま適用する。Google Drive API v3 `changes.list` 経由で Docs / Slides / Sheets の metadata + delta を fetch し、`source_type="google_doc"` / `"google_slides"` / `"google_sheets"` で `sources` projection に persist する。

- **Protocol signature 変更なし** — Phase 3 で確定し Phase 7-12 で適合済の Connector Protocol を再利用 (本 ADR §Phase 11 改訂 (a) と同じ追加パターン)
- **本 ADR の禁止事項 1-7 すべて適用** — Task / Decision / Link 直接生成禁止 / projection 直接更新禁止 / Application Service 経由必須 / vendor 固有 event 名禁止 / write-back ban (§禁止事項 7) を Google Workspace にも継承。**Drive write API (`files.update` / `files.create` / `files.copy` / `comments.create` / `permissions.*`) を connector に実装しない** ことで構造的に書き戻し経路を不在にする (§禁止事項 7 の Google Workspace への自然延長)
- **能動性禁止の延長: Drive push notification (`files.watch`) 禁止** — Google Drive API は `changes.watch` / `files.watch` で push channel を張る経路を持つが、本 ADR では **`changes.list` poll のみに制限**。push 経路は能動性混入 (Phase 14+ 段階 3 通知層の領域) であり、形 A (能動性なし) の本 phase scope に抵触する。`files.watch` を呼ぶ code path を connector に実装しないことで構造的に不在を保証
- **MS365 / Box / Slack ADR-0018 と principal パターンを揃える** — 後述 Phase 13 改訂 (h) を参照

### Phase 13 改訂 (f) — Workspace export 経路の本文抽出契約

Phase 13 Sub-issue G2 / G4 (#276 / #278) で Google Workspace 由来文書 (Docs / Slides / Sheets) の本文を取り込むにあたり、本 ADR §責務 1-2 (external API fetch + source normalization) と §責務 6 (body の minimization) を **Workspace export 経路に延伸** する形で本文抽出契約を pin する (Phase 11 改訂 (b) の local-FS-backed connector 経路に対する Web API 経路の対応物)。

本文抽出契約の流れ (Workspace export 経路):

```text
external metadata fetch (Drive API v3: files.get + changes.list)
  → diff detection (delta page token との比較)
  → 拡張子相当の Google mimeType マッチ + content_extraction = true の場合のみ
    → Drive API files.export(fileId, mimeType=<Office mediatype>) で binary export
      ├─ application/vnd.google-apps.document   → MS Word (.docx) として export
      ├─ application/vnd.google-apps.presentation → MS PowerPoint (.pptx) として export
      └─ application/vnd.google-apps.spreadsheet  → MS Excel (.xlsx) として export
    → 抽出 (core/document_extract.extract(path or bytes) 経由、markitdown 単独経路、ADR-0025 §決定 (a))
    → fail-safe (例外時は body=None、ADR-0025 §決定 (c))
  → SourceObserved with (body + provenance_origin + provenance_trust) を Application Service 経由で append
```

抽出層の不変条件:

1. **抽出経路は ADR-0025 §決定 (a) の markitdown 1 本** — connector が直接 `python-docx` / `openpyxl` / `python-pptx` を import / 呼び出すことを禁止 (`core/document_extract.py` 1 module に集中化)。Phase 11 改訂 (b) の不変条件をそのまま継承
2. **Google ネイティブ markdown export は使わない** — Docs は `text/markdown` 直接 export を持つが、Sheets / Slides は markdown 直接 export 非対応で、3 source_type の export 経路が分岐する。`core/document_extract.py` の API 表面 1 経路 (markitdown 経由) を保つために **3 形式とも MS Office mediatype 経由で統一**
3. **size 上限 / text 上限 / cells 上限は ADR-0025 §決定 (b)(e) で pin** — connector ごとに独自上限を上書きしない。ただし Workspace export 由来文書は元 file size 概念が異なる (Google 側 native fmt → export 後 size) ため、cap 適合性は Phase 13 plan OQ9 で実測後に必要なら `[office.google_workspace] max_file_size_mb` separate override 導入 (本 ADR では separate override の存在可能性のみ pin、defaults は ADR-0025 を継承)
4. **抽出失敗は fail-safe で SourceObserved 発行継続** — ADR-0025 §決定 (c) の `body=None` + warning log + summary 注記契約を Google Workspace connector でも共通 (Phase 11 改訂 (b) §不変条件 3 をそのまま継承)
5. **source_type は形式別 3 種** — `google_doc` / `google_slides` / `google_sheets` (ADR-0025 改訂 §決定 (d') で pin)。Drive API が返す Google mimeType (`application/vnd.google-apps.*`) → source_type のマッピングは connector の責務 (本 ADR §責務 2 source normalization)、`core/document_extract.py` 側は形式を意識しない (markitdown 1 経路)

text-only 本文取り込み (Slack / Outlook / Teams chat) との関係は Phase 11 改訂 (b) と同じ。Google Workspace の Docs / Slides / Sheets はバイナリ export 経由なので `core/document_extract.py` を介する側 (Phase 11 改訂 (b) §バイナリ文書経路と同型)。

### Phase 13 改訂 (g) — Drive `changes.list` cursor + TTL 失効時 full-pass fallback 義務

Phase 13 Sub-issue G3 (#277) Google Workspace connector で Drive API v3 `changes.list` page token を cursor として使うにあたり、Phase 11 改訂 (c) の **delta-link cursor + 失効時 full-pass fallback** と同パターンを Google Workspace にも適用する (Microsoft Graph delta query と Drive API changes は別 API だが cursor 戦略は構造的に同型)。

`changes.list` cursor の運用:

1. **正常時** — `files.list` (initial sync) で start page token を取得 → `changes.list?pageToken=<token>` で差分のみ取得、`nextPageToken` / `newStartPageToken` を `connector_cursors.cursor` に opaque string で永続化
2. **TTL 失効時** — Drive API が `400 invalidToken` / `404 startPageToken expired` / `410 Gone` 系エラーを返した場合、**自動 fallback**:
   1. `WARNING` log: `event="connector.changes_list.expired"`, `connector=google_workspace`, `since=<original_page_token>` (page token 本体は opaque で credential を含まないが安全側で sanitised)
   2. **full-pass** モードで直近 N 日 (`fallback_window_days`、default `30`、`opshub.toml` 上書き可) を `files.list` + `modifiedTime >= <since>` filter で fetch、各 file を SourceObserved として append (重複は SourceObserved の dedup key で吸収、append-only の自然な挙動)
   3. fallback 完了時に **新しい start page token を取得** し `connector_cursors.cursor` を更新 (次回 sync から差分 mode に復帰)
3. **fallback 自体が失敗** — Drive API への接続失敗 / 認証エラー / 全件取得中の throttling 等で fallback も完遂しない場合、`ConnectorSyncFailed` event を append して fail-fast (本 ADR §責務 4 と整合)

`fallback_window_days` の設定例:

```toml
[connectors.google_workspace]
fallback_window_days = 30  # default 30; 0 = disable fallback (非推奨)
```

採用理由:

- **Drive API `changes.list` の token 失効を構造的に吸収** — Drive API の start page token は documented TTL (公式は 30 日前後、実値は変動) を持ち、long-tail で失効する。失効を手当てしないと operator が sync を再起動するまで「Google Workspace 文書が取り込まれない」状態が継続する
- **Phase 11 改訂 (c) と完全同型** — Microsoft Graph delta query と Drive `changes.list` は別 API だが cursor 戦略 (opaque token + TTL + full-pass fallback) は構造的に同型。Phase 11 改訂 (c) の cursor 運用を Google Workspace にもそのまま適用することで operator のメンタルモデルを 1 つに保つ
- **重複は append-only で吸収** — SourceObserved の dedup は projection 側で `external_id` (Drive file id) によって行われるため、fallback で同 file が再 append されても projection 側で 1 row に収束する。対で append される `ItemEnqueued` も `source_ref` 冪等 (§不変条件 7、issue #522) で inbox 側 1 行へ収束するため、fallback の再 append は inbox を二重化しない (本 ADR §責務 4 / 不変条件 7 と整合)
- **fallback_window_days 上書きで運用調整** — 1 年以上の長期 outage 後の re-onboarding 等で `fallback_window_days = 365` 等の一時設定が可能

本契約 §(g) は Drive API `changes.list` を持つ全 connector (Phase 13 google_workspace、将来追加され得る Google Drive 系 connector) に適用される。

### Phase 13 改訂 (h) — Google Workspace User Token principal = MS365 / Box pattern (Teams pattern とは別系統)

Phase 13 Sub-issue G3 (#277) Google Workspace connector の認証 principal を **User Token (OAuth 2.0 Refresh Token + offline access + 自前 refresh + rotation 書き戻し)** に確定する。これは **MS365 / Box pattern** であり、Phase 11 改訂 (d) で Teams に採用した **verbatim user token + アプリ層 refresh なし** pattern とは **別系統** である。本節は両 pattern が ADR-0010 内に並立することを明文化する。

Google Workspace User Token の運用:

1. **取得経路** — GCP Console で OAuth Client (Desktop App credential) を作成し `https://www.googleapis.com/auth/drive.readonly` (Phase 13 plan OQ6 確定) delegated scope を operator が consent → OAuth 2.0 paste-code flow (MS365 / Box と同型、`opshub connector auth set google_workspace` 経由) で Authorization Code → Refresh Token + initial Access Token を取得。`access_type=offline` + `prompt=consent` パラメータで Refresh Token 取得を保証
2. **保管経路** — ADR-0014 (SaaS Token Storage) の keyring 経路を再利用、key 規約は **単一 slot** `connector:google_workspace:refresh_token` (`src/opshub/connectors/google_workspace/auth.py` の `GOOGLE_WORKSPACE_REFRESH_TOKEN_SECRET_KEY`)。env override は `OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN` (CI / 緊急用)。ADR-0014 §Phase 7 Validation 節の rotation pin リストに `connector:google_workspace:refresh_token` を追加 (MS365 / Box に続く 3 件目、Phase 13 plan で本 ADR と同時に ADR-0014 改訂)
3. **scope** — `drive.readonly` 単独 (Phase 13 plan OQ6 確定)。`drive.metadata.readonly` は `drive.readonly` の subset なので併記しない (operator IT consent UX の改善 + 過剰 scope フラグ回避)。`drive.activity.readonly` も `changes.list` poll のみなら不要。`docs/google-workspace-setup.md` (G5 で新設) に consent screen 設定手順を記載
4. **refresh = MS365 / Box pattern** — Google OAuth 2.0 Refresh Token は **rotation** される (毎 access token 取得 / refresh で新 refresh token が返り得る)。`src/opshub/connectors/google_workspace/auth.py` で `requests.post(token_endpoint, ...)` 相当の token refresh callback を実装し、新 refresh token が返るたびに **即 keyring (または env var override 経路) に書き戻す**。MS365 (`acquire_token_by_refresh_token` + `store_tokens` コールバック) / Box (Box SDK の `store_tokens` コールバック) と同パターン
5. **rotation pin test 必須** — Phase 13 Sub-issue G3 / G5 DoD として `tests/unit/connectors/google_workspace/test_auth.py::test_get_access_token_persists_rotated_refresh_token` を **MS365 / Box の同型 test と完全に対応** する形で配置 (forget regression 防止)。ADR-0014 §Phase 7 Validation の MS365 / Box test と並列で言及

**Teams pattern (Phase 11 改訂 (d)) との対比** — 両 pattern が ADR-0010 内に並立することを明示する:

| 項目 | MS365 / Box / Google Workspace pattern (Phase 13 改訂 (h)) | Teams pattern (Phase 11 改訂 (d)) |
|---|---|---|
| token slot | refresh_token を keyring に保管、access token は memory cache | user token を keyring に保管、refresh token は別 slot なし |
| アプリ層 refresh | あり (`acquire_token_by_refresh_token` 相当 + rotation 書き戻し) | **なし** (verbatim token を Graph API に投げる) |
| rotation 書き戻し | あり (毎 refresh ごとに `set_secret(REFRESH_TOKEN_KEY, new_rt)`) | なし (token 失効時は operator が再投入) |
| rotation pin test | 必須 (`test_get_access_token_persists_rotated_refresh_token`) | なし (該当 code path 不在) |
| env override | `*_REFRESH_TOKEN` (MS365 / Box / Google Workspace) | `*_TOKEN` (Teams) |

採用理由 (Google Workspace = MS365 / Box pattern 選択):

- **Google Drive API の前提が refresh token + access token 分離** — Drive API access token は documented 1 hour TTL で短命、refresh token を offline access 取得で受けてアプリ層 refresh するのが Google OAuth 2.0 の標準運用。MS365 / Box と完全同型
- **MSAL / Box SDK 同様の rotation 書き戻し義務** — Google も refresh token rotation を行うため、書き戻しを忘れると次回 refresh で失効する。MS365 / Box で確立した `store_tokens` コールバックパターンをそのまま適用
- **Teams pattern を採用しない理由** — Teams は MSAL device code / interactive flow で user token を verbatim 取得する経路を採り、refresh token は別 slot に保管しない (Phase 11 改訂 (d) §運用 4)。Google Workspace ではこの単純化が成立しない (refresh token を保管しないと毎回 paste-code flow が必要になり operator UX が極端に劣化)
- **operator メンタルモデルの一貫性** — Phase 7-13 の SaaS connector で principal pattern が 2 系統 (MS365 / Box / Google Workspace と Teams) に整理され、それぞれ「refresh token あり / verbatim token のみ」で明確に区別できる。Slack / Outlook / OneDrive / Google Workspace の 4 connector は同経路 (consent → keyring 保管 → refresh)、Teams のみ別経路 (consent → keyring 保管 → 失効時再投入)

採用理由の根拠は ADR-0014 §Phase 7 Validation MS365 / Box validation と全く同じため詳細は ADR-0014 を参照。本 ADR §Phase 13 改訂 (h) は ADR-0014 の MS365 / Box pattern を **Google Workspace にも適用する確認** + **Teams pattern との並立を明文化** にとどまる。

## Phase 14 改訂 (Sub-issue G1、2026-05-31)

Phase 14 (epic #292) で Gmail + Google Calendar 新コネクタを Phase 13 で確立した Google OAuth principal の流用 (scope 拡張: `drive.readonly` → `drive.readonly + gmail.readonly + calendar.readonly`) として導入するにあたり、本 ADR を改訂し以下 5 点を追加する (Phase 10 改訂節 §禁止事項 7 / Phase 11 改訂節 (a)-(d) / Phase 13 改訂節 (e)-(h) は **保持**、本節は **加算改訂**)。

### Phase 14 改訂 (i) — Gmail + Google Calendar 新コネクタを契約対象に追加

Phase 14 Sub-issue G3 / G4 (#295 / #296) で **`connectors/google_mail/` + `connectors/google_calendar/` connector** を新設し、本 ADR の `Connector` Protocol + 責務 1-6 + 禁止事項 1-7 をそのまま適用する。

- **Gmail connector** — Gmail API v1 `users.messages.list` (initial sync) + `users.history.list` (delta) 経由で message 単位を fetch し、`source_type="gmail_message"` で `sources` projection に persist
- **Google Calendar connector** — Google Calendar API v3 `events.list(syncToken=...)` 経由で master event を fetch し、`source_type="google_calendar"` で `sources` projection に persist。recurring event の override (Google API が独立 event として返す `recurringEventId` + `originalStartTime` 持ち) は別 record として retain (本 ADR §責務 4 と整合、master event instance の動的展開は projection 層の責務として Phase 15+ defer)
- **Protocol signature 変更なし** — Phase 3 で確定し Phase 7-13 で適合済の Connector Protocol を再利用 (本 ADR §Phase 11 改訂 (a) / §Phase 13 改訂 (e) と同じ追加パターン)
- **本 ADR の禁止事項 1-7 すべて適用** — Task / Decision / Link 直接生成禁止 / projection 直接更新禁止 / Application Service 経由必須 / vendor 固有 event 名禁止 / write-back ban (§禁止事項 7) を Gmail / Calendar にも継承。**Gmail send API (`users.messages.send` / `users.drafts.create` / `users.drafts.send`) / Calendar write API (`events.insert` / `events.update` / `events.delete` / `events.patch`) を connector に実装しない** ことで構造的に書き戻し経路を不在にする (§禁止事項 7 の Gmail / Calendar への自然延長)
- **能動性禁止の延長: Gmail / Calendar push notification (`users.watch` / Calendar `events.watch`) 禁止** — Phase 13 改訂 (e) で Drive `files.watch` を禁止した方針と同型。Gmail / Calendar push channel を張る code path を connector に実装しないことで構造的に不在を保証 (能動性混入は形 A scope 抵触)。`history.list` poll / `events.list(syncToken)` poll のみに制限
- **Google OAuth principal は Phase 13 改訂 (h) を流用** — Gmail / Calendar 専用 keyring slot を追加せず、`connector:google_workspace:refresh_token` の 1 slot を 3 connector (Drive / Gmail / Calendar) が共有 (詳細は本 ADR §Phase 14 改訂 (m) を参照)

### Phase 14 改訂 (j) — delta-cursor 型 connector 全般への TTL fallback 一般化 (改訂 (g) generalize)

Phase 11 改訂 (c) で Microsoft Graph delta query 限定として導入し、Phase 13 改訂 (g) で Drive API `changes.list` page token に拡張した **delta-cursor + 失効時 full-pass fallback 義務** を、Phase 14 で **delta-cursor 型 connector 全般 (Drive `changes.list` / Gmail History API / Calendar sync token / 後続 delta-cursor 型 connector)** に generalize する。本契約 §(j) は §改訂 (c) / §改訂 (g) の SSOT を統合する位置付けで、両条文は本契約 §(j) の specialization として継続有効。

統一規約 (本 ADR が pin する delta-cursor 型 connector 全般の共通契約):

1. **正常時** — vendor 固有の delta token / page token / sync token (opaque string) を `connector_cursors.cursor` に永続化。次回 sync は token 起点で差分のみ取得
2. **TTL 失効時** — vendor 側が「token expired」相当のエラーを返した場合 (vendor 別 status code は下表)、**自動 fallback**:
   1. `WARNING` log: `event="connector.<cursor_kind>.expired"`, `connector=<name>`, `since=<original_token>` (token は credential を含み得るため sanitised、未含有でも安全側で sanitised)
   2. **full-pass** モードで直近 N 日 (`fallback_window_days`、default `30`、`opshub.toml` 上書き可) を vendor 固有の time-window query (e.g. Gmail `q="after:..."` / Calendar `timeMin` / Drive `modifiedTime >= ...`) で fetch、各 record を SourceObserved として append (重複は SourceObserved の dedup key で吸収、append-only の自然な挙動)
   3. fallback 完了時に **新しい cursor token を取得** し `connector_cursors.cursor` を更新 (次回 sync から差分 mode に復帰)
3. **fallback 自体が失敗** — vendor API への接続失敗 / 認証エラー / 全件取得中の throttling 等で fallback も完遂しない場合、`ConnectorSyncFailed` event を append して fail-fast (本 ADR §責務 4 と整合)

vendor 別の TTL 失効検知 trigger:

| Connector | API / cursor 種別 | TTL 失効 trigger | `event` log 名 |
|---|---|---|---|
| ms365 (Outlook / OneDrive) / teams | Microsoft Graph delta query | `410 Gone` / `invalidatedDeltaToken` 系 | `connector.delta_link.expired` |
| google_workspace | Drive API `changes.list` page token | `400 invalidToken` / `404 startPageToken expired` / `410 Gone` | `connector.changes_list.expired` |
| google_mail (Phase 14) | Gmail API History API (`users.history.list`) | `404 historyId not found` / `410 Gone` (7 日 TTL、Google documented) | `connector.history.expired` |
| google_calendar (Phase 14) | Google Calendar API `events.list` syncToken | `410 Gone` (sync token invalidated) | `connector.sync_token.expired` |
| 後続 delta-cursor 型 connector | TBD | 各 vendor の token expiration 仕様に従う | `connector.<kind>.expired` |

`fallback_window_days` の設定例:

```toml
[connectors.google_mail]
fallback_window_days = 30  # default 30; 0 = disable fallback (非推奨)

[connectors.google_calendar]
fallback_window_days = 30  # default 30; 0 = disable fallback (非推奨)
```

Gmail / Calendar の time-window fetch 詳細:

- **Gmail full-pass**: `users.messages.list?q="after:<unix_ts>"` で `now - fallback_window_days` 以降の message を全件取得。`users.messages.get(id, format="full")` で payload を取り直し SourceObserved emit
- **Calendar full-pass**: `events.list?timeMin=<now - past_window>&timeMax=<now + future_window>` で window 内の event を全件取得。window 設計は Phase 14 plan OQ11 (default は過去 90 日 + 未来 365 日、`opshub.toml` 上書き可) で確定

採用理由 (一般化の根拠):

- **delta-cursor 型は構造的に同型** — Drive / Gmail / Calendar / Graph delta は API は異なれど cursor 戦略 (opaque token + TTL + full-pass fallback) は構造的に同型。connector ごとに別契約として書き分けると 4 つの「ほぼ同じ条文」が ADR に並ぶ。本 §(j) で 1 条文化することで operator メンタルモデルを 1 つに保つ
- **Phase 11 改訂 (c) / Phase 13 改訂 (g) との関係** — 本 §(j) は両条文の generalization であり、両条文は本 §(j) の specialization として継続有効。改訂 (c) は Microsoft Graph delta query 限定の歴史的経緯を、改訂 (g) は Drive `changes.list` 限定の歴史的経緯を凍結する目的で残置
- **Phase 15+ で追加され得る後続 delta-cursor 型 connector** — Notion (last_edited_time + cursor) / Jira (issue search since updated) / Confluence (cql with lastModified) 等の後続 connector が delta-cursor 型を採る場合、本 §(j) が自動的に契約として適用される (改訂不要)

### Phase 14 改訂 (k) — 本文抽出契約: Outlook 流継承 (Gmail / Calendar)

Phase 14 Sub-issue G3 / G4 (#295 / #296) で Gmail / Google Calendar の本文を取り込むにあたり、本 ADR §責務 1-2 (external API fetch + source normalization) と §責務 6 (body の minimization) を **Outlook 流 (Phase 11 ms365 outlook mapper) を継承する形** で pin する。本契約 §(k) は Phase 11 改訂 (b) の markitdown 経路 (バイナリ文書) とは **別系統** であり、text-only 本文取り込み経路の Outlook / Slack / Teams chat ファミリーに Gmail / Calendar を加える位置付け。

Gmail 本文取り込みの不変条件:

1. **text/plain 優先 → text/html 生保持** — Gmail API `users.messages.get(format="full")` payload を mapper が parse、`text/plain` part が存在すればそれを `SourceObserved.body` に載せる。text/plain が不在で text/html のみの場合は HTML を **生のまま** body に載せる (markitdown / html2text 等の中間変換を **しない**)。HTML → markdown 変換は host LLM / skill 側の rendering 責務 (Outlook と完全 symmetric、mapper / skill 側の logic 分岐を防ぐ)
2. **添付 retain なし** — Gmail attachments (`payload.parts[].body.attachmentId`) は Phase 14 scope では **取り込まない**。`users.messages.attachments.get` を connector が呼ばない。添付 retain は Phase 15+ で `users.messages.attachments.get` + markitdown 経路 (ADR-0025 拡張) として別 Phase で切る
3. **threadId は field 保持、replied_to link 化は Phase 15+ defer** — Gmail `threadId` は `SourceObserved` の field として保持 (event store immutability 整合、thread 単位の dynamic 集約は projection 層の責務として後続 Phase)。Gmail thread 単位の source_type (`gmail_thread` 等) は **作らない** (message 単位 `gmail_message` で固定、Outlook と symmetric)
4. **label は body 埋め込みのみ (構造化 field 追加なし)** — Gmail `labelIds` は mapper が body の冒頭に `[Labels: <label1>, <label2>, ...]` 形式で prepend するのみ。SourceObserved に `labels` field を追加しない (Outlook の `attendees_count` 流に揃える、domain 改変なし)
5. **body 上限** — Outlook 流の `[gmail body truncated: N / M chars]` tag を mapper layer で。閾値は Phase 14 plan OQ10 で確定 (Outlook と揃えるか `[office.gmail] max_body_chars` separate override を切るか)

Google Calendar 本文取り込みの不変条件:

1. **summary フォーマット = `start_iso - end_iso (N attendees)`** — MS365 Calendar mapper (`ms365_calendar`) と完全 symmetric。例: `"2026-06-15T10:00:00Z - 2026-06-15T11:00:00Z (5 attendees)"`
2. **attendee email list / 議題 / 会議室は body 埋め込みのみ** — Calendar API `attendees[].email` / `description` / `location` は mapper が body に prepend / append する形で保持 (Outlook と同型)。SourceObserved に `attendees` field / `location` field を追加しない (構造化 filter は Phase 15+ defer)
3. **RRULE は field 保持、instance 展開は Phase 15+ projection 層** — Calendar API `recurrence[]` (RRULE / EXDATE 等) は SourceObserved の field として保持。master event のみを source として取り込み、recurring instance の動的展開は projection 層の責務として Phase 15+ で `ms365_calendar` / `google_calendar` 両 calendar 同時に切る (Phase 14 では projection 層 instance 展開を作らない)
4. **override は別 record として取り込み** — Google Calendar API は recurring event の修正済み instance を独立 event として返す (`recurringEventId` + `originalStartTime` を持つ)。これらを master event とは別 record として `SourceObserved` emit (Phase 14 plan OQ3 確定)
5. **markitdown 経由を要さない** — Outlook / Slack / Teams chat と同じく text-only 経路、`core/document_extract.py` は介在しない (Phase 11 改訂 (b) §バイナリ文書経路 / Phase 13 改訂 (f) §Workspace export 経路とは異なる系統)

text-only 本文取り込み経路の全体像 (Phase 14 時点):

| Connector | Source type | Body 構築経路 | markitdown 経由 |
|---|---|---|---|
| slack (Phase 7) | `slack_message` | vendor SDK plain text を直接 body 載せ | No |
| ms365 outlook (Phase 7 + Phase 11 改訂 (b)) | `ms365_outlook` | Graph API text/plain 優先 → text/html 生保持 | No |
| ms365 calendar (Phase 7) | `ms365_calendar` | Graph API summary + body 構造化 | No |
| teams (Phase 11 改訂 (a)) | `teams_message` | Graph delta query plain text | No |
| **google_mail (Phase 14)** | **`gmail_message`** | **Gmail API text/plain 優先 → text/html 生保持** | **No** |
| **google_calendar (Phase 14)** | **`google_calendar`** | **Calendar API summary + body 構造化** | **No** |
| box_drive / onedrive_drive (Phase 11) | `word_document` / `excel_spreadsheet` / `powerpoint_slide_deck` | local file → markitdown | Yes |
| google_workspace (Phase 13) | `google_doc` / `google_slides` / `google_sheets` | Drive API files.export → MS Office mediatype → markitdown | Yes |

### Phase 14 改訂 (l) — Gmail unit + Calendar unit + label / attendee 表現契約

Phase 14 Sub-issue G3 / G4 で取り込み単位を以下に確定する (Phase 14 plan OQ2 / OQ3 / OQ7 と整合)。本契約 §(l) は §(k) (本文抽出) と分離して unit 設計を独立 pin することで、将来の thread aggregation / instance 展開 projection 追加時の改訂 surface を明確化する目的で独立条文化。

1. **Gmail unit = message 単位 (`gmail_message`)** — Outlook (`ms365_outlook`) と symmetric。thread 単位 source_type (`gmail_thread` 等) は作らない、threadId は field 保持で表現 (Phase 14 改訂 (k) §不変条件 3)。event store immutability と摩擦するため (thread = 複数 message の動的集約、message append 毎に thread record を再書きすると event log が無限増殖)
2. **Calendar unit = master event のみ (`google_calendar`)** — MS365 Calendar (`ms365_calendar`) と symmetric。recurring instance の動的展開は projection 層の責務として Phase 15+ で両 calendar 同時に切る。RRULE は field 保持で表現 (Phase 14 改訂 (k) §不変条件 3)
3. **Calendar override = 別 record** — Google Calendar API が recurring の修正済み instance を独立 event として返す場合、これを master event とは別 record として `SourceObserved` emit (Phase 14 plan OQ3 確定)。`recurringEventId` + `originalStartTime` field で master との関係を保持
4. **Gmail label は summary / body 埋め込みのみ** — `labelIds` を body の冒頭に `[Labels: ...]` 形式で prepend するのみ、新 field 追加なし (Outlook の `attendees_count` 流、domain 改変なし)
5. **Calendar attendee は summary / body 埋め込みのみ** — `attendees[].email` を body に list として埋め込み、`attendees_count` も Outlook 同型で summary に `(N attendees)` 形式で出すのみ。新 field 追加なし

採用理由:

- **event store immutability 整合** — message 単位 / master event 単位は append-only の自然な単位、thread / instance 展開は集約 = derived state なので projection 層に分離するほうが ADR-0002 と素直
- **Outlook / MS365 Calendar との mapper symmetry** — Phase 14 plan §決定事項 §mapper symmetry 不変方針と整合、skill 側 (personal-brief / meeting-prep / next-actions) で source_type 別に分岐する logic を増やさず recall を均一に扱える
- **構造化 field 追加を避けるメリット** — `labels` / `attendees` / `location` を SourceObserved の field に持たせると Phase 15+ で migration が発生する、Phase 14 時点では body 埋め込みで足り、構造化 filter は Phase 15+ で需要顕在化時に切る
- **override 別 record の根拠** — master event を mutating する形で取り込むと event store の append-only 原則に反する、override = 別事象として SourceObserved emit するのが event-sourced と整合

### Phase 14 改訂 (m) — Google OAuth principal の scope 拡張 (shared auth foundation)

Phase 13 改訂 (h) で `connector:google_workspace:refresh_token` keyring slot 単独で Drive 専用 (`drive.readonly`) として確定した Google OAuth principal を、Phase 14 で **scope 拡張 (`drive.readonly + gmail.readonly + calendar.readonly`)** + **3 connector 共有 (Drive + Gmail + Calendar)** + **shared auth foundation 抽出 (`connectors/google_auth/auth.py`、G1 plan の `google_common` 仮置きから G2 着手時に rename 採用、下記要点 7)** に拡張する。本契約 §(m) は Phase 13 改訂 (h) の principal 設計を流用する位置付け (新 keyring slot 追加 **なし**、1 Google account = 1 principal を維持)。詳細は ADR-0014 §Phase 7 Validation 節を参照 (Phase 13 G1 で追加した google_workspace slot の scope 拡張を Phase 14 G1 で同節改訂)。

要点:

1. **keyring slot 単一性維持** — `connector:google_workspace:refresh_token` 1 slot を Drive / Gmail / Calendar 3 connector が共有。新 slot 追加なし (`connector:google_mail:refresh_token` / `connector:google_calendar:refresh_token` は **作らない**)。1 Google account = 1 principal が opshub アシスタント MVP の前提
2. **scope 拡張** — `drive.readonly` から `drive.readonly + gmail.readonly + calendar.readonly` に拡大 (Phase 14 plan OQ6 確定)。`gmail.metadata` / `gmail.modify` 等の追加 scope は要求しない (read-only 3 scope のみ、書き戻し ban § 禁止事項 7 との整合)
3. **scope 宣言 = `auth.py` 内固定 list** — connector ごとの subset 宣言 (`google_mail` は gmail.readonly のみ要求、等) は **不採用**。固定 list 案を採用 (Phase 14 plan §X §設計選択の trade-off 参照)
4. **env override 名称はそのまま** — `OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN` (CI / 緊急用) を Drive / Gmail / Calendar 3 connector が共有
5. **shared auth foundation = `connectors/google_auth/auth.py`** — Phase 13 で `src/opshub/connectors/google_workspace/auth.py` に置いた token lifecycle (refresh + rotation 書き戻し + paste-code flow) を Phase 14 G2 で **`src/opshub/connectors/google_auth/auth.py` に抽出**。Phase 13 既存の `google_workspace` connector は新場所から import に re-wire。token lifecycle pin test (`test_get_access_token_persists_rotated_refresh_token`) は shared 側 (`tests/unit/connectors/google_auth/test_auth.py`) に 1 本集約 (3 connector 分の重複を避ける)。**G1 時点では `google_common` を仮置き名としていたが、G2 着手時に下記 7 の rename 評価を実施し、catch-all 化リスクを回避するため `google_auth` を採用**
6. **既存 operator への影響 = 1 回 re-consent** — scope 拡張により Google OAuth は既存 refresh token を invalidate する。Phase 14 release 時に operator は `opshub connector auth set google_workspace` を再実行して全 scope を 1 回で取得 (詳細手順は `docs/upgrading.md` Phase 14 行 + `docs/google-workspace-setup.md` で記載、G5 で更新)
7. **命名は `google_auth` を採用** — G1 時点では `google_common` を仮置きとしていたが、G2 着手時に再評価し catch-all 化リスク (`google_common/cursor.py` / `google_common/mapper.py` を後付けで増やすと「Google 系の共通置き場」になり境界が曖昧化) を回避するため **`google_auth` に rename して採用**。Phase 14 範囲で shared 化対象は auth.py のみで、cursor / mapper / settings は 3 connector で独立であることが G2 着手時に確認できたため、責務を狭く明示する `google_auth` が最適 (Phase 14 plan §X.3 §設計選択の trade-off 参照)

採用理由 (新 slot 追加せず scope 拡張のみ):

- **1 Google account = 1 principal が opshub アシスタント MVP の前提** — operator 1 名前提 (ADR-0018 同根拠)、同一 Google account から Drive / Gmail / Calendar を取り込むのが自然な操作モデル
- **scope 拡張は Google OAuth incremental authorization で吸収可能** — 1 回 re-consent で全 scope を取得、operator UX への影響は scope 拡張時の 1 回のみ
- **connector ごと subset scope 宣言は overkill** — 「Gmail だけ使いたい / Drive だけ使いたい」 use case は connector enable / disable で表現可能、scope 単位の subset 宣言は実利用シーンとマッチしない
- **shared auth foundation の必然性** — token refresh + rotation 書き戻しを 3 connector に各々実装すると pin test も 3 つ並ぶ、shared 化で 1 本に集約することで「rotation 書き戻し忘れ regression」を構造的に防ぐ
- **Teams pattern との比較** — Teams は別 OAuth tenant (Microsoft Identity) + verbatim user token で別 keyring slot (`connector:teams:token`) を要する、Google は 1 OAuth tenant + 3 connector を 1 slot 共有が成立する非対称性は Microsoft / Google それぞれの OAuth エコシステムの違いに由来 (Microsoft は app registration 単位の token、Google は Google account 単位の token + scope 拡張)

採用理由の根拠は ADR-0014 §Phase 7 Validation 節 (Phase 13 G1 で追加した google_workspace slot を Phase 14 G1 で scope 拡張) と全く同じため詳細は ADR-0014 を参照。本 ADR §Phase 14 改訂 (m) は Phase 13 改訂 (h) の MS365 / Box pattern 適用を **scope 拡張 + 3 connector 共有 + shared auth foundation 抽出** に延伸する確認にとどまる。

## Phase 21 改訂 (Sub-issue A、2026-06-07)

Phase 21 (epic #504) で **Playwright ベースの browser read 層** ([ADR-0037](0037-browser-read-layer-playwright.md)) を新設し、ブラウザレンダリングを要する Web ページの本文を取り込む **web connector** を導入するにあたり、本 ADR を改訂し以下 2 点を追加する (Phase 10 改訂節 §禁止事項 7 / Phase 11 改訂節 (a)-(d) / Phase 13 改訂節 (e)-(h) / Phase 14 改訂節 (i)-(m) は **保持**、本節は **加算改訂**)。

### Phase 21 改訂 (n) — web 新コネクタを契約対象に追加 (crawler 非該当)

Phase 21 Sub-issue C (#507) で **`connectors/web/` connector** を新設し、本 ADR の `Connector` Protocol + 責務 1-6 + 禁止事項 1-7 をそのまま適用する。[ADR-0037](0037-browser-read-layer-playwright.md) の Playwright browser core (Chromium / headless / 専用 user-data-dir) でページをレンダリングし、rendered DOM → text 抽出した本文を `source_type="web_page"` で `sources` projection に persist する。

- **Protocol signature 変更なし** — Phase 3 で確定し Phase 7-14 で適合済の Connector Protocol を再利用 (本 ADR §Phase 11 改訂 (a) / §Phase 13 改訂 (e) / §Phase 14 改訂 (i) と同じ追加パターン)
- **`external_id` = 正規化 URL** — `external_id` は取得対象 URL を正規化 (scheme 小文字化 / 末尾スラッシュ規約 / fragment 除去 等、正規化規約の詳細は 21-C で確定) した文字列をそのまま使う。SHA hash しない (grep / `opshub source show <id>` / 人間 debug を可能にするため、box_drive の rel_path 識別 [ADR-0019](0019-local-filesystem-backed-connector.md) §決定 (c) と同方針)
- **summary = `<title>`** — ページの `<title>` を `SourceObserved.summary` に載せる (200 char cap、ADR-0005 互換)。`<title>` 不在時は正規化 URL を summary fallback とする (本 ADR §不変条件 6 の summary 不在時 title fallback と整合する形)
- **本文 = rendered DOM → text、500K char cap / fail-safe** — 本文抽出は [ADR-0037](0037-browser-read-layer-playwright.md) §決定 (d) で pin した契約 (rendered DOM → text + 500K char cap [ADR-0025](0025-office-document-content-extraction.md) §決定 (b) 継承 + 抽出失敗 fail-safe [ADR-0025](0025-office-document-content-extraction.md) §決定 (c) 継承) に従う。抽出後本文を `SourceObserved.body` に載せ、空本文時 (レンダリング失敗 / 本文ゼロ) は `body = summary` fallback で本 ADR §不変条件 6 (body 非空) を満たす
- **operator が明示登録した URL のみ取得 = crawler 非該当** — web connector は `[connectors.web] pages` に operator が明示列挙した URL のみを取得する。**リンク追跡 (取得したページ内の `<a href>` を辿る) / sitemap 巡回を行わない**。これは「外部 SaaS の全イベントを Task 化しない」(本 ADR §禁止事項 1) のと同根の能動性抑制であり、operator が観測対象を明示宣言する local-first posture ([ADR-0019](0019-local-filesystem-backed-connector.md) の operator 明示 root_path、Slack の operator 明示 channels と同型) を web にも適用する。crawler 化 (リンク追跡 / sitemap) は形 A (能動性なし) scope に抵触するため code path を実装しない
- **本 ADR の禁止事項 1-7 すべて適用** — Task / Decision / Link 直接生成禁止 / projection 直接更新禁止 / Application Service 経由必須 / vendor 固有 event 名禁止 / write-back ban (§禁止事項 7) を web connector にも継承。**ブラウザ操作系 (`page.click` / `page.fill` / `page.set_input_files` / form submit) を connector に実装しない** ことで構造的に書き戻し経路を不在にする (§禁止事項 7 の web への自然延長、[ADR-0037](0037-browser-read-layer-playwright.md) §決定 (f) 操作系 defer と整合)。能動性禁止の延長として **crawler (リンク追跡 / sitemap)** も実装しない (上記)

### Phase 21 改訂 (o) — delta API なし connector の fingerprint 変更検知契約 (ADR-0019 §決定 (d) pattern の web への適用)

web connector は **delta API を持たない** (Web ページに「前回からの差分」を返す標準 API はない)。Phase 11-14 で導入した delta-cursor 型 connector の TTL fallback 契約 (§Phase 14 改訂 (j)) は web には適用できない。代わりに、[ADR-0019](0019-local-filesystem-backed-connector.md) §決定 (d) で box_drive / onedrive_drive (local-FS-backed、これも delta API なし) に適用した **`SourceObserved.fingerprint` 列ベースの変更検知 pattern** を web connector にも適用する。

fingerprint 変更検知の流れ (web connector):

```text
[connectors.web] pages の各 URL について:
  external metadata fetch (Playwright でページを開く → rendered DOM → text 抽出)
  → fingerprint 計算 (抽出後本文の安定 hash、構成要素は 21-C で確定)
  → diff detection (prior_fingerprints.get(normalized_url) との比較、ADR-0019 §決定 (d) pattern)
    - 一致     → skip (SourceObserved 発火しない)
    - 不一致 or 不在 → SourceObserved (body + fingerprint + provenance) を Application Service 経由で append
```

fingerprint 契約の不変条件:

1. **`sources.fingerprint` 列 + `SourceObserved.fingerprint` field を再利用** — [ADR-0019](0019-local-filesystem-backed-connector.md) §決定 (d) で migration 0017 が追加した `sources.fingerprint TEXT NULL` 列と `SourceObserved.fingerprint: str | None = None` field をそのまま使う。web connector 専用の列 / event field を追加しない (schema 変更なし、backward-compat)
2. **scanner in-memory 比較 pattern を踏襲** — sync 開始時に `SELECT external_id, fingerprint FROM sources WHERE connector_name = 'web'` で prior_fingerprints を一括取得し、各 URL の取得結果と in-memory 比較する ([ADR-0019](0019-local-filesystem-backed-connector.md) §決定 (d) の `BoxDriveScanner.scan()` 手順と同型)
3. **fingerprint の構成要素は 21-C で確定** — box_drive は `f"{size}:{mtime_ns}"` (stat() のみ) だが、web は stat() 概念がないため抽出後本文 (rendered DOM → text) の安定 hash (例: 本文 text の SHA-256、あるいは 正規化後 text の hash) を採る。具体的な構成は 21-C (#507) で確定し本節に反映する。box_drive の「本文 read 禁止だから stat() のみ」制約 ([ADR-0019](0019-local-filesystem-backed-connector.md) §決定 (b)) は web には適用されない (web は本文取り込みが目的のため、本文 hash を取ってよい)
4. **削除追跡なし** — `[connectors.web] pages` から URL が外された場合、box_drive の削除追跡なし ([ADR-0019](0019-local-filesystem-backed-connector.md) §決定 (e)) と同方針で `SourceDeleted` 系 event を発火しない。prior row は projection に stale row として残る (event-sourced append-only の自然な帰結)
5. **false positive 受容** — 広告 / タイムスタンプ / CSRF token 等の動的要素で fingerprint が毎回変わり再 SourceObserved が発火し得る。「rendered text が変わった = 変更ありと観測」が agent 観点の semantics であり、box_drive の `touch` false positive 受容 ([ADR-0019](0019-local-filesystem-backed-connector.md) §決定 (d)) と同方針で受容する。動的ノイズの抑制 (本文正規化での noise 除去) が必要なら 21-C で fingerprint 構成側で扱う

採用理由:

- **delta API なし connector の変更検知 SSOT を踏襲** — box_drive / onedrive_drive (local-FS、delta API なし) で確立した `sources.fingerprint` 列ベース変更検知を web (Web API、delta API なし) にも適用することで、「delta API を持つ connector は cursor + TTL fallback (§改訂 (j))、delta API を持たない connector は fingerprint 列比較 (§改訂 (o))」という 2 系統が ADR-0010 内で明確に整理される
- **schema 変更不要** — [ADR-0019](0019-local-filesystem-backed-connector.md) §決定 (d) で既に追加済の `sources.fingerprint` 列 / `SourceObserved.fingerprint` field を再利用するため、migration / event schema 変更なし
- **event noise 抑制** — 毎回 SourceObserved を発火すると event log が膨らむため、fingerprint 一致時 skip で「本文が変わった URL のみ event 化」を成立させる ([ADR-0019](0019-local-filesystem-backed-connector.md) §決定 (d) と同根拠)

## 関連

- [Principles 7 (Connector Contract)](../principles.md)
- [Architecture 2.1 (Connector Layer)](../architecture.md)
- [ADR-0002: Event-Sourced Architecture](0002-event-sourced-architecture.md)
- [ADR-0004: Agent Runtime Boundary](0004-agent-runtime-boundary.md)
- [ADR-0005: External Content Minimization](0005-external-content-minimization.md) — Phase 10 で ADR-0020 が supersede 済、本文取り込み経路は ADR-0020 / ADR-0025 を参照
- [ADR-0014: SaaS Token Storage](0014-saas-token-storage.md) — Teams User Token も同経路で keyring 保管 (Phase 11 改訂 (d))
- [ADR-0018: Slack Connector Token Principal](0018-slack-token-principal.md) — Teams User Token principal は本 ADR と同根拠 (Phase 11 改訂 (d))
- [ADR-0019: Local-FS-backed Connector](0019-local-filesystem-backed-connector.md) — Phase 11 Sub-issue F1 で `content_extraction` opt-in 例外節 + onedrive_drive 汎化を同時改訂
- [ADR-0020: Full Local Content Retention](0020-full-local-content-retention.md) — 本文取り込みの根拠 (Phase 11 改訂 (b) で Office 文書経路に延伸)
- [ADR-0025: Office Document Content Extraction](0025-office-document-content-extraction.md) — Phase 11 Sub-issue F1 で新規起票、本 ADR §Phase 11 改訂 (b) 本文抽出契約の実装層を pin
- [Phase 11 Plan §2 ADR 構成 + §3 Sub-issue F](../phase-11-plan.md)
- [Phase 13 Plan §2 改訂 ADR + §3 Sub-issue G](../phase-13-plan.md) — Google Workspace コネクタ (本 ADR §Phase 13 改訂 (e)-(h))
- [Phase 14 Plan §2 改訂 ADR + §3 Sub-issue G](../phase-14-plan.md) — Gmail + Google Calendar コネクタ (本 ADR §Phase 14 改訂 (i)-(m))
- [ADR-0037: Browser Read Layer via Playwright](0037-browser-read-layer-playwright.md) — Phase 21 で新設する web connector の browser read 層 (Playwright / Chromium / rendered DOM → text)。本 ADR §Phase 21 改訂 (n)-(o) が web connector を契約対象に追加し、fingerprint 変更検知を web に適用する
- [ADR-0031: CLI Command Surface Organization](0031-cli-command-surface-organization.md) — connector の CLI dispatch surface (top-level group の組織方針 / noun-first / per-noun group) は ADR-0031 で確定。本 ADR の Connector Protocol + 責務 / 禁止事項 / 改訂 (a)-(o) は CLI 表面再編とは独立で **不変**
- [ADR-0041: Slack Multi-Workspace](0041-slack-multi-workspace.md) — Phase 24 で Slack の cursor schema を per-alias nest (`{"workspaces": {"<alias>": {...}}}`) に、`external_id` を `f"{team_id}:{channel_id}:{ts}"` (3-token) に改める。§Phase 7 validation の「Slack は per-channel JSON」の cursor 記述はこの nest の内側として継続有効。connector instance は単一 (`name = "slack"`) のまま、本 contract の責務 / 禁止事項は不変
