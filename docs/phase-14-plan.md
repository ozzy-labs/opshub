# Phase 14 Implementation Plan

> Status: **Phase 14 in progress (2026-05-31 着手)**. Scope: **Gmail + Google Calendar コネクタ** = Phase 13 で確立した Google OAuth principal (`connector:google_workspace:refresh_token` keyring slot + offline access + 自前 refresh + rotation 書き戻し = MS365 / Box pattern) を流用し、scope を `drive.readonly` から `drive.readonly + gmail.readonly + calendar.readonly` に拡張。Gmail = Gmail API History API delta + message 単位 (Outlook と symmetric)、Calendar = Calendar API `events.list(syncToken=...)` + master event only + override 別 record (MS365 Calendar と symmetric)。Outlook 流の本文抽出方針 (text/plain 優先 → text/html 生保持、markitdown なし、添付 retain なし) を Gmail / Calendar に完全 symmetric 適用し mapper / skill 側 logic 分岐を防ぐ。形 A (能動性なし) + 外部書き戻しなしを Phase 10/11/12/13 から継承。
>
> Sub-issue は **G1〜G5 の 5 つ**（親 epic #292、子 #293〜#297）。**新規 ADR ゼロ、改訂 2 本**（ADR-0010 + ADR-0014）で吸収。Phase 11 / 12 / 13 流の単一改訂路線を踏襲 (Phase 11 = 1 新規 + 2 改訂、Phase 12 = 0 新規 + 3 改訂、Phase 13 = 0 新規 + 3 改訂、**Phase 14 = 0 新規 + 2 改訂**)。本 plan が SSOT であり、各 sub-issue body は要点抜粋。
>
> 本ドキュメントは **planning skeleton** であり、各 sub-issue の詳細設計・不変条件・最終 DoD は着手前に本 plan 内で確定する。実装契約（uow_factory / `EventStore.append` / `Projector.apply` / registry SSOT / cold-start guard / `core/sanitise.sanitise_error_message` / Pluggable backend Protocol freeze / Connector framework / `tests/_secrets.py` 連結ビルド規範 等、Phase 1-13 で確立）は Phase 14 も全て継承する。

Phase 14 の目的は、opshub の秘書射程を **Google 派 operator (Gmail + Google Calendar)** に広げることにある。具体的には Phase 13 で確立した Google OAuth principal + httpx 経路 + paste-code flow を流用し、Gmail (`users.history.list` 経由 delta) + Google Calendar (`events.list(syncToken=...)` 経由 delta) を取り込んで recall / search (FTS5) / personal-brief / meeting-prep / next-actions / reply-draft 系 skill が Gmail / Calendar 由来の情報を一級市民として扱えるようにする。

設計の中心は **Outlook 流の継承**: Phase 7 の `ms365_outlook` mapper + Phase 7 の `ms365_calendar` mapper を mirror として Gmail / Calendar mapper を実装することで、host LLM / skill 側に「Outlook と Gmail で挙動が違う」「MS365 Calendar と Google Calendar で挙動が違う」分岐ロジックを増やさず、mapper symmetry で recall を均一に扱える。本文抽出は markitdown 経由を **使わない** (Gmail / Calendar は text-only family、Phase 11 / 13 のバイナリ文書経路 = Office / Workspace export 経由とは別系統)。

---

## 1. 確定済み事項

### Open Question 決定（2026-05-31 設計セッション、親 epic #292 §決定事項）

| # | 論点 | 決定 |
|---|---|---|
| OQ1 | scope shape | **Gmail + Google Calendar 同時取り込み**。Phase 11 (Teams + Office) 流の複合 Phase。OAuth re-consent は 1 回、Outlook と並ぶ「Microsoft 派と Google 派両方の秘書補完」を一気に達成 |
| OQ2 | Gmail 単位 | **message 単位 `gmail_message`** (Outlook と symmetric)。threadId は field 保持、replied_to link 化は Phase 15+ defer。event store immutability 整合 |
| OQ3 | Calendar 単位 | **master event only `google_calendar`** (MS365 `ms365_calendar` と symmetric)。RRULE は field、override (Google API が独立 event として返す) は別 record として取り込む。instance 展開は Phase 15+ で projection 層 |
| OQ4 | 本文抽出 | **Outlook と完全 symmetric**: Gmail = text/plain 優先 → text/html 生保持、markitdown なし、添付 retain なし。Calendar = summary に `start_iso - end_iso (N attendees)`、attendee email list / 議題 / 会議室は body に追記 |
| OQ5 | cursor 戦略 | **Phase 13 TTL fallback pattern を delta-cursor 型一般に generalize**。Gmail History API 7 日 TTL 失効時 → `users.messages.list` で full-pass + WARN、Calendar `410 GONE` 失効時 → syncToken なしで再取得 + WARN。ADR-0010 §改訂 (g) を「Drive / Gmail / Calendar / 後続 delta-cursor 型 connector 全般」に拡張 = §改訂 (j) として一般化条文化 |
| OQ6 | OAuth principal | **既存 `connector:google_workspace:refresh_token` keyring slot 流用、scope 拡張**: `drive.readonly + gmail.readonly + calendar.readonly`。1 Google account = 1 principal、auth.py は `connectors/google_common/auth.py` に shared module として抽出 (Phase 13 google_workspace は新場所から import に re-wire)。**scope 宣言形式 = `auth.py` 内固定 list を全 Google connector が共通で要求**、connector ごとの subset 宣言は不採用 (overkill、connector enable / disable で実利用範囲は制御可能)。**`google_common` 命名は catch-all 化リスクあり、G2 着手時に `google_auth` / `google_oauth` への rename を再評価** (§X §設計選択の trade-off 参照) |
| OQ7 | label / attendee | **Outlook 流に揃える** (summary / body 埋め込みのみ、新 field なし)。SourceObserved 改変なし、migration 不要。構造化 filter は Phase 15+ defer |
| OQ8 | source_type | **`gmail_message` + `google_calendar`** (Outlook / ms365_calendar と symmetric)。**connector module 命名 (`google_mail` / `google_calendar`、`google_` prefix 統一) と source_type prefix (`gmail_` / `google_`) が不揃いになる点は Gmail のブランド名性を優先した結果**、§X §設計選択の trade-off で trade-off 明示 |
| OQ9 | ADR 構成 | **新規 ADR ゼロ、改訂 2 本 (ADR-0010 + ADR-0014)**。Phase 11 / 12 / 13 流の単一改訂路線踏襲。ADR-0025 は Gmail 添付を retain しないため改訂不要 |

### Phase 10〜13 から継承する不変方針

1. **形A**: opshub は MCP server + Agent Skills のみ提供、頭脳 (runtime) は外部ホスト
2. **能動性なし**: リクエスト駆動のみ、常駐・定期実行は Phase 15+。**Gmail / Calendar push notification (`users.watch` / Calendar `events.watch`) は禁止**、`users.history.list` / `events.list(syncToken)` poll のみ (Phase 13 で Drive `files.watch` 禁止と同型)
3. **外部書き戻しなし**: 取り込み + ローカル context 生成のみ。**Gmail send API / Calendar write API を connector に実装しない** (ADR-0010 §禁止事項 7 の Gmail / Calendar への自然延長)
4. **本文ローカル保持** + `provenance_origin="external"` / `provenance_trust="untrusted"` 付き (ADR-0020)
5. **SQLCipher 丸ごと暗号化 opt-in** (ADR-0021、keyring 経由)
6. **HITL boundary**: read tools は host LLM 自律 OK、write tools は host LLM が user 確認必須 (ADR-0022 annotation policy)
7. **`_secrets.py` 連結ビルド規範** を Gmail / Calendar fixture / mock にも適用
8. **M6 cold-start guard / `opshub --help` ≤ 300ms / plaintext-leak 検出 CI 常駐**
9. **mapper symmetry**: Outlook ↔ Gmail / ms365_calendar ↔ google_calendar の field / summary / body フォーマットを同形に保つ (Phase 14 plan §決定事項 §mapper symmetry)

### Phase 番号

**Phase 14**（top-level）。新 connector 2 vendor (`google_mail` + `google_calendar`) + 新 source_type 2 種 + ADR 改訂 2 本を伴う「新 connector category 2 vendor 同時追加 + shared auth foundation 抽出」が主目的。Phase 11 / 13 流の単一カテゴリ集中パターン (Phase 11 = MS Office 深掘り、Phase 13 = Google Workspace 深掘り) に揃え、Phase 13 plan §9 で forecast していた「画像 OCR / Drive Comments / Suggestions」を **Gmail + Google Calendar 同時追加** に再評価した (理由は親 epic #292 §Phase 13 plan §9 からの変更点に記載、G5 で `docs/phase-13-plan.md` §9 forecast を「Phase 14 = Gmail + Google Calendar に再評価済み」と書き戻す = Phase 13 audit R2-CROSS-06 同型ミス防止)。メモリ方針 [[phase-numbering-new-arch-pattern]] に整合 (新 connector category vendor 追加 + 新 source_type = Phase X+1 で起票)。

---

## 2. 改訂 ADR

> **新規 ADR ゼロ**。改訂 2 本のみ。Phase 11 / 12 / 13 流の単一改訂路線を踏襲し、既存 ADR の延伸条文として吸収する。**新規 ADR ゼロ方針** = (i) connector contract は ADR-0010 で完結 (Teams / Office 抽出 / Google Workspace も既存 ADR への加算改訂で対応した先例)、(ii) token storage は ADR-0014 §Phase 7 Validation 節 google_workspace slot の scope 拡大 + shared auth foundation 抽出方針追記で完結、(iii) ADR-0025 は Gmail 添付を retain しないため改訂不要、の 3 点による。

| ADR | 種別 | タイトル | 主な改訂内容 |
|---|---|---|---|
| **ADR-0010** | 改訂 | Connector Contract | **Phase 10 改訂 (write-back ban、§禁止事項 7) と Phase 11 改訂 (a)-(d) と Phase 13 改訂 (e)-(h) は保持** したまま、以下 5 点を加算追加：(i) Gmail + Google Calendar 新コネクタを契約対象に追加 + Gmail / Calendar push notification 禁止 / (j) delta-cursor 型 connector 全般 (Drive `changes.list` / Gmail History API / Calendar sync token / 後続 delta-cursor 型 connector) への TTL fallback 一般化、Phase 11 改訂 (c) + Phase 13 改訂 (g) の SSOT を統合 / (k) 本文抽出 = Outlook 流継承 (text/plain 優先 → text/html 生保持、markitdown なし、添付 retain なし) を Gmail / Calendar 適用先として明示 / (l) Gmail unit = message 単位 + Calendar unit = master event only + override 別 record + label / attendee は summary / body 埋め込みのみ (Outlook 流) を契約化 / (m) Google OAuth principal の scope 拡張 (`drive.readonly` → `drive.readonly + gmail.readonly + calendar.readonly`) + shared auth foundation `connectors/google_common/auth.py` 抽出方針 |
| **ADR-0014** | 改訂 | SaaS Token Storage | §Phase 7 Validation 節の `connector:google_workspace:refresh_token` slot の scope を Drive 専用 → Drive + Gmail + Calendar 全般 (drive.readonly + gmail.readonly + calendar.readonly) に拡大。env override `OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN` 名称はそのまま。新 slot 追加なし (1 Google account = 1 principal、3 connector 共有)。shared auth foundation `connectors/google_common/auth.py` への抽出方針を明示 (rotation pin test も shared 側に 1 本集約) |

---

## 3. Commit 順序（Sub-issue 骨子）

> 各 sub-issue を 1〜複数 PR に割る。詳細 PR 分割と DoD は着手前に確定。依存順に並べる。

### Sub-issue G1: ADRs (ADR-0010 + ADR-0014 改訂) + phase-14-plan + Phase 13 影響リフレクト (#293)

- 改訂 2 本 (ADR-0010 + ADR-0014) を 1 PR で Accepted
- `docs/phase-14-plan.md` 新規 (本ファイル) を main 追加
- `docs/decisions-log.md` entries (2 件)
- Phase 13 google_workspace への shared auth 影響を本 plan で明示 (G2 で実施、Phase 13 既存実装には Phase 14 G2 で re-wire のみ、Phase 14 G1 段階では docs に方針記載のみ)

**PR G1** `docs(adr,plan): adr-0010 + 0014 改訂 + phase-14-plan (Gmail + Google Calendar)`

### Sub-issue G2: shared auth foundation (connectors/google_auth/auth.py 抽出 + scope 拡張) (#294)

> G2 着手時に §X.3 の rename 評価を実施し、`google_common` 仮置きから **`google_auth`** に rename して採用済 (catch-all 化リスク回避、scope を狭く明示)。以下の `google_common` 表記は G1 時点の plan で使われていた仮置き名であり、最終的な実装は `google_auth/` に着地している。

- **依存: G1 のみ**
- `src/opshub/connectors/google_workspace/auth.py` を `src/opshub/connectors/google_auth/auth.py` に **物理移動** (Phase 13 既存実装の logic は無変更で物理場所のみ変更、G2 着手時に §X.3 の catch-all 化リスク再評価を行い `google_common` 仮置きから `google_auth` への rename を採用 = scope を狭く明示)
- scope を `drive.readonly` から `drive.readonly + gmail.readonly + calendar.readonly` に拡張 (`SCOPES` 定数 = 3 scope の固定 list、connector ごとの subset 宣言は不採用)
- `connectors/google_workspace/` 配下から新場所を import に re-wire (`from opshub.connectors.google_auth.auth import ...` 形、G2 採用名)
- token lifecycle pin test (`test_get_access_token_persists_rotated_refresh_token`) を `tests/unit/connectors/google_workspace/` から `tests/unit/connectors/google_auth/` に物理移動 + shared 側で 1 本に集約 (3 connector 分の重複防止、G2 採用名)
- CLI `opshub connector auth set google_workspace` の挙動を「全 Google scope (drive + gmail + calendar) を 1 回 paste-code flow で取得」に統一 (re-consent 1 回、operator UX 維持)
- `[connectors-google-common]` extras 名は **作らず**、既存 `[connectors-google-workspace]` extras を流用 (Phase 14 で extras 追加なし、G3 / G4 でも `[connectors-google-workspace]` 配下に Gmail / Calendar 依存を追加)
- `google_common` 命名は仮、G2 着手時に `google_auth` / `google_oauth` への rename を **本 PR 内で再評価** (catch-all 化リスク回避、§X §設計選択の trade-off と整合)
- unit tests (scope 引数化 / scope 拡張時の token 取得 / rotation 書き戻し pin / Phase 13 既存 google_workspace round-trip 維持)

**PR G2** `refactor(connectors/google_auth): shared auth foundation 抽出 + scope 拡張` (G2 着手時に §X.3 rename 評価で `google_common` → `google_auth` を採用)

### Sub-issue G3: Gmail connector (httpx + History API + message mapper) (#295)

- **依存: G2** (shared auth foundation が必要)
- `src/opshub/connectors/google_mail/` 新設、5 module 構成 (`cursor.py` / `client.py` / `mapper.py` / `connector.py` / `settings.py` + `__init__.py`、Phase 13 google_workspace 5 module 構成踏襲)
- httpx + Gmail API v1 `users.messages.list` (initial sync) + `users.history.list` (delta) + 7 日 TTL 失効時 full-pass fallback + WARN (ADR-0010 §Phase 14 改訂 (j))
- message 単位 mapper (Outlook と symmetric、body = text/plain 優先 → text/html 生保持、`[Labels: ...]` prepend、`[gmail body truncated: N / M chars]` tag)
- 新 source_type `gmail_message` literal を `domain/events/source.py` に登録 + 公開
- `opshub connector sync google_mail` CLI 統合 + `[connectors.google_mail] fallback_window_days = 30` settings
- rate limit 429 + 5xx exponential backoff retry (Phase 7-13 既存 connector と同パターン)
- tests: cursor (History API 正常 / 7 日 TTL 失効 / full-pass fallback + WARN) / client (rate limit 429 retry / httpx mock) / mapper (text/plain 優先 / text/html fallback / `[Labels: ...]` prepend / body truncation + tag / threadId field 保持)

**PR G3** `feat(connectors/google_mail): gmail api history + message mapper + cursor + fallback`

### Sub-issue G4: Google Calendar connector (httpx + sync token + master event mapper + override 別 record) (#296)

- **依存: G2** (shared auth foundation が必要、G3 と並列可能)
- `src/opshub/connectors/google_calendar/` 新設、5 module 構成 (G3 と同型)
- httpx + Calendar API v3 `events.list(syncToken=...)` + 410 GONE 失効時 full-pass fallback (syncToken なし + `timeMin` / `timeMax` window) + WARN (ADR-0010 §Phase 14 改訂 (j))
- master event mapper (MS365 Calendar と symmetric、summary = `start_iso - end_iso (N attendees)`、attendee email list / 議題 / 会議室は body)
- override (recurringEventId + originalStartTime) は別 record として SourceObserved emit (ADR-0010 §Phase 14 改訂 (l) §不変条件 3)
- 新 source_type `google_calendar` literal を `domain/events/source.py` に登録 + 公開
- `opshub connector sync google_calendar` CLI 統合 + `[connectors.google_calendar] fallback_window_days = 30` settings
- rate limit 429 + 5xx exponential backoff retry
- tests: cursor (sync token 正常 / 410 GONE 失効 / full-pass + timeMin/timeMax window + WARN) / client (rate limit 429 retry) / mapper (master event / override 別 record / RRULE field / attendee body 埋め込み / summary フォーマット)

**PR G4** `feat(connectors/google_calendar): events api sync token + master event mapper + override + cursor + fallback`

### Sub-issue G5: Phase 14 closeout (#297)

- **依存: G2 + G3 + G4 全てマージ済み**
- 設計 docs 一括 (§5)
- ユーザー docs 一括 (§6)
- e2e lifecycle test (§7.3)
- guard 確認 (§7.4)
- AGENTS.md / CLAUDE.md Status 行 Phase 14 complete
- `docs/phase-14-plan.md` Status header を `Phase 14 complete (YYYY-MM-DD)` に更新 (Phase 11 R2-CROSS-06 / Phase 12 / Phase 13 Status header pattern 継承)
- **`docs/phase-13-plan.md` §9 forecast を「Phase 14 = Gmail + Google Calendar に再評価済み」と書き戻す** (Phase 13 audit R2-CROSS-06 同型ミス防止、親 epic #292 §Phase 13 plan §9 からの変更点に記載済)
- G5 マージ後、main の CI workflow が green であることを確認 (Phase 10 #221 / Phase 11 / Phase 12 / Phase 13 hotfix 経験を踏まえた事前確認)

**PR G5** `docs: phase 14 closeout + e2e`

### Wave 配置（依存 DAG）

```text
Wave 1: G1                ← entry (#293)
Wave 2: G2                ← G1 (#294 shared auth foundation)
Wave 3: G3 / G4 (並列)     ← G2 (#295 Gmail / #296 Calendar)
Wave 4: G5                ← G2 + G3 + G4 (#297 closeout)
```

drive 例: `/drive --merge #293 -> #294 -> #295,#296 -> #297`（Wave 3 で G3/G4 並列、Wave 4 で G5 closeout）

**並列性の根拠**: G2 (shared auth foundation 抽出) は G3 / G4 の前提となるため Wave 2 で単独。G3 (Gmail) と G4 (Calendar) は shared auth を共有するが mapper / cursor / source_type / settings はすべて独立 (Gmail と Calendar は別 API、別 endpoint、別 cursor 戦略)、Wave 3 で並列可能。Phase 13 Wave 2 (G2/G3 並列) と同パターン。G5 は 3 connector が main に揃った後の closeout。

---

## 4. 各 Sub-issue の Definition of Done（骨子）

> 着手前に各項目を具体化。ここでは代表項目のみ。

### G1 — ADRs + phase-14-plan

- [ ] ADR-0010 改訂 (Phase 14 改訂 (i)-(m): Gmail + Calendar + delta-cursor 型一般 generalize + Outlook 流本文抽出 + Gmail unit/Calendar unit + scope 拡張 + shared auth foundation) + decisions-log.md entry
- [ ] ADR-0014 改訂 (google_workspace slot scope 拡張 + shared auth foundation 抽出方針) + decisions-log.md entry
- [ ] `docs/phase-14-plan.md` が SSOT として確定 OQ1-9 / 残 OQ10-14 / PR 分割 / 各 Sub DoD / テスト戦略 / Alternatives を網羅
- [ ] 新規 ADR ゼロ方針が plan §2 に明記
- [ ] `google_common` 命名仮置き 旨が plan §X に明記
- [ ] §設計選択の trade-off に 3 点 (scope 固定 list / module 名と source_type prefix の不揃い / `google_common` 命名仮置き) が明示
- [ ] §Phase 15+ outlook に 画像 OCR / Drive Comments / Suggestions を Phase 13 §9 から移送する旨が記載

### G2 — shared auth foundation

- [x] `src/opshub/connectors/google_auth/auth.py` が存在し、Phase 13 既存 `connectors/google_workspace/auth.py` の logic を内包 (G2 着手時の §X.3 再評価で `google_common` 仮置きから `google_auth` に rename を採用、catch-all 化リスク回避)
- [ ] scope = `drive.readonly + gmail.readonly + calendar.readonly` の 3 scope を固定 list で要求
- [ ] `connectors/google_workspace/` 配下から新場所を import に re-wire 完了
- [ ] token lifecycle pin test (`test_get_access_token_persists_rotated_refresh_token`) が shared 側に物理移動 (3 connector 分重複なし)
- [ ] CLI `opshub connector auth set google_workspace` が全 Google scope を 1 回 paste-code flow で取得 (re-consent 1 回)
- [ ] Phase 13 既存 google_workspace round-trip が 1 byte たりとも壊れない (既存 unit / integration test 全 pass)
- [x] 命名の rename 決定が本 PR 内で確定: **`google_common` (仮置き) → `google_auth` を採用**。Phase 14 範囲では shared 化対象が auth.py のみであることが G2 着手時に再確認できたため、責務を狭く明示する `google_auth` を採用 (catch-all 化リスク回避、§X.3 trade-off 表で「狭い (auth 専用、責務明確)」評価の通り)

### G3 — Gmail connector

- [ ] `connectors/google_mail/` 5 module 構成で実装 (cursor + client + mapper + connector + settings)
- [ ] `opshub connector sync google_mail` で initial sync (`users.messages.list`) + delta sync (`users.history.list`) round-trip
- [ ] Gmail History API 7 日 TTL 失効 → full-pass fallback + WARN 動作 (ADR-0010 §Phase 14 改訂 (j) §不変条件 2)
- [ ] mapper: text/plain 優先 → text/html 生保持 (markitdown なし)、`[Labels: ...]` prepend、`[gmail body truncated: N / M chars]` tag (OQ10 確定値で)
- [ ] threadId field 保持 (replied_to link 化は Phase 15+)
- [ ] 新 source_type `gmail_message` literal が `domain/events/source.py` に登録 + recall / search / personal-brief / next-actions / reply-draft が認識
- [ ] rate limit 429 + 5xx exponential backoff retry pass
- [ ] body 上限の OQ10 確定 (Outlook と揃えるか separate override か、G3 着手時に決定)

### G4 — Google Calendar connector

- [ ] `connectors/google_calendar/` 5 module 構成で実装
- [ ] `opshub connector sync google_calendar` で initial sync (`events.list` without syncToken) + delta sync (`events.list(syncToken=...)`) round-trip
- [ ] Calendar 410 GONE 失効 → full-pass fallback + timeMin/timeMax window + WARN 動作
- [ ] mapper: summary = `start_iso - end_iso (N attendees)` フォーマット (MS365 Calendar と symmetric)、attendee email list / 議題 / 会議室は body
- [ ] master event のみ source 取り込み、recurring instance の動的展開なし
- [ ] override (recurringEventId + originalStartTime) を別 record として SourceObserved emit
- [ ] RRULE は field 保持 (instance 展開は Phase 15+ projection 層)
- [ ] 新 source_type `google_calendar` literal が `domain/events/source.py` に登録 + recall / search / personal-brief / meeting-prep が認識
- [ ] OQ11 確定 (timeMin / timeMax window のデフォルト値 = 過去 90 日 + 未来 365 日 を採用するか、G4 着手時に決定)
- [ ] OQ13 確定 (secondary calendar 含むか primary のみか、G4 着手時に決定)

### G5 — closeout

- [ ] 設計 docs (principles / architecture / repository-structure / decisions-log) 更新済み
- [ ] ユーザー docs (README ja/en / upgrading / secretary-agent / mcp-setup / SECURITY / google-workspace-setup) 更新済み
- [ ] **`docs/phase-13-plan.md` §9 forecast 整合化済み**（「Phase 14 = Gmail + Google Calendar に再評価済み」と書き戻し、Phase 13 audit R2-CROSS-06 教訓）
- [ ] e2e lifecycle test pass (`test_phase14_google_mail_calendar_lifecycle.py`、rotation シナリオ + scope 拡張後の 3 connector 並行 sync 含む)
- [ ] M6 guard / `opshub --help` ≤ 300ms 維持、暗号化平文リーク検出 CI 常駐継続
- [ ] AGENTS.md / CLAUDE.md Status 行 Phase 14 complete
- [ ] **`docs/phase-14-plan.md` Status header を `Phase 14 complete (YYYY-MM-DD)` に更新**（Phase 11 / 12 / 13 教訓継承）
- [ ] **G5 マージ後、main の CI workflow が green であることを確認**（Phase 10 #221 / Phase 11 / 12 / 13 hotfix 経験を踏まえた事前確認）

---

## 5. 設計ドキュメント更新計画（G5）

- **`docs/principles.md`**:
  - §1 Local-first — Gmail / Calendar も本文 local 保持 (ADR-0020 整合) を追記
  - §6 External Content Retention — Gmail / Calendar 本文も保持対象
  - §9 Phased Delivery — Phase 14 行追加
- **`docs/architecture.md`**:
  - §Connector Layer — `google_mail` + `google_calendar` 追加、shared auth foundation `connectors/google_auth/` の図示 (G2 採用名、Phase 13 google_workspace と 3 connector 共有)
  - §9 Phased Delivery — Phase 14 行追加
- **`docs/repository-structure.md`**: `src/opshub/connectors/google_auth/` (G2 採用名) + `google_mail/` + `google_calendar/` 追加
- **`docs/decisions-log.md`**: ADR-0010 改訂 + ADR-0014 改訂 entry (2 件、G1 で追加)
- **`docs/secretary-agent.md`**: personal-brief / meeting-prep / next-actions / reply-draft 表に Gmail / Google Calendar source_type 追加 + 全 source_type 一覧 update

---

## 6. ユーザー向けドキュメント更新計画（G5）

- **`README.md` / `README.ja.md`**:
  - Phase 14 行追記
  - 新 connector 表 (`google_mail` / `google_calendar`)
  - 依頼例に「Gmail に来てたあの件」「Google Calendar の来週の予定」「Gmail 返信案考えて」追加
  - 「OpsHub に今あるもの」表に Phase 14 行追加
- **`docs/upgrading.md`**: Phase 14 re-consent 手順 (Google scope 拡張で既存 refresh token invalidate、`opshub connector auth set google_workspace` 再実行) + 新 connector enablement (`opshub connector sync google_mail` / `opshub connector sync google_calendar`)
- **`SECURITY.md`**: Gmail / Calendar 本文 local 保持の含意 (Phase 11 / 13 同型追記)
- **`docs/google-workspace-setup.md`**: scope 拡張記載 (drive + gmail + calendar)、GCP Console での scope 追加手順、re-consent 必要性
- **`docs/secretary-agent.md`**: personal-brief / meeting-prep / next-actions / reply-draft 表 + source_type 一覧 update
- **`docs/mcp-setup.md`**: connector 一覧に `google_mail` + `google_calendar` 追加 (Phase 13 google_workspace に続く 8 → 10 connector 体制)

---

## 7. テスト計画

### 7.1 単体テスト (unit)

- **`connectors/google_auth/auth.py`** (shared 側、G2 採用名): scope 引数化が google_workspace の既存挙動を壊さない pin / scope 拡張時の token 取得 pin / rotation 書き戻し pin (shared 側で 1 本に集約、Phase 13 配置から物理移動)
- **`connectors/google_mail`**: cursor (History API 正常 / 7 日 TTL 失効 / full-pass fallback + WARN) / client (rate limit 429 retry / httpx mock) / mapper (text/plain 優先 / text/html fallback / `[Labels: ...]` prepend / body truncation + tag / threadId field 保持)
- **`connectors/google_calendar`**: cursor (sync token 正常 / 410 GONE 失効 / full-pass + timeMin/timeMax window + WARN) / client (rate limit 429 retry) / mapper (master event / override 別 record / RRULE field / attendee body 埋め込み / summary フォーマット)
- **`core/secrets` 規約**: `connector:google_workspace:refresh_token` keyring slot (3 connector 共有) + `OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN` env override の優先順位 (env > keyring) を pin (Phase 13 から無変更、3 connector 共有を pin)

### 7.2 結合テスト (integration)

- **`tests/integration/test_phase14_google_mail_sync.py`**: Gmail connector round-trip (cursor + 7 日 TTL fallback) + rate limit retry
- **`tests/integration/test_phase14_google_calendar_sync.py`**: Calendar connector round-trip (sync token + 410 GONE fallback) + rate limit retry
- **`tests/integration/test_phase14_shared_auth_scope_extension.py`**: scope 拡張後の既存 google_workspace round-trip 維持 + rotation 後の 3 connector 並行 sync
- **extras 確認**: `[connectors-google-workspace]` extras 1 つで 3 connector (Drive / Gmail / Calendar) すべて使える (Phase 14 で新 extras 追加なし)

### 7.3 e2e lifecycle テスト

- **`tests/integration/test_phase14_google_mail_calendar_lifecycle.py`**: **Gmail + Calendar データ統合シナリオ** を台本 MCP クライアントで再現:
  1. Gmail / Calendar から sample 取り込み (httpx mock + minimal Gmail message / Calendar event fixture)
  2. MCP `search` (FTS5) / `recall.search` で「●●会議」関連トピック検索 → gmail_message / google_calendar hit
  3. MCP `personal-brief` で Gmail / Calendar 由来の情報が今日のまとめに含まれる
  4. MCP `meeting-prep` で Google Calendar 由来の event metadata が活用される
  5. MCP `next-actions` で Gmail 由来の inbox item が候補に含まれる
  6. MCP `reply-draft` で Gmail message に対する返信下書きが生成される (Outlook と symmetric)
  7. **refresh token rotation シナリオ + scope 拡張後の 3 connector 並行 sync**: shared auth foundation が refresh 時に新 refresh token を返し keyring に書き戻し → 次の Gmail / Calendar / Drive 3 connector sync がすべて新 refresh token で成功する経路
  8. write-back 経路が呼べないこと (Gmail send / Calendar write API 経路非存在) を確認
- 形 A につき opshub 内に頭脳はないので **MCP クライアントが台本どおりツール呼び出し列を再現** して MCP 面と ①コアを検証 (実エージェント・実 LLM は不要)

### 7.4 持続検証 / guard

- M6 cold-start guard (`google_mail.*` / `google_calendar.*` / `google_auth.*` (G2 採用名) module-level import が `test_cli_imports.py` whitelist に無いこと、extras 未 install で import 失敗しないこと)
- `time opshub --help` ≤ 300ms 維持
- 暗号化平文リーク検出 CI 常駐継続
- gitleaks / secret scanner 対策: テストフィクスチャは `tests/_secrets.py` から import (連結ビルド規範を Phase 14 でも継続、Google Refresh Token mock も `_secrets.py` 経路)

### 7.5 Gmail / Calendar API mock 戦略

- **`httpx.MockTransport` 統一** (Phase 13 google_workspace と同パターン)
- fixture: `tests/fixtures/google_mail/*.json` (Gmail messages.get response sample = text/plain only / text/html only / multipart / 添付付き) + `tests/fixtures/google_calendar/*.json` (events.list sample = single / recurring + override / 410 GONE)
- mock 戦略は Phase 13 google_workspace fixture と同様に `tests/fixtures/google_mail/` / `tests/fixtures/google_calendar/` 配下に固定 JSON で配置

---

## 8. Open Questions (残)

> Phase 14 着手時に確定済の OQ1-9 は §1 確定済み事項参照。以下は **G3 / G4 着手時に決定** する実装詳細。

| # | 論点 | 決定時期 |
|---|---|---|
| OQ10 | Gmail body 上限 (Outlook OQ2 と同型)。`[gmail body truncated: N / M chars]` tag を mapper layer で。閾値は Outlook と揃えるか別 override (`[office.gmail] max_body_chars`) を切るか | G3 着手時、Outlook の閾値を確認の上で決定 |
| OQ11 | Calendar full-pass 時の `timeMin` / `timeMax` window。無限遡及防止 (operator が 10 年前から calendar 使ってる場合の暴発防止)。MVP は「過去 90 日 + 未来 365 日」程度を default、override 可能とするか | G4 着手時、MS365 Calendar との比較で決定 |
| OQ12 | Shared mailbox / delegated mailbox 対応。MVP は personal mailbox 単独 (operator 1 名前提、ADR-0018 同根拠)、shared mailbox は Phase 15+ extension | G3 着手時、personal mailbox 単独で確定見込み |
| OQ13 | Calendar の secondary calendar (operator が複数 calendar を持つケース)。Google Calendar API は `calendarList.list` で取れる。MVP では primary のみ取り込みか全 calendar loop か | G4 着手時、primary 単独で確定見込み |
| OQ14 | SDK 確認 = `httpx` 手書き継続 (Phase 13 google_workspace と同方針) で確定見込みだが、Gmail API の `users.messages.get(format='full')` payload parsing 量を測って G3 で実測検証 | G3 着手時、M6 cold-start guard 維持可能性で評価 |

着手中に新たな OQ が発生した場合は本節を更新。

---

## 9. Phase 15+ outlook

**Phase 15 候補** (Phase 13 §9 / Phase 14 epic §Phase 15+ outlook から移送):

- 画像 OCR (PPT 内画像 + Office 図表、tesseract / pytesseract、Phase 11 OQ7 / Phase 12 §9 / Phase 13 §9 defer 分の正規実装、Phase 13 plan §9 では Phase 14 候補としていたが Phase 14 = Gmail + Calendar に再評価したため Phase 15+ に再 defer)
- Drive Comments / Suggestions 取り込み (Phase 13 §9 defer、Google Workspace の議論履歴 = 決定経緯の context source、能動性ではなく次回 sync 時に diff として取り込む。Phase 13 plan §9 では Phase 14 候補としていたが本 Phase 14 = Gmail + Calendar に再評価したため Phase 15+ に再 defer)
- メール添付の本文抽出 (Gmail attachments.get + markitdown 経路、Outlook 添付も同時、ADR-0025 拡張)

**Phase 16+ 候補**:

- Notion コネクタ (OAuth principal + page hierarchy)
- Jira / Linear コネクタ (issue + comments)
- Confluence コネクタ (page + version)
- 能動性段階 1-4 (緊張点②、cron 委譲 / 記憶キュレーション / 通知 / filewatch / **Gmail `users.watch` / Calendar `events.watch` / Drive `files.watch` push notification 再評価**)
- 外部書き戻し (緊張点③、Reply send / Calendar event create / Drive write、明示承認必須)
- 統合・検索融合レイヤ (RRF + dreaming + bi-temporal links)
- ozzy-labs/skills 配布完成 (ADR-0004 §決定 (c) 復権 + Renovate preset 整備)
- メール・カレンダー meta 構造化 (label / attendee / response_status を SourceObserved field 化、ms365 / google 両 connector 同時に)
- Calendar instance 展開 projection 層 (master event + RRULE → instance dynamic 展開、ms365_calendar / google_calendar 両 calendar 同時に)
- Google Workspace multi-account 対応 (Phase 13 OQ11 継承、operator が個人 GCP + 業務 Workspace 併用)
- Gmail thread aggregation projection (message 単位 source を thread でまとめて recall に提示、graph layer の `replied_to` link 経由)

---

## X. 設計選択の trade-off

Phase 14 着手時に検討した 3 つの設計選択について、採用案と却下案の trade-off を明示する。実装中に「なぜこの形を採ったのか」を将来読者が辿れるようにすることが目的 (ADR レベルの凍結ではなく、Phase plan レベルでの議論履歴の保全)。

### X.1 scope 宣言形式: `auth.py` 内固定 list vs connector ごとの subset 宣言

**採用案**: `connectors/google_common/auth.py` 内の `SCOPES = ["drive.readonly", "gmail.readonly", "calendar.readonly"]` 固定 list を全 Google connector が共通で要求。OAuth consent 時は 3 scope を 1 回で取得し、connector ごとに「自分は drive.readonly しか使わない」「自分は gmail.readonly しか使わない」等の subset 宣言はしない。

**却下案**: connector ごとに subset 宣言 (`google_workspace` は drive.readonly のみ、`google_mail` は gmail.readonly のみ、`google_calendar` は calendar.readonly のみ要求、scope は connector enable 時に動的に合算して consent)。

**trade-off**:

| 観点 | 採用案 (固定 list) | 却下案 (subset 宣言) |
|---|---|---|
| operator consent 体験 | 1 回の consent で全 scope (operator は「Google 系全部 read-only に同意」と認識) | connector enable / disable のたびに scope 増減 → re-consent が頻発する可能性 |
| 実装複雑度 | 低 (`SCOPES` 定数 1 つ) | 中 (scope set の動的合算 + 既存 token の scope subset 判定 + scope 不足時の re-consent 誘導) |
| 「Gmail だけ使いたい」 use case | connector enable / disable で表現 (scope は 3 つ常時付与だが、Gmail / Calendar connector を disable すれば実利用は Drive のみ) | scope subset 宣言で表現 |
| Google OAuth incremental authorization との整合 | 不要 (1 回で全 scope) | 必要 (incremental scope upgrade の handling 実装) |
| 過剰 scope への懸念 | 全 read-only scope なので情報漏洩リスクは限定的、IT consent UX も Phase 13 で OK 確認済 | scope 最小化は理想的だが、operator UX 劣化 (re-consent 頻発) を上回るメリットなし |

**採用根拠**: opshub MVP は operator 1 名前提、Google 派 operator は Drive / Gmail / Calendar をセットで使うのが自然な操作モデル (秘書として動かすなら 3 つ全部欲しい)。subset 宣言は overkill。3 scope すべて read-only で外部書き戻し ban (§禁止事項 7) との整合で過剰 scope 懸念も限定的。

### X.2 connector module 命名 (`google_mail` / `google_calendar`) と source_type prefix (`gmail_` / `google_`) の不揃い

**採用案**: connector module 命名は `google_mail` / `google_calendar` (Google prefix 統一)、source_type は `gmail_message` / `google_calendar` (Gmail のみ `gmail_` prefix、Calendar は `google_` prefix)。

**却下案**: source_type も `google_mail_message` / `google_calendar` で完全揃え (Google prefix 統一)、または connector module も `gmail` / `google_calendar` で完全揃え (vendor brand 統一)。

**trade-off**:

| 観点 | 採用案 (不揃い) | 却下案-a (`google_mail_message` 統一) | 却下案-b (`gmail` module 統一) |
|---|---|---|---|
| Gmail のブランド名性 | Gmail = Google のメール製品のブランド名として独立で認知度が高い、`gmail_message` は operator が直感的 | `google_mail_message` は冗長、operator の自然文 query (「Gmail にあった」) と直接結びつかない | `gmail` module は Calendar と並べた時の Google prefix 統一感が崩れる |
| MS365 との対比 | `ms365_outlook` ↔ `gmail_message` (両方 vendor brand)、`ms365_calendar` ↔ `google_calendar` (両方 vendor + product) | `ms365_outlook` ↔ `google_mail_message` の対称性が崩れる | `ms365_outlook` (module: `ms365`) ↔ `gmail` の module 命名対比が崩れる |
| source_type を秘書の自然文 query が拾うか | 「Gmail にあった」query が `gmail_message` source_type を直接 match (自然文と source_type が同語) | 「Gmail にあった」query が `google_mail_message` に直接 match しない (1 hop 必要) | 同採用案 |
| module 命名の Google エコシステム内整合 | `google_mail` / `google_calendar` / `google_common` / `google_workspace` で Google prefix 統一、cross-connector ナビゲーション容易 | 同採用案 | `gmail` だけ Google prefix を外れる |

**採用根拠**: 「source_type が秘書の自然文 query に直接出やすい location」を優先。operator が「Gmail にあった」「Gmail で来た」と発話するのが自然で、`gmail_` prefix の source_type がその発話を直接受ける。一方 module 命名は repo 内 navigation の頻度が高く、Google prefix 統一による発見性を優先。不揃いの代償 (source_type ↔ module 名が 1 hop ずれる) よりも、両者をそれぞれ最適化したメリットの方が大きい。

### X.3 `google_common` 命名の仮置き → G2 で `google_auth` に rename 採用

> **G2 着手時の最終決定 (#294)**: 下記 G1 段階の検討を踏まえ、`google_common` 仮置きから **`google_auth`** に rename して採用。Phase 14 範囲で shared 化対象は auth.py のみであることが G2 着手時に再確認できたため、責務を狭く明示する `google_auth` のメリット (catch-all 化リスク回避) を取った。将来 cursor / mapper の shared 化が必要になった場合は新パッケージ (`connectors/google_<新責務>/`) を別途切る。

**採用案 (本 Phase 14 G1 段階)**: shared auth foundation を `connectors/google_common/auth.py` に置く (`google_common` 命名は仮置き、G2 着手時に rename 候補を再評価)。

**却下案 (G2 着手時に再評価する候補)**: `connectors/google_auth/` (auth 専用パッケージ) / `connectors/google_oauth/` (OAuth 専用パッケージ) / `connectors/_shared/google/` (Python convention の private prefix)。

**trade-off**:

| 観点 | `google_common` (仮置き) | `google_auth` | `google_oauth` | `_shared/google/` |
|---|---|---|---|---|
| 命名の射程 | 広い (catch-all 化リスク: `google_common/cursor.py` / `google_common/mapper.py` を後付けで増やすと「Google 系の共通置き場」になり境界が曖昧化) | 狭い (auth 専用、責務明確) | 狭い (OAuth 専用、責務明確) | 中 (Google 配下、共通の意味) |
| 後付けで cursor 共通化 / mapper 共通化が必要になった時 | 同 package に追加可能 (catch-all 化) | rename 必要 / 別 package 新設 | rename 必要 / 別 package 新設 | 同 package に追加可能 |
| Python convention | OK | OK | OK | private prefix (`_`) で外部 import を抑制する慣習、明示性は高い |
| Phase 13 既存 `google_workspace` connector との関係 | shared module を import する形 (現状方向) | 同 | 同 | 同 |
| G2 段階での Phase 14 全 sub 範囲 | shared 化対象は auth.py のみ (Phase 14 範囲では cursor / mapper は 3 connector で別実装) | 同範囲なら最適 | 同範囲なら最適 (OAuth 専用と明示) | 同範囲なら過剰命名 |

**採用根拠 (Phase 14 G1 段階)**: Phase 14 範囲では shared 化対象は auth.py のみで、cursor / mapper / settings は 3 connector で独立。`google_auth` / `google_oauth` のほうが射程が狭く catch-all 化リスクを回避できるが、G1 段階での命名確定は時期尚早 (実装が始まる G2 で「auth 以外も shared 化すべき場面」が見えるかどうかは G2 着手時に判断するのが妥当)。本 Phase 14 G1 では `google_common` を仮置きとし、**G2 着手時に rename を再評価する旨を本 plan に明示** することで判断保留を可視化する。

---

## Alternatives（却下した選択肢と理由）

### 1. Phase 14 = 画像 OCR + Drive Comments / Suggestions (Phase 13 §9 forecast 通り)

却下理由: Phase 13 §9 forecast 当時は「Google Workspace 深掘りの自然延長 = OCR + Comments」だったが、Phase 14 着手時に opshub の秘書 use case を再評価した結果、Google 派 operator にとって Gmail + Calendar 未対応が MS365 (Outlook + Calendar) 対称性の最大欠落であり、秘書として体感価値が最大と判断。OCR + Comments は Phase 15+ に再 defer (本 plan §Phase 15+ outlook)、forecast 整合化は G5 で `docs/phase-13-plan.md` §9 に書き戻し (R2-CROSS-06 教訓継承)。

### 2. Gmail / Calendar 用に新 keyring slot を追加 (`connector:google_mail:refresh_token` / `connector:google_calendar:refresh_token`)

却下理由: 1 Google account = 1 principal が opshub 秘書 MVP の前提 (ADR-0018 同根拠、operator 1 名スケール)。同一 Google account から Drive / Gmail / Calendar を取り込むのが自然な操作モデルで、3 つの keyring slot を別管理する意味がない (3 つすべてに同じ refresh token を入れることになり冗長)。scope 拡張で 1 slot 3 connector 共有が成立する非対称性は Google OAuth エコシステムの「account 単位の token + scope 拡張」設計に由来。ADR-0010 §Phase 14 改訂 (m) で明文化。

### 3. Gmail thread 単位の source_type (`gmail_thread`)

却下理由: event store immutability と摩擦する。thread = 複数 message の動的集約で、message append 毎に thread record を再書きすると event log が無限増殖する (Phase 1 で確定した event-sourced architecture と直接抵触)。message 単位 (`gmail_message`) で固定し、threadId は field 保持で表現するのが自然。thread aggregation は projection 層の責務として Phase 15+ で切る (本 plan §Phase 15+ outlook)。

### 4. Calendar instance 展開 (recurring event を connector layer で展開)

却下理由: master event = 1 record + RRULE field のほうが event-sourced と素直。recurring event を connector で展開すると同一 event の複数 instance が SourceObserved として大量 emit され、本来 derived state であるべきものが event log に固定化される。instance 展開は projection 層の責務として Phase 15+ で `ms365_calendar` / `google_calendar` 両 calendar 同時に切る (本 plan §Phase 15+ outlook + Phase 14 改訂 (l) §不変条件 3)。

### 5. Gmail HTML body に markitdown を通す

却下理由: Outlook 流の text/html 生保持と非対称になる。markitdown を通すと HTML → markdown 変換層が mapper に増え、Outlook mapper との symmetry が崩れる。HTML rendering は host LLM / skill 側の責務 (markdown 化 / plain text 化のどちらが必要かは skill ごとに異なる)。Outlook mapper が text/html を生保持しているのと同じ判断を Gmail にも適用 (mapper symmetry 維持、ADR-0010 §Phase 14 改訂 (k) §Gmail 不変条件 1)。

### 6. Gmail label / Calendar attendee を構造化 field (`labels` / `attendees`) として SourceObserved に追加

却下理由: SourceObserved domain 改変は migration を伴い、Outlook の `attendees_count` 流 (summary / body 埋め込みのみ、新 field なし) と非対称になる。Phase 14 時点では body 埋め込みで足り、構造化 filter (label filter / attendee filter) は Phase 15+ で需要顕在化時に切る (両 connector 同時に改訂、ms365 / google 揃えて、本 plan §Phase 15+ outlook)。

### 7. Teams pattern (verbatim user token + アプリ層 refresh なし) を Gmail / Calendar にも採用

却下理由: Google OAuth Refresh Token は documented rotation を行う (毎 access token 取得 / refresh で新 refresh token が返り得る)、verbatim token のみ pattern では Phase 13 plan §Alternatives #6 と同じく毎回 paste-code flow が必要で operator UX が極端に劣化。Phase 13 改訂 (h) で確立した MS365 / Box pattern (Refresh Token + 自前 refresh + rotation 書き戻し + pin test 必須) を Phase 14 でも 3 connector に共通適用するのが自然。Phase 14 改訂 (m) は Phase 13 改訂 (h) の流用 + scope 拡張 + shared auth foundation 抽出。

### 8. Gmail / Calendar 用に独立 ADR (ADR-0026 / ADR-0027 等) を起票

却下理由: Phase 11 / 12 / 13 流の単一改訂路線を踏襲 (Phase 11 = 1 新規 + 2 改訂、Phase 12 = 0 新規 + 3 改訂、Phase 13 = 0 新規 + 3 改訂、Phase 14 = 0 新規 + 2 改訂、と縮退継続)。Connector Protocol + 責務 1-6 + 禁止事項 1-7 を Gmail / Calendar にも適用する確認のみで独立 ADR は概念的二重化。Phase 14 plan §1 OQ9 で「ADR-0010 + ADR-0014 の 2 改訂」を確定。

### 9. 新 extras (`[connectors-google-mail]` / `[connectors-google-calendar]`) を追加

却下理由: shared auth foundation が `connectors/google_common/` に集約され、Gmail / Calendar も同じ httpx + Google OAuth 経路を使う。`[connectors-google-workspace]` 1 extras に Gmail / Calendar 依存を追加するほうが operator の install 体験が簡潔 (Drive / Gmail / Calendar = Google 系全部入り 1 extras)。3 extras に分割するメリット (Gmail だけ install したい / Calendar だけ install したい) は実利用シーンとマッチしない (Google 派 operator は 3 つセットで使うのが前提)。

### 10. Gmail 添付の本文抽出を Phase 14 scope に含める

却下理由: Phase 14 scope の肥大化。Gmail 添付は `users.messages.attachments.get` + markitdown 経路で取り込むことになり、ADR-0025 拡張 + 添付の dedup / size cap / fail-safe 設計が必要 (Phase 11 Office 抽出 + Phase 13 Workspace export と同程度の設計コスト)。Phase 14 = Gmail / Calendar 取り込みの基盤確立に集中し、添付は Phase 15+ で Outlook 添付も同時に切る (本 plan §Phase 15+ outlook、メール添付の本文抽出)。

---

## 関連

- principles.md §1 (Local-first) / §6 (External Content Retention) / §9 (Phased Delivery)
- architecture.md §Connector Layer / §9 (Phased Delivery)
- ADR-0010 (Connector Contract、本 phase で改訂 = §Phase 14 改訂 (i)-(m))
- ADR-0014 (SaaS Token Storage、本 phase で改訂 = §Phase 7 Validation 節 google_workspace slot scope 拡張 + shared auth foundation 抽出方針)
- ADR-0018 (Slack Connector Token Principal、operator 1 名スケール根拠)
- ADR-0020 (Full Local Content Retention、Gmail / Calendar 本文も対象)
- ADR-0021 (Encryption at Rest、Gmail / Calendar 本文も保護対象)
- ADR-0022 (MCP Server Surface、既存 read tools (`search` / `recall.search` / `find-document`) が Gmail / Calendar source_type を自動的に活用する設計)
- ADR-0025 (Office Document Content Extraction、本 phase では touch せず = Gmail 添付を retain しないため改訂不要、添付対応は Phase 15+)
- 参考実装: `src/opshub/connectors/google_auth/auth.py` (Phase 14 G2 で `google_workspace/auth.py` から物理移動済 = shared auth foundation。G1 時点では `google_common` を仮置き名としていたが、G2 着手時の §X.3 再評価で catch-all 化リスク回避のため `google_auth` を採用) / `src/opshub/connectors/ms365/mapper.py` (Outlook / Calendar mapper symmetry 参照先) / `src/opshub/connectors/google_workspace/cursor.py` (TTL fallback パターン)
- Phase 11 plan §3 Sub-issue F1 (delta-link cursor TTL fallback 起源)
- Phase 12 plan §9 outlook (Phase 13 candidate に Google Workspace を含めていた forecast)
- Phase 13 plan §9 outlook (本 Phase 14 で「Gmail + Google Calendar」に再評価、G5 で書き戻し = R2-CROSS-06 教訓)
- Phase 14 epic #292、子 sub-issue #293-#297
- Gmail API: <https://developers.google.com/gmail/api>
- Gmail History API: <https://developers.google.com/gmail/api/v1/reference/users/history/list>
- Google Calendar API: <https://developers.google.com/calendar/api>
- Google Calendar events.list with syncToken: <https://developers.google.com/calendar/api/v3/reference/events/list>
- Phase 1 #3 / Phase 2 #23 / Phase 3 #43 / Phase 4 #62 / Phase 5 #81 / Phase 6 #99 / Phase 7 #113 / Phase 8 #128 / Phase 9 #187 / Phase 10 #203 (closed) / Phase 11 #233 (closed) / Phase 12 #253 (closed) / Phase 13 #274 (closed)
