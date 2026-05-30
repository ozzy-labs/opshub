# 0010. Connector Contract

- Status: Accepted (revised 2026-05-31 for Phase 11 Sub-issue F1)
- Date: 2026-05-17 (initial); 2026-05-30 (Phase 10 §Write-back scope clarification: 当面 scope 外); 2026-05-31 (Phase 11 改訂: Teams 追加 + 本文抽出契約 + delta-link cursor + User Token principal)
- Deciders: ozzy

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
6. body 本文の minimization (ADR-0005 参照、summary / extracted action items のみ保持)

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
- **重複は append-only で吸収** — SourceObserved の dedup は projection 側で `external_id` (vendor message id) によって行われるため、fallback で同 message が再 append されても projection 側で 1 row に収束する (本 ADR §責務 4 と整合)
- **fallback_window_days 上書きで運用調整** — 1 年以上の長期 outage 後の re-onboarding 等で `fallback_window_days = 365` 等の一時設定が可能

本契約 §(c) は Graph delta query を持つ全 connector (Phase 7 ms365 outlook / onedrive / Phase 11 teams) に適用される。Phase 7 既存 connector への適用は Phase 11 で **forward-compat** に追加 (既存 cursor 値は opaque string として扱われ、TTL 失効を検知した時点で fallback が起動する、breaking change なし)。

### Phase 11 改訂 (d) — Teams User Token principal (ADR-0014 keyring 経由、Bot Token は alternative)

Phase 11 Sub-issue F5 (#238) Teams connector の認証 principal を **User Token** に確定する。Slack ADR-0018 (`xoxp-` user token を採用、bot token は却下) と同パターン。

Teams User Token の運用:

1. **取得経路** — Azure Portal で App Registration を作成し `Chat.Read` / `ChannelMessage.Read.All` 等の delegated permissions を operator が consent → MSAL device code flow / interactive flow で User Token を取得
2. **保管経路** — ADR-0014 (SaaS Token Storage) の keyring 経路を再利用、key 規約 `connector:teams:access_token` + `connector:teams:refresh_token`。env override は `OPSHUB_CONNECTOR_TEAMS_TOKEN` (CI / 緊急用)
3. **scope** — minimum-required scope を `docs/teams-setup.md` (F6 で新設) に列挙、operator が consent screen で広 scope を許諾しないよう案内
4. **refresh** — MSAL の refresh token + acquire_token_silent で透過的に refresh、refresh 失敗時は `ConnectorSyncFailed` event + setup docs を指す actionable error
5. **Bot Token は alternative** — 一部企業環境で User Token consent が拒否される / app registration が許可されない場合、Bot Token (Application permissions) で代替する経路を `docs/teams-setup.md` に記載。ただし default は User Token

採用理由 (Slack ADR-0018 と同根拠):

- **operator 1 名スケールが OpsHub の前提** — Bot は team-level identity で「個人秘書」境界に合わない、User Token なら operator の own context (自身が参加している channels のみ) を自然に表現
- **書き戻し非対応 (Phase 10 改訂 §禁止事項 7) との整合** — User Token は read scope のみ要求すれば足り、write scope を持たないことで「経路の不在」を keyring 設定段階から強制可能
- **Slack / Outlook / OneDrive と principal パターンが揃う** — Phase 7-11 で全 SaaS connector が User Token principal に揃い、operator のメンタルモデルが 1 つ (consent → keyring 保管 → refresh)
- **app registration ハードルが高い環境への退路** — Bot Token alternative を docs に明記することで、企業 IT policy で User Token consent が阻まれる operator にも経路を残す (Phase 11 では Bot Token 経路の test pin は不要、code path を Optional に予約)

採用理由が成立する根拠は ADR-0018 §Decision と全く同じため詳細は ADR-0018 を参照。本 ADR §Phase 11 改訂 (d) は ADR-0018 の Slack User Token 確定を **Teams にも適用する確認** にとどまる。

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
