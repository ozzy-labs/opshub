# Phase 13 Implementation Plan

> Status: **Phase 13 complete (2026-05-31). Sub-issue G1-G5 すべて merged** (Phase 11 R2-CROSS-06 / Phase 12 H6 教訓継承)。Scope: **Google Workspace コネクタ** = Google Docs / Slides / Sheets を Drive API v3 + OAuth Refresh Token (offline access + 自前 refresh + rotation 書き戻し = MS365 / Box pattern) で取り込み、Workspace export → MS Office mediatype (docx / pptx / xlsx) → markitdown (Phase 11 `core/document_extract.py` 再利用) で本文 + provenance を抽出する。Teams pattern (verbatim user token + アプリ層 refresh なし) とは別系統である旨を ADR-0010 改訂で明文化。形A（runtime なし）・能動性なし (Drive `files.watch` 禁止 + `changes.list` poll のみ)・外部書き戻しなしを Phase 10/11/12 から継承。
>
> Sub-issue は **G1〜G5 の 5 つ**（親 epic #274、子 #275〜#279）。**新規 ADR ゼロ、改訂 3 本**（ADR-0010 + ADR-0014 + ADR-0025）で吸収。Phase 11 流の単一改訂路線を踏襲 (Phase 11 = 1 新規 + 2 改訂、Phase 12 = 0 新規 + 3 改訂、Phase 13 = 0 新規 + 3 改訂、と縮退継続)。本 plan が SSOT であり、各 sub-issue body は要点抜粋。
>
> 本ドキュメントは **planning skeleton** であり、各 sub-issue の詳細設計・不変条件・最終 DoD は着手前に本 plan 内で確定する。実装契約（uow_factory / `EventStore.append` / `Projector.apply` / registry SSOT / cold-start guard / `core/sanitise.sanitise_error_message` / Pluggable backend Protocol freeze / Connector framework / `tests/_secrets.py` 連結ビルド規範 等、Phase 1-12 で確立）は Phase 13 も全て継承する。

Phase 13 の目的は、Phase 11 で MS Office (Word / Excel / PowerPoint) の本文を `body + provenance` 付きで取り込めるようにしたパイプラインを **Google Workspace に拡張** することにある。具体的には Google Docs / Slides / Sheets を Drive API v3 + OAuth Refresh Token + Workspace export 経由で取り込み、recall / search (FTS5) / find-document / meeting-prep 系 skill が Google Workspace 由来の情報を一級市民として扱えるようにする。

抽出経路は Phase 11 `core/document_extract.py` を再利用 (Workspace export → MS Office mediatype → markitdown)、Google ネイティブ markdown export は使わない (Sheets / Slides が markdown 直接 export 非対応のため、3 形式統一として MS Office mediatype 経由)。Token lifecycle は MS365 / Box pattern (refresh token + offline access + 自前 refresh + rotation 書き戻し)、Teams pattern (verbatim user token) とは別系統である旨を ADR-0010 改訂で明文化する。

---

## 1. 確定済み事項

### Open Question 決定（2026-05-31 設計セッション）

| # | 論点 | 決定 |
|---|---|---|
| OQ1 | API 経路 | **Google Drive API v3 + OAuth 2.0 User Token (Refresh Token + offline access)**。Drive for Desktop の WSL2 mount は不安定、API 経路で統一 |
| OQ2 | 抽出経路 | **Workspace export → MS Office mediatype (docx / pptx / xlsx) → markitdown**（Phase 11 `core/document_extract.py` 再利用） |
| OQ3 | source_type | **新規 3 種：`google_doc` / `google_slides` / `google_sheets`**。find-document の自然文 query を直接支える。`Final[Literal[...]]` で G2 が公開し G3 が import |
| OQ4 | ADR 構成 | **新規 ADR ゼロ、改訂 3 本（ADR-0010 + ADR-0014 + ADR-0025）**。Phase 11 流の単一改訂路線を踏襲。Google Refresh Token は ADR-0014 (既存 SaaS token storage) §Phase 7 Validation 節の rotation pin リストに `google_workspace` を追加し、ADR-0010 改訂内で「Google = MS365 / Box pattern、Teams pattern とは別系統」を明文化 |
| OQ5 | cursor 戦略 | **Drive API `changes.list` page token + TTL 失効時 full-pass fallback**。ADR-0010 改訂で契約化 (Teams delta-link と cursor 戦略は同パターン、ただし token lifecycle は MS365 pattern) |
| OQ6 | scope | **`drive.readonly` 単独**。`drive.metadata.readonly` は `drive.readonly` の subset なので併記しない (operator IT consent UX の改善 + 過剰 scope フラグ回避)。`drive.activity.readonly` も `changes.list` poll のみなら不要 |
| OQ7 | trashed / Shared with me / removed | trashed=true は archived 相当として保持 (ADR-0020 全保持整合)。`changes.list` の `removed=true` (永続削除) も retain。Shared with me も含む (秘書としての実用性)。詳細 semantics は本 plan §trashed / removed semantics で domain 確認の上確定 |

### Phase 10/11/12 から継承する不変方針

1. **形A**: opshub は MCP server + Agent Skills のみ提供、頭脳 (runtime) は外部ホスト
2. **能動性なし**: リクエスト駆動のみ、常駐・定期実行は Phase 14+。**Drive push notification (`files.watch`) は禁止**、`changes.list` poll のみ
3. **外部書き戻しなし**: 取り込み + ローカル context 生成のみ。Drive write API (`files.update` / `files.create` / `files.copy` / `comments.create` / `permissions.*`) を connector に実装しない (ADR-0010 §禁止事項 7 の Google Workspace への自然延長)
4. **本文ローカル保持** + `provenance_origin="external"` / `provenance_trust="untrusted"` 付き (ADR-0020)
5. **SQLCipher 丸ごと暗号化 opt-in** (ADR-0021、keyring 経由)
6. **HITL boundary**: read tools は host LLM 自律 OK、write tools は host LLM が user 確認必須 (ADR-0022 annotation policy)
7. **`_secrets.py` 連結ビルド規範** に Google Refresh Token も追加
8. **M6 cold-start guard / `opshub --help` ≤ 300ms / plaintext-leak 検出 CI 常駐**

### Phase 番号

**Phase 13**（top-level）。新 connector (`google_workspace`) + 新 source_type 3 種 + ADR 改訂 3 本を伴う「新 connector category 1 vendor 追加」が主目的。Phase 11 流の単一コネクタ集中パターン (MS Office 深掘り) に揃え、Phase 12 plan §9 で forecast していた「データ拡張系一括 (OCR / Google Workspace / Notion / Jira / Linear / Confluence)」を **Google Workspace 単独** に再評価した (理由は親 epic #274 §Phase 12 plan §9 からの変更点に記載、G5 で `docs/phase-12-plan.md` §9 forecast を「Phase 13 = Google Workspace 単独に再評価済み」と書き戻す = R2-CROSS-06 同型ミス防止)。メモリ方針 [[phase-numbering-new-arch-pattern]] に整合 (新 connector category + 新 projection 行 = Phase X+1 で起票)。

---

## 2. 改訂 ADR

> **新規 ADR ゼロ**。改訂 3 本のみ。Phase 12 plan §9 で forecast していた独立 ADR-0026 (Google Workspace connector) / ADR-0027 (Workspace export 経路) は **立てない**。Phase 11 流の単一改訂路線を踏襲し、既存 ADR の延伸条文として吸収する。**新規 ADR ゼロ方針** = (i) connector contract は ADR-0010 で完結 (Teams / Office 抽出も既存 ADR への加算改訂で対応した先例)、(ii) token storage は ADR-0014 §Validation 節への rotation pin リスト追加で完結、(iii) source_type 拡張は ADR-0025 §決定 (d') / §決定 (j) として吸収可能、の 3 点による。

| ADR | 種別 | タイトル | 主な改訂内容 |
|---|---|---|---|
| **ADR-0010** | 改訂 | Connector Contract | **Phase 10 改訂 (write-back ban、§禁止事項 7) と Phase 11 改訂 (a)-(d) は保持** したまま、以下 4 点を加算追加：(e) Google Workspace 新コネクタを契約対象に追加 + Drive `files.watch` 禁止 / (f) Workspace export 経路の本文抽出契約 (markitdown 1 本経路を保持、3 形式とも MS Office mediatype 経由で統一) / (g) Drive `changes.list` cursor + TTL 失効時 full-pass fallback 義務 (Teams delta-link と同パターン) / (h) Google Workspace User Token principal = MS365 / Box pattern (refresh token + offline access + 自前 refresh + rotation 書き戻し)、Teams pattern (verbatim user token + アプリ層 refresh なし) とは別系統である旨を明文化 |
| **ADR-0014** | 改訂 | SaaS Token Storage | §Phase 7 Validation 節の rotation pin リストに `connector:google_workspace:refresh_token` を追加 (MS365 / Box に続く 3 件目)、env var override `OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN` も pin。Phase 7 keyring keys 一覧にも Phase 11 Teams (5 件目) + Phase 13 Google Workspace (6 件目) を追記 |
| **ADR-0025** | 改訂 | Office Document Content Extraction | §決定 (d) を保持したまま、(d') Google Workspace 由来 source_type 3 種 `google_doc` / `google_slides` / `google_sheets` を加算追加 (Google mimeType → source_type マッピング表)。(j) Workspace export 経路 (Drive API `files.export` → MS Office mediatype → markitdown) を §決定として追加。`core/document_extract.extract` の API 表面を `path_or_bytes` 形式で local file path / in-memory bytes 両方受ける形に拡張 (Phase 11 は path-only) |

---

## 3. Commit 順序（Sub-issue 骨子）

> 各 sub-issue を 1〜複数 PR に割る。詳細 PR 分割と DoD は着手前に確定。依存順に並べる。

### Sub-issue G1: ADRs (ADR-0010 + ADR-0014 + ADR-0025 改訂) + phase-13-plan (#275)

- 改訂 3 本 (ADR-0010 / ADR-0014 / ADR-0025) を 1 PR で Accepted
- `docs/phase-13-plan.md` 新規 (本ファイル) を main 追加
- `docs/decisions-log.md` entries (3 件)

**PR G1** `docs(adr,plan): adr-0010 + 0014 + 0025 改訂 + phase-13-plan (Google Workspace)`

### Sub-issue G2: 抽出 foundation (export 経路 + 新 source_type literal 公開) (#276)

- **依存: G1 のみ**
- `src/opshub/core/document_extract.py` に Workspace export 経路を追加 (Google mimeType → MS Office mediatype マッピング、`extract(path_or_bytes, source_hint=...)` API 拡張、ADR-0025 §決定 (j) と整合)
- 新 source_type を `domain/events/source.py` の `Final[Literal[...]]` に登録 (`google_doc` / `google_slides` / `google_sheets`、G3 が import)
- export 後の Office mediatype (`.docx` / `.pptx` / `.xlsx`) を markitdown に渡す経路を実装 (Google mimeType → export target mediatype マッピングは G3 connector の責務、本 PR では Office mediatype を受ける expand 経路のみ)
- size cap / fail-safe / cells cap は Phase 11 を継承 (ADR-0025 §決定 (b)(c)(e))
- unit tests (Workspace export 経路 / 各 mediatype 抽出 / fail-safe / cap)

**PR G2** `feat(core,domain): workspace export 経路 + 新 source_type literal 公開`

### Sub-issue G3: OAuth + Drive API metadata (#277)

- **依存: G1 のみ** (G2 と並列可能、metadata mapper は source_type literal の存在を前提とするが、import order で吸収可)
- `connectors/google_workspace/` 新設 (`auth.py` + `client.py` + `mapper.py` + `connector.py` + `settings.py` の 5 module 構成、box_drive / onedrive_drive 先例を踏襲)
- Google OAuth 2.0 paste-code flow を `src/opshub/cli/connector.py` の `connector auth set google_workspace` に追加 (MS365 / Box と同型)
- `GOOGLE_WORKSPACE_REFRESH_TOKEN_SECRET_KEY` 定数を `connectors/google_workspace/auth.py` に定義、ADR-0014 規約 `connector:google_workspace:refresh_token` keyring slot + `OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN` env override に従う
- Drive API v3 `changes.list` + `files.list` (initial sync) + page token cursor 管理 + TTL 失効時 full-pass fallback (ADR-0010 §Phase 13 改訂 (g))
- rate limit 429 + 5xx exponential backoff retry (Phase 7 既存 connector と同パターン)
- metadata mapper (Drive API file metadata → `SourceObserved` with `source_type` + `external_id` + `summary` + provenance)
- Google mimeType → source_type lookup (ADR-0025 §決定 (d') 表に従う)
- rotation pin test (`test_get_access_token_persists_rotated_refresh_token`、ADR-0014 §Phase 7 Validation MS365 / Box の同型 test)
- **本 PR では `content_extraction` 設定値の wiring は scope 外** (G4 で実装)、本 PR は metadata + provenance のみで `body=None` を返す経路を pin
- **SDK 選定 (OQ8)** は本 PR 着手時に決定。`google-api-python-client` (discovery 重い、M6 cold-start guard との両立検証要) vs `httpx` + 手書き OAuth + manual JSON (Teams / MS365 方式と整合) の二択

**PR G3** `feat(connectors/google_workspace): oauth + drive api metadata + cursor + fallback`

### Sub-issue G4: Workspace export 統合 (body + provenance + content_extraction wiring) (#278)

- **依存: G2 + G3** (`core/document_extract.py` 拡張 + connector 基盤の両方が必要)
- mapper に `core/document_extract.extract` 呼び出しを統合 (Drive API `files.export` でバイナリ取得 → `extract(bytes, source_hint=<mimeType>)` → body + provenance)
- `connectors/google_workspace/settings.py` に `content_extraction = false` (default) opt-in 設定 (box_drive / onedrive_drive 先例踏襲、ADR-0010 §Phase 13 改訂 (f) §不変条件 5)
- connector.py で `content_extraction` 値を読み取り mapper に propagate (mapper.py 単独に閉じない、box_drive / onedrive_drive 先例踏襲)
- `[connectors.google_workspace] content_extraction = true` opt-in 時のみ files.export 経路が起動
- `files.export` 失敗 fail-safe (Drive API throttling / file permission lost / 巨大 file export timeout → `body=None` + warning log)
- tests: export 統合 (Drive API mock 経路) / content_extraction false default 挙動 / 巨大 file fail-safe / provenance 確認

**PR G4** `feat(connectors/google_workspace): workspace export + body + provenance integration`

### Sub-issue G5: Phase 13 closeout (#279)

- **依存: G2 + G3 + G4 全てマージ済み**
- 設計 docs 一括 (§5)
- ユーザー docs 一括 (§6)
- e2e lifecycle test (§7.3)
- guard 確認 (§7.4)
- AGENTS.md / CLAUDE.md Status 行 Phase 13 complete
- `docs/phase-13-plan.md` Status header を `Phase 13 complete (YYYY-MM-DD)` に更新 (Phase 11 R2-CROSS-06 / Phase 12 Status header pattern 継承)
- `docs/phase-12-plan.md` §9 forecast を「Phase 13 = Google Workspace 単独に再評価済み」と書き戻す (R2-CROSS-06 同型ミス防止、親 epic #274 §Phase 12 plan §9 からの変更点に記載済)
- G5 マージ後、main の CI workflow が green であることを確認 (Phase 10 #221 / Phase 11 / Phase 12 hotfix 経験を踏まえた事前確認)

**PR G5** `docs: phase 13 closeout + e2e`

### Wave 配置（依存 DAG）

```text
Wave 1: G1                ← entry
Wave 2: G2 / G3（2 並列） ← G1
Wave 3: G4                ← G2 + G3
Wave 4: G5                ← G2 + G3 + G4
```

drive 例: `/drive --merge #275 -> #276,#277 -> #278 -> #279`（Wave 2 で G2/G3 並列、Wave 3 で G4 統合、Wave 4 で G5 closeout）

**並列性の根拠**: G2 (foundation = `core/document_extract.py` 拡張 + literal 公開) と G3 (connector OAuth + metadata) は独立。G3 mapper は G2 で公開される source_type literal を import するが、G2 の literal 追加と G3 の import は merge 順に依存しないため並列可能 (Python の forward reference + 開発中の文字列 literal で先行可能、merge 時点で literal が main に揃う)。Phase 11 Wave 2 の F2/F3/F5 3 並列と同パターン。G4 のみ document_extract 拡張 (G2) + connector 基盤 (G3) の両方が必須なため Wave 3。

---

## 4. 各 Sub-issue の Definition of Done（骨子）

> 着手前に各項目を具体化。ここでは代表項目のみ。

### G1 — ADRs + phase-13-plan

- [ ] ADR-0010 改訂 (Google Workspace + changes.list + TTL fallback + Refresh Token principal を MS365 / Box pattern として明文化、Teams pattern とは別系統) + decisions-log.md entry
- [ ] ADR-0014 改訂 (§Phase 7 Validation rotation pin リストに google_workspace 追加) + decisions-log.md entry
- [ ] ADR-0025 改訂 (新 source_type 3 種 + export 経路) + decisions-log.md entry
- [ ] `docs/phase-13-plan.md` が SSOT として確定 OQ1-7 / 残 OQ8-11 / PR 分割 / 各 Sub DoD / テスト戦略 / Alternatives を網羅
- [ ] 新規 ADR ゼロ方針が plan §2 に明記
- [ ] Google Workspace の token lifecycle 経路 (MS365 pattern 踏襲) と Teams pattern (verbatim token) の区別が ADR-0010 / plan で明文化
- [ ] **Drive push notification (`files.watch`) 禁止**が ADR-0010 / Epic「やってはいけないこと」と整合 (`changes.list` poll のみ)

### G2 — 抽出 foundation

- [ ] `core/document_extract.extract` の API 表面が `path_or_bytes` + `source_hint` 形式に拡張 (Phase 11 path-only との backward-compat 維持)
- [ ] Workspace export 経路の Google mimeType → MS Office mediatype マッピングが実装
- [ ] 新 source_type 3 種 (`google_doc` / `google_slides` / `google_sheets`) が `domain/events/source.py` の `Final[Literal[...]]` に登録
- [ ] 既存 office source_type (Phase 11 `word_document` / `excel_spreadsheet` / `powerpoint_slide_deck`) の挙動が 1 byte たりとも変わらない
- [ ] unit tests pass (各 mediatype 抽出 / Workspace export 経路 / fail-safe / size cap)
- [ ] mypy strict pass

### G3 — OAuth + Drive API metadata

- [ ] `connectors/google_workspace/` 5 module 構成で実装 (auth + client + mapper + connector + settings)
- [ ] `opshub connector auth set google_workspace` で paste-code flow による Refresh Token 取得 + keyring 保存
- [ ] `connector:google_workspace:refresh_token` keyring slot + `OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN` env override が動作 (ADR-0014 規約と整合)
- [ ] Drive API v3 `changes.list` + `files.list` (initial sync) + page token cursor 管理 + TTL 失効時 full-pass fallback (ADR-0010 §Phase 13 改訂 (g))
- [ ] **Refresh Token rotation 書き戻し pin test pass** (`test_get_access_token_persists_rotated_refresh_token`、MS365 / Box 同型)
- [ ] metadata mapper が `SourceObserved` を Application Service 経由で append (`body=None`、G4 で body 追加予定)
- [ ] Google mimeType → source_type lookup が ADR-0025 §決定 (d') 表と整合
- [ ] rate limit 429 + 5xx exponential backoff retry (Phase 7 既存 connector と同パターン)
- [ ] `[connectors-google-workspace]` extras 定義 + CI workflow `--extra connectors-google-workspace` 追記
- [ ] OQ8 SDK 選定が本 PR 着手時に決定済

### G4 — Workspace export 統合

- [ ] mapper が `core/document_extract.extract` 経由で Drive API `files.export` バイナリを抽出 → body + provenance (`provenance_origin="external"` / `provenance_trust="untrusted"`)
- [ ] `[connectors.google_workspace] content_extraction = false` (default) で従来挙動 (`body=None`、G3 完了時挙動)
- [ ] `content_extraction = true` opt-in 時のみ export 経路が起動
- [ ] `files.export` 失敗 fail-safe (Drive API throttling / file permission lost / 巨大 file export timeout → `body=None` + warning log で SourceObserved 発行継続)
- [ ] connector.py / settings.py の content_extraction 値読み取り + propagate (mapper.py 単独に閉じない、box_drive / onedrive_drive 先例踏襲)
- [ ] tests: export 統合 (Drive API mock) / content_extraction false default / 巨大 file fail-safe / provenance 確認

### G5 — closeout

- [ ] 設計 docs (principles / architecture / repository-structure / decisions-log) 更新済み
- [ ] ユーザー docs (README ja/en / upgrading / secretary-agent / mcp-setup / SECURITY / 新規 google-workspace-setup) 更新済み
- [ ] **`docs/phase-12-plan.md` §9 forecast 整合化済み**（「Phase 13 = Google Workspace 単独に再評価済み」と書き戻し、R2-CROSS-06 教訓）
- [ ] e2e lifecycle test pass (`test_phase13_google_workspace_lifecycle.py`、rotation シナリオ含む)
- [ ] M6 guard / `opshub --help` ≤ 300ms 維持、暗号化平文リーク検出 CI 常駐継続
- [ ] AGENTS.md / CLAUDE.md Status 行 Phase 13 complete
- [ ] **`docs/phase-13-plan.md` Status header を `Phase 13 complete (YYYY-MM-DD)` に更新**（Phase 11 R2-CROSS-06 / Phase 12 教訓継承）
- [ ] **G5 マージ後、main の CI workflow が green であることを確認**（Phase 10 #221 hotfix 経験を踏まえた事前確認）

---

## 5. 設計ドキュメント更新計画（G5）

- **`docs/principles.md`**:
  - §1 Local-first — Google Workspace は Web API 経由だが本文 local 保持 (ADR-0020 整合) を追記
  - §6 External Content Retention — Google Workspace 本文も保持対象 (ADR-0025 改訂 (d') 反映)
  - §9 Phased Delivery — Phase 13 行追加
- **`docs/architecture.md`**:
  - §Connector Layer — `google_workspace` 追加、Drive API v3 経路 + token lifecycle が MS365 / Box pattern と並列であることを明示
  - §Office Document Extraction Layer — Workspace export 経路を追加 (Phase 11 で導入した markitdown 1 本経路の延伸)
  - §9 Phased Delivery — Phase 13 行追加
- **`docs/repository-structure.md`**: `src/opshub/connectors/google_workspace/` 5 module 構成を追加
- **`docs/decisions-log.md`**: ADR-0010 改訂 + ADR-0014 改訂 + ADR-0025 改訂 entry (3 件、G1 で追加)
- **`docs/secretary-agent.md`**: find-document 表 + source_type 一覧 update (Phase 11 office 3 種 + Phase 13 Google Workspace 3 種 = 計 6 種を秘書 context として利用可能な旨)

---

## 6. ユーザー向けドキュメント更新計画（G5）

- **`README.md` / `README.ja.md`**:
  - Phase 13 行追記
  - 新 extras (`connectors-google-workspace`) を extras 表に追加
  - 新 connector 表 (`google_workspace`)
  - 依頼例に「Google Docs にあった仕様書探して」「あの Sheets」「Google Slides で説明したやつ」追加
  - 「OpsHub に今あるもの」表に Phase 13 行追加
- **`docs/upgrading.md`**: Google Workspace OAuth setup 手順 (`[connectors-google-workspace]` extras + paste-code flow + `content_extraction = true` opt-in)
- **`SECURITY.md`**: Google Workspace 本文 local 保持の含意 (Phase 11 同型追記)
- **新規 `docs/google-workspace-setup.md`**: **MS365 paste-code flow と同型** で書く：
  - GCP project 作成
  - OAuth consent screen 設定 (公開状態の選択、test users 設定、テストモード vs 公開モード)
  - Desktop App credential 作成 (Client ID + Client Secret)
  - paste-code flow (`opshub connector auth set google_workspace` 経由、redirect URL = OOB or localhost)
  - 失効時の再認証手順 (`OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN` env override の使い方含む)
  - 推奨 scope (`drive.readonly` 単独、IT consent UX の説明)
- **`docs/secretary-agent.md`**: find-document 表 + source_type 一覧 update
- **`docs/mcp-setup.md`**: connector 一覧に `google_workspace` 追加

---

## 7. テスト計画

### 7.1 単体テスト (unit)

- **`core/document_extract`** 拡張部分: Workspace export 経路 (Google mimeType → MS Office mediatype マッピング) / bytes 入力経路 / source_hint 引数 / fail-safe (export 失敗時の `body=None`) / size cap (Phase 11 Cap を Workspace export 由来文書にも適用、OQ9 実測で必要なら separate override)
- **`connectors/google_workspace`**: auth (access token refresh / **refresh token rotation 書き戻し pin = MS365 pattern**) / client (Drive API v3 mock) / cursor (page token 永続化 + TTL fallback) / mapper (metadata + body) / rate limit 429 + 5xx exponential backoff retry / Google mimeType → source_type lookup
- **`core/secrets` 規約**: `connector:google_workspace:refresh_token` keyring slot + `OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN` env override の優先順位 (env > keyring) を pin (Phase 3 GitHub / Phase 7 MS365 / Box と同パターン)

### 7.2 結合テスト (integration)

- **`tests/integration/test_phase13_google_workspace_sync.py`**: connector round-trip (cursor + TTL fallback) + content_extraction false default 挙動 + content_extraction true opt-in 挙動
- **`tests/integration/test_phase13_google_workspace_extract.py`**: 実 docx / xlsx / pptx fixture (`tests/fixtures/google_workspace/*.docx` / `*.xlsx` / `*.pptx`、minimal 生成) を Drive API mock 経由で `files.export` させ markitdown 抽出 → body persist → recall hit
- **`tests/integration/test_phase13_token_rotation.py`** (or unit test 相当): refresh token rotation 後の cursor 続行 (新 refresh token で次の `changes.list` が成功する経路)
- **extras 有無の挙動**: `[connectors-google-workspace]` 未インストールで `content_extraction = true` を設定した場合 `ConfigError` で fail-fast (ADR-0025 §軽減策 2 と同パターン)

### 7.3 e2e lifecycle テスト

- **`tests/integration/test_phase13_google_workspace_lifecycle.py`**: **Google Workspace データ統合シナリオ** を台本 MCP クライアントで再現:
  1. Google Workspace から sample 取り込み (Drive API mock + minimal docx/xlsx/pptx fixture、markitdown 経由抽出)
  2. MCP `search` (FTS5) / `recall.search` で「●●仕様書」関連トピック検索 → google_doc / google_sheets / google_slides hit
  3. MCP `find-document` で自然文 query → 既存 office source_type (Phase 11) と Google Workspace source_type (Phase 13) が混在して filter 可能
  4. MCP `recall.search` で source_type filter を Phase 11 office 3 種 / Phase 13 Google Workspace 3 種で切替動作確認
  5. **refresh token rotation シナリオ**: Drive API mock が refresh 時に新 refresh token を返し、keyring に書き戻し → 次の sync が新 refresh token で成功する経路
  6. write-back 経路が呼べないこと (Drive write API 経路非存在) を確認
- 形A につき opshub 内に頭脳はないので **MCP クライアントが台本どおりツール呼び出し列を再現** して MCP 面と ①コアを検証 (実エージェント・実 LLM は不要)

### 7.4 持続検証 / guard

- M6 cold-start guard (`google.*` / `google_auth.*` / `googleapiclient.*` module-level import が `test_cli_imports.py` whitelist に無いこと、extras 未 install で import 失敗しないこと)
- `time opshub --help` ≤ 300ms 維持
- 暗号化平文リーク検出 CI 常駐継続
- gitleaks / secret scanner 対策: テストフィクスチャは `tests/_secrets.py` から import (連結ビルド規範を Phase 13 でも継続、Google Refresh Token mock も `_secrets.py` 経路)

### 7.5 Drive API mock 戦略

- **`httpx.MockTransport` 統一** (G3 で SDK 選定が `google-api-python-client` でも httpx 経由に薄ラップ、OQ8 で確定)
- fixture: `tests/fixtures/google_workspace/*.docx` / `*.xlsx` / `*.pptx` を minimal 生成 (G5 で配置、Phase 11 Office fixture と同パターン)
- Drive API mock response は `tests/_secrets.py` 経路と同様に `tests/fixtures/google_workspace/drive_api_responses/` 配下に固定 JSON で配置

---

## 8. Open Questions (残)

> Phase 13 着手時に確定済の OQ1-7 は §1 確定済み事項参照。以下は **G3 着手時に決定** する実装詳細。

| # | 論点 | 決定時期 |
|---|---|---|
| OQ8 | SDK 選定 = `google-api-python-client` (discovery 重い) vs `httpx` + 手書き OAuth (Teams / MS365 方式) | G3 着手時、M6 cold-start guard 維持可能性で評価 |
| OQ9 | ADR-0025 cap (50MB/500K chars) の Workspace export 適合性 | ✅ closed: G2/G4 実測の結果、共通 cap (50MB / 500K chars) で十分につき `[office.google_workspace] max_file_size_mb` separate override は **未導入**。代表 Sheets→xlsx / Docs→docx / Slides→pptx sample の export size 分布はいずれも共通 cap 内に収まり、`[office]` 単一 knob が Box Drive / OneDrive / Google Workspace の 3 経路を lockstep で governing する Phase 11 audit Cluster B two-key composition (ADR-0025 §決定 (g)) を Workspace export 経路にもそのまま適用 |
| OQ10 | Shared Drives (Team Drives) を含むか | G3 着手時、含む場合は `supportsAllDrives=true + includeItemsFromAllDrives=true` パラメータ pin |
| OQ11 | 複数 Google アカウント対応 (multi-principal) | Phase 13 MVP は single-slot に倒す (Phase 11 Teams 同様、operator 1 名前提)、Phase 14+ で multi-account extension |

着手中に新たな OQ が発生した場合は本節を更新。

### 着手中に追加で詰める実装詳細（forecast）

- markitdown の bytes 入力対応の挙動確認 (path-only vs bytes/BytesIO の対応状況、必要なら tempfile 経路で wrap)
- Drive API `files.export` の TTL / timeout の実値 (巨大 Sheets で 60s timeout 等の境界を実装時に確認)
- Drive API rate limit (user-level + project-level quota) の実値と 429 backoff 戦略の最適化

---

## 9. Phase 14+ outlook

> **Phase 14 再評価メモ (2026-05-31)**: 本節は Phase 13 完了時点の forecast。Phase 14 着手時 (2026-05-31、epic #292) に **「Phase 14 = Gmail + Google Calendar コネクタ」** に再評価し、Phase 13 plan §9 で Phase 14 候補としていた「画像 OCR」「Drive Comments / Suggestions 取り込み」は **Phase 15+ へ移送** した。理由 = (a) opshub の秘書 use case で MS365 (Outlook + Calendar) 対称性の最大欠落が Gmail + Calendar 未対応であり、operator が Google 派なら秘書として体感価値が最大、(b) Phase 13 で Google OAuth refresh token rotation + paste-code flow + httpx 経路が確立しているため auth.py は scope 拡張のみで再利用可能 (1 回 re-consent)、(c) Outlook の mapper / cursor / source_type が前例として存在し symmetric に実装することで mapper / skill 側 logic を分岐させずに済む、(d) 画像 OCR は ADR-0025 拡張 + tesseract システム依存が必要で性質が異なる、Drive Comments も別データモデルで分割が綺麗、(e) Phase 13 / Phase 11 流の単一カテゴリ集中パターンに揃う。Phase 14 完了時点 (2026-05-31、PR #298 / #300 / #301 / #303 / G5 closeout PR) で本節を書き戻し済 — Phase 13 audit R2-CROSS-06 (forecast 取り残し) 同型ミス防止のため、再評価時の docs 書き戻しを規律化した。

**Phase 14 (完了、2026-05-31)**: Gmail + Google Calendar コネクタ (epic #292、上記再評価で確定)。新規 `connectors/google_mail/` + `connectors/google_calendar/` + shared `connectors/google_auth/` foundation。OAuth scope を `drive.readonly` から `drive.readonly + gmail.readonly + calendar.readonly` の 3-scope 固定 list に拡張、1 Google account = 1 principal を Drive + Gmail + Calendar の 3 connector で共有 (1 回 re-consent)。Gmail = message 単位 (Outlook と symmetric)、Calendar = master event only + override 別 record (MS365 Calendar と symmetric)、本文抽出は Outlook 流継承 (text/plain 優先 → text/html 生保持、markitdown なし、添付 retain なし)。ADR 改訂 2 本 (ADR-0010 §Phase 14 改訂 (i)-(m) + ADR-0014)、新規 ADR ゼロ。詳細は [`docs/phase-14-plan.md`](phase-14-plan.md)。

**Phase 15+ 候補** (Phase 13 → 14 から繰り越し分を含む):

- **画像 OCR** (PPT 内画像 + Office 図表、tesseract / pytesseract、Phase 11 OQ7 / Phase 12 §9 / **Phase 13 → 14 から再 defer** 分の正規実装)
- **Drive Comments / Suggestions 取り込み** (Google Workspace の議論履歴 = 決定経緯の context source、能動性ではなく次回 sync 時に diff として取り込む、**Phase 13 → 14 から再 defer**)
- **Gmail / Calendar 添付の本文抽出** (Gmail attachments.get + Calendar 添付 → markitdown、ADR-0025 拡張、Phase 14 から新規 defer)
- **Calendar instance 展開 projection** (master event + RRULE → instance dynamic 展開、ms365_calendar / google_calendar 両 connector 同時に projection 層として切る、Phase 14 から新規 defer)
- **Gmail thread aggregation projection** (message 単位 source を thread でまとめて recall に提示、graph layer の `replied_to` link 経由、Phase 14 から新規 defer)
- **メール・カレンダー meta 構造化** (label / attendee / response_status を SourceObserved field 化、ms365 / google 両 connector 同時に、Phase 14 から新規 defer)
- Notion コネクタ (OAuth principal + page hierarchy)
- Jira / Linear コネクタ (issue + comments)
- Confluence コネクタ (page + version)
- 能動性段階 1-4 (緊張点②、cron 委譲 / 記憶キュレーション / 通知 / filewatch / **Drive `files.watch` + Gmail `users.watch` + Calendar `events.watch` push notification 再評価**)
- 外部書き戻し (緊張点③、Drive write / Reply send / **Gmail send / Calendar event create**、明示承認必須)
- 統合・検索融合レイヤ (RRF + dreaming + bi-temporal links)
- ozzy-labs/skills 配布完成 (ADR-0004 §決定 (c) 復権 + Renovate preset 整備)
- Google Workspace multi-account 対応 (OQ11、operator が個人 GCP + 業務 Workspace 併用するケース)

---

## trashed / removed semantics

Google Workspace の trashed / removed / Shared with me の取り込み方針:

- **trashed=true** — Drive API は trashed file を `changes.list` / `files.list` で `trashed=true` flag 付きで返す。**archived 相当として保持** (ADR-0020 全保持原則と整合)。`SourceObserved.summary` に `[trashed]` 注記を含めるかは G3 mapper 実装時に確定 (Phase 11 onedrive_drive で trashed 概念がなかったため domain layer に既存の `archived` / `trashed` 概念があるかを G1 で確認 → G3 で再利用 or 新 column 追加 or existing flag re-use で確定)
- **`changes.list` の `removed=true`** — Google 側で **永続削除** された file。ADR-0020 全保持原則に従い **retain** (永続削除しない)。`SourceObserved.summary` に `[removed from Google Workspace]` 注記を含めるかは G3 mapper 実装時に確定
- **Shared with me** — operator が所有していないが共有された file。**含む** (秘書としての実用性 = 業務で共有された Docs / Sheets / Slides を context として扱える)。Drive API `q="sharedWithMe=true"` query で取得、`files.list` / `changes.list` の通常結果に混在
- G3 DoD で test 可能な形に整理 (trashed file の取り込み確認 / removed file の retain 確認 / Shared with me file の取り込み確認の 3 test pin)

---

## Alternatives（却下した選択肢と理由）

### 1. scope `drive.readonly` + `drive.metadata.readonly` 併記

却下理由: `drive.metadata.readonly` は `drive.readonly` の subset。併記すると consent screen 冗長 + 社内 IT consent 審査で「過剰 scope」フラグのリスク。`drive.readonly` 単独に確定 (OQ6)。

### 2. `drive.activity.readonly`

却下理由: `changes.list` poll のみで delta 検出可能、activity feed 不要。activity feed を使うと「誰がどう編集したか」の context が増えるが、Phase 13 scope (Workspace 文書本文取り込み) には過剰。Phase 14+ で activity feed が必要になった場合は別 scope として再評価。

### 3. Drive `files.watch` (push notification)

却下理由: 能動性混入。形A (能動性なし) の Phase 13 scope に抵触する。`changes.list` poll のみに制限することで「リクエスト駆動のみ」を維持 (親 epic #274 §やってはいけないこと第 3 項 + ADR-0010 §Phase 13 改訂 (e) §禁止事項拡張)。Phase 14+ 能動性段階で再評価。

### 4. Docs `text/markdown` 直接 export

却下理由: Sheets / Slides は markdown 直接 export 非対応。3 source_type の export 経路を統一して `core/document_extract.py` の API 表面 1 経路を保つために **3 形式とも MS Office mediatype 経由 (docx / pptx / xlsx)** に統一 (ADR-0025 §決定 (j) §不変条件 2)。Docs だけ markdown 直接 export を採ると `core/document_extract.py` の経路が `google_doc` のみ別分岐になり、Sheets / Slides との API 表面整合性が崩れる。

### 5. 独立 ADR-0026 (Google Workspace connector) / ADR-0027 (Workspace export 経路)

却下理由: Phase 11 流の単一改訂路線 (Teams + Office 抽出も既存 ADR への加算改訂で対応した先例) を踏襲。新 ADR を立てる場合の議論面 (Google Workspace 固有の論点) は ADR-0010 / 0014 / 0025 の延伸条文として吸収可能で、独立 ADR は概念的二重化。Phase 11 plan §2 / Phase 12 plan §2 と同パターン (Phase 11 = 1 新規 + 2 改訂、Phase 12 = 0 新規 + 3 改訂、Phase 13 = 0 新規 + 3 改訂、と縮退継続)。

### 6. Teams pattern (verbatim user token + アプリ層 refresh なし) を Google Workspace にも採用

却下理由: Google Drive API access token は documented 1 hour TTL で短命、refresh token を offline access 取得で受けてアプリ層 refresh するのが Google OAuth 2.0 の標準運用。verbatim token のみ keyring 保管 + アプリ層 refresh なし pattern では、token 失効時に毎回 paste-code flow が必要になり operator UX が極端に劣化。MS365 / Box pattern (refresh token + 自前 refresh + rotation 書き戻し) が Google にも自然な選択 (ADR-0010 §Phase 13 改訂 (h))。

### 7. Phase 13 MVP に multi-account 対応 (multi-principal) を含める

却下理由: Phase 11 Teams (single-slot principal) と同パターンで single-slot に倒す。operator 1 名前提が opshub の MVP scope (ADR-0018 同根拠)。multi-account 対応は Phase 14+ extension (OQ11)。MVP に含めると keyring slot 設計 + cursor 管理 + token lifecycle が分岐し、scope が肥大化する。

### 8. Phase 13 = データ拡張系一括 (OCR / Google Workspace / Notion / Jira / Linear / Confluence) を Phase 12 plan §9 forecast 通り実装

却下理由: コネクタごとに OAuth principal / cursor 戦略 / ADR 増分が異なり、一括 Phase は粒度が大きすぎる。Phase 11「MS Office 深掘り」の単一コネクタ集中パターンに揃える方が CI / 計画 / レビューが回しやすい。画像 OCR は ADR-0025 拡張 (`[ocr]` extras + tesseract 依存)、Notion / Jira は別 OAuth principal で性質が異なるため、後続 Phase に分割 (Phase 14+ outlook §Phase 15+ 候補)。Phase 13 = Google Workspace 単独に再評価 (親 epic #274 §Phase 12 plan §9 からの変更点に記載)。

---

## 関連

- principles.md §1 (Local-first) / §6 (External Content Retention) / §9 (Phased Delivery)
- architecture.md §Connector Layer / §Office Document Extraction Layer / §9 (Phased Delivery)
- ADR-0010 (Connector Contract、本 phase で改訂 = §Phase 13 改訂 (e)-(h))
- ADR-0014 (SaaS Token Storage、本 phase で改訂 = §Phase 7 Validation 節 rotation pin リストに google_workspace 追加)
- ADR-0017 (Knowledge Graph)
- ADR-0019 (Local-FS-backed Connector、Phase 11 で改訂、本 phase での変更なし)
- ADR-0020 (Full Local Content Retention、Google Workspace 本文も対象)
- ADR-0021 (Encryption at Rest、Google Workspace 本文も保護対象)
- ADR-0022 (MCP Server Surface、Phase 12 で改訂、本 phase での変更なし = 既存 read tools (`search` / `recall.search` / `find-document`) が Google Workspace source_type を自動的に活用する設計)
- ADR-0025 (Office Document Content Extraction、本 phase で改訂 = §決定 (d') / §決定 (j))
- 参考実装: `src/opshub/connectors/ms365/auth.py` (token lifecycle 最近傍) / `src/opshub/connectors/box_drive/connector.py` (content_extraction wiring) / `src/opshub/core/document_extract.py` (markitdown 経路)
- Phase 11 plan §3 Sub-issue F2 (Phase 13 G2 と対の関係: F2 = local-FS Office 抽出 / G2 = Workspace export 抽出)
- Phase 12 plan §9 outlook (Phase 13 候補に「データ拡張系一括」と forecast、本 phase で「Google Workspace 単独」に再評価、G5 で書き戻し)
- Phase 13 epic #274、子 sub-issue #275-#279
- markitdown: <https://github.com/microsoft/markitdown>
- Google Drive API v3: <https://developers.google.com/drive/api/v3>
- Google Drive API v3 changes.list: <https://developers.google.com/drive/api/v3/manage-changes>
- Google Drive API v3 files.export: <https://developers.google.com/drive/api/v3/reference/files/export>
- Phase 1 #3 / Phase 2 #23 / Phase 3 #43 / Phase 4 #62 / Phase 5 #81 / Phase 6 #99 / Phase 7 #113 / Phase 8 #128 / Phase 9 #187 / Phase 10 #203 (closed) / Phase 11 #233 (closed) / Phase 12 #253 (closed)
