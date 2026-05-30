# Phase 11 Implementation Plan

> Status: Draft (起票後骨子). Last reviewed: 2026-05-31. Scope: **MS Office 深掘り** = Teams 新コネクタ＋Outlook 本文 deep retention＋Word/Excel/PowerPoint 文書抽出。Phase 10 plan §3 Sub-issue F / §9 outlook で forecast 済みの内容を実装。形A（runtime なし）・能動性なし・外部書き戻しなしを Phase 10 から継承。
>
> Sub-issue は **F1〜F6 の6つ**（親 epic #233、子 #234〜#239）。新規 ADR は 1本（ADR-0025）+ 改訂 2本（ADR-0019 / ADR-0010）に縮退（Phase 10 の3新規+4改訂から半減）。本 plan が SSOT であり、各 sub-issue body は要点抜粋。
>
> 本ドキュメントは **planning skeleton** であり、各 sub-issue の詳細設計・不変条件・最終 DoD は着手前に本 plan 内で確定する。実装契約（uow_factory / `EventStore.append` / `Projector.apply` / registry SSOT / cold-start guard / `core/sanitise.sanitise_error_message` / Pluggable backend Protocol freeze / Connector framework / 7 link_type + reply_draft / `tests/_secrets.py` 連結ビルド規範 等、Phase 1-10 で確立）は Phase 11 も全て継承する。

Phase 11 の目的は、Phase 10 で「動く秘書の枠組み」を完成させた後、**MS Office 環境のデータを秘書の context に統合**することにある。具体的には Teams chat、Outlook 本文、Word/Excel/PowerPoint 文書を `body + provenance` 付きで取り込み、recall / search / reply-draft / meeting-prep 系 skill が MS Office 由来の情報を一級市民として扱えるようにする。

Office 抽出は markitdown 経由で多形式統一、ローカル FS（Box Drive / OneDrive Desktop）を一級経路として ADR-0019 の §パターン汎化を初実利用する。Teams は Microsoft Graph delta query + User Token principal で取り込み、Slack ADR-0018 / 既存 ms365 connector のパターンに揃える。

---

## 1. 確定済み事項

### Open Question 決定（2026-05-31 設計セッション）

| # | 論点 | 決定 |
|---|---|---|
| OQ1 | Office 文書抽出ライブラリ | **markitdown**（Microsoft 公式 Python、多形式統一、markdown 出力）。`[office]` extras に閉じて cold-start guard 維持 |
| OQ2 | 巨大ファイル上限 | **ファイル 50 MB / 抽出後テキスト 500K chars**。超過は skip/truncate + warning log。`opshub.toml` で operator 上書き可 |
| OQ3 | onedrive_drive 用 ADR | **ADR-0019 改訂で吸収**（独立 ADR-0027 不要）。box_drive と同パターン |
| OQ4 | Teams cursor 戦略 | **Graph delta link + TTL 失効時 full-pass fallback**。ADR-0010 改訂で delta+fallback 契約化 |
| OQ5 | Office source_type | **細分3タイプ**：`word_document` / `excel_spreadsheet` / `powerpoint_slide_deck` |
| OQ6 | Excel 抽出範囲 | **全シート、10K cells/シート + 50K cells/ブック** 上限で防御 |
| OQ7 | PowerPoint 抽出範囲 | **本文 + ノート**含む。画像 OCR は Phase 12+ |

### Phase 10 から継承する不変方針

1. **形A**: opshub は MCP server + Agent Skills のみ提供、頭脳（runtime）は外部ホスト。
2. **能動性なし**: リクエスト駆動のみ。常駐・定期実行は Phase 12+。
3. **外部書き戻しなし**: 取り込み専用、書き戻しは ADR-0010 で明示禁止（緊張点③）。
4. **本文ローカル保持** + `provenance_origin="external"` / `provenance_trust="untrusted"` 付き（ADR-0020）。
5. **SQLCipher 丸ごと暗号化 opt-in**（ADR-0021、keyring 経由）。
6. **excludes.yaml 共通機構**を Teams 等にも適用（channels selector 再利用）。

### Phase 番号

**Phase 11**（top-level）。新 ADR 1本 + 新 connector（teams / onedrive_drive）+ 新 source_type 3 種を伴うため Phase 10.x 枠ではない（メモリ方針 [[phase-numbering-new-arch-pattern]] に整合）。

---

## 2. 新規 / 改訂 ADR

> ADR-0025 は新規、ADR-0019 / ADR-0010 は改訂。独立 ADR-0026（Teams principal） / ADR-0027（onedrive_drive） は **立てない**（OQ3 / OQ4 で吸収先確定済）。

| ADR | 種別 | タイトル | 主な論点 |
|---|---|---|---|
| **ADR-0025** | 新規 | Office Document Content Extraction | markitdown 採用根拠（多形式統一・markdown 出力・Microsoft 公式）/ 50 MB+500K chars cap / 細分 source_type 3 種 / Excel 10K+50K cells 上限 / PPT 本文+ノート / 画像 OCR Phase 12+ defer / 抽出失敗 fail-safe policy（warning log + body=None）/ ADR-0019 との緊張解消 |
| **ADR-0019** | 改訂 | Local-FS-backed Connector | (a) §不変条件 (b) `open()` ban に `[connectors.<name>] content_extraction = true` opt-in 例外節（明示的 extractor lib 経路のみ open 許可、CldAPI/FSE hydration 抑制ガイドライン継続）/ (b) §パターン汎化節に onedrive_drive 追加、`root_path` platform default 表に **WSL2 `/mnt/onedrive`** / **macOS `~/OneDrive`** を追記 |
| **ADR-0010** | 改訂 | Connector Contract | **Phase 10 改訂（write-back ban）に追加で**：(a) Teams 新コネクタ追加 / (b) 本文抽出契約（連続 stat → 抽出 → SourceObserved with body）/ (c) delta-link cursor + 失効時 full-pass fallback 義務 / (d) Teams User Token principal を明記（Bot Token は alternative） |

---

## 3. Commit 順序（Sub-issue 骨子）

> 各 sub-issue を 1〜複数 PR に割る。詳細 PR 分割と DoD は着手前に確定。依存順に並べる。

### Sub-issue F1: ADRs (#234)

- ADR-0025 起草 + ADR-0019 改訂 + ADR-0010 改訂 を1 PR で Accepted
- `docs/decisions-log.md` entries（3件）

**PR F1** `docs(adr): adr-0025 office doc extraction / adr-0019 + 0010 改訂 (phase 11)`

### Sub-issue F2: Office 抽出 foundation (#235)

- `src/opshub/core/document_extract.py` 新設（markitdown 経由、Word/Excel/PPT 統一 API、size+text cap、fail-safe）
- `[office]` extras 定義（pyproject.toml に `office = ["markitdown>=0.1"]`）
- CI workflow `--extra office` 追記（mypy strict / pytest 両方で必要）
- mypy types 整合（markitdown stub 不在なら `# type: ignore[import-untyped, unused-ignore]` 同梱）
- 新 source_type を `domain/events/source.py` の Literal に登録：`word_document`, `excel_spreadsheet`, `powerpoint_slide_deck`
- unit tests（各形式 / 失敗 fail-safe / size 超 skip / cells 超 truncate / PPT ノート抽出）

**PR F2** `feat(core): office document extraction foundation`

### Sub-issue F3: Outlook 本文 deep retention (#236)

- **依存: F1 のみ**（Outlook body は plain text/HTML、markitdown 抽出層 = F2 を経由しない）
- `src/opshub/connectors/ms365/mapper.py` の `map_outlook_message` を本文取り込みに拡張
- 既存 outlook source の backward-compat（body=None で従来挙動）
- 巨大メール（500K chars 超）の truncate は F3 内 inline 実装（F2 の `core/text_limits` 共通機構化は並行進行、統合は将来 PR）
- tests: 本文取り込み / provenance / 既存挙動非破壊 / truncate

**PR F3** `feat(connectors/ms365): outlook body deep retention`

### Sub-issue F4: Office FS-scan (#237)

- **F4-a**: `box_drive/scanner.py` に Office 拡張子フック（.docx/.xlsx/.pptx 検出時に `core/document_extract` を呼ぶ）+ `content_extraction` 設定（box_drive settings、default false）+ mapper が body + provenance を載せる + tests
- **F4-b**: `connectors/onedrive_drive/` connector 一式（scanner + mapper + connector + settings、box_drive を踏襲）+ `root_path` platform default（WSL2 `/mnt/onedrive` / macOS `~/OneDrive`）+ `opshub connector sync onedrive_drive` 経路 + CldAPI hydration 抑制 contract test + tests

**PR F4-a** `feat(connectors/box_drive): office content extraction hook`
**PR F4-b** `feat(connectors/onedrive_drive): new local-fs connector`

### Sub-issue F5: Teams connector (#238)

- **依存: F1 のみ**（Teams body は chat text、抽出層 = F2 を経由しない）
- `connectors/teams/` connector 一式（auth + fetcher + mapper + connector + settings）
- Teams User Token を keyring 経由（ADR-0014 再利用、`OPSHUB_CONNECTOR_TEAMS_TOKEN` env override）
- Graph delta query（`/me/chats/getAllMessages` または `/teams/{id}/channels/{id}/messages/delta`）
- TTL 失効時の full-pass fallback（直近 N 日、N は設定可、default 30）
- excludes.yaml の channels selector を再利用（Slack 同パターン）
- mapper が body + provenance を SourceObserved に載せる
- `[connectors-teams]` extras（msal + httpx）
- CI workflow `--extra connectors-teams` 追記
- tests: auth / fetcher（Graph mock）/ mapper / connector（cursor + fallback） / excludes 動作

**PR F5** `feat(connectors/teams): graph chat delta + user token`

### Sub-issue F6: Phase 11 closeout (#239)

- 設計 docs 一括（§5）
- ユーザー docs 一括（§6）
- e2e lifecycle test（§7.3）
- guard 確認（§7.4）
- AGENTS.md / CLAUDE.md Status 行 Phase 11 complete

**PR F6** `docs: phase 11 closeout + e2e`

### Wave 配置（依存 DAG）

```text
Wave 1: F1                         ← entry
Wave 2: F2 / F3 / F5（3 並列）    ← F1
Wave 3: F4                         ← F2
Wave 4: F6                         ← F2-F5
```

drive 例: `/drive #234 -> #235,#236,#238 -> #237 -> #239`（Wave 2 で F2/F3/F5 が3並列、最大効率）

**最適化の根拠（2026-05-31 audit）**：F3（Outlook body）と F5（Teams chat body）は markitdown 抽出層を経由しないため F2 に依存しない。OQ2 の 500K chars truncate は F3 内 inline 実装で F2 を待たず先行可能。F4 のみ document_extract（F2）が必須なため Wave 3。

---

## 4. 各 Sub-issue の Definition of Done（骨子）

> 着手前に各項目を具体化。ここでは代表項目のみ。

### F1 — ADRs

- [ ] ADR-0025 Accepted + decisions-log.md entry
- [ ] ADR-0019 改訂（content_extraction 例外節 + onedrive_drive 汎化追記）+ decisions-log.md entry
- [ ] ADR-0010 改訂（Teams + 本文抽出契約 + delta+fallback + User Token）+ decisions-log.md entry
- [ ] markitdown 採用 / 50MB+500K cap / 細分 source_type が ADR-0025 で明文化
- [ ] CldAPI/FSE hydration 抑制ガイドラインが ADR-0019 §例外節に明記

### F2 — 抽出 foundation

- [ ] `core/document_extract.py` で 3 形式統一抽出 API
- [ ] 50 MB / 500K chars 上限が `opshub.toml` で operator 上書き可能
- [ ] 抽出失敗 fail-safe（warning log + body=None で SourceObserved）
- [ ] Excel: 10K cells/シート + 50K cells/ブック 上限、超過時注記
- [ ] PowerPoint: 本文 + ノート両方抽出（画像 OCR なし）
- [ ] `[office]` extras 定義 + CI workflow 同期
- [ ] mypy strict pass（types-* stubs or `# type: ignore[..., unused-ignore]`）
- [ ] 3 source_type を `domain/events/source.py` の Literal に登録

### F3 — Outlook body

- [ ] outlook mapper が body + provenance を SourceObserved に載せる
- [ ] 既存 outlook source の backward-compat（body=None で従来挙動）確認
- [ ] excludes.yaml の senders selector が効くこと確認
- [ ] 巨大メールの truncate

### F4 — Office FS-scan

- [ ] box_drive で .docx/.xlsx/.pptx 検出時に markitdown 抽出 → body + provenance
- [ ] `[connectors.box_drive] content_extraction = false` で従来挙動（backward-compat）
- [ ] onedrive_drive connector が box_drive 同等カバレッジで動作
- [ ] WSL2 / macOS の platform default が正しく解決
- [ ] CldAPI/FSE hydration 抑制 contract test pass（box_drive 同パターン）

### F5 — Teams

- [ ] Teams connector が User Token で認証、`auth set connector:teams` で keyring に保存
- [ ] Graph delta query で差分取得、cursor opaque で `connector_cursors` に保存
- [ ] TTL 失効時の fallback（直近 N 日 full pass + 新 delta link 取得）
- [ ] excludes.yaml の channels selector が効く
- [ ] mapper が body + provenance を SourceObserved に載せる
- [ ] `[connectors-teams]` extras + CI workflow 同期

### F6 — closeout

- [ ] 設計 docs（principles / architecture / repository-structure / decisions-log）更新済み
- [ ] ユーザー docs（README ja/en / upgrading / SECURITY / 新規 onedrive-drive-setup / teams-setup / secretary-agent）更新済み
- [ ] e2e lifecycle test pass（test_phase11_office_lifecycle.py）
- [ ] M6 guard / `opshub --help` ≤ 300ms 維持、暗号化平文リーク検出 CI 常駐継続
- [ ] AGENTS.md / CLAUDE.md Status 行 Phase 11 complete
- [ ] **`docs/phase-11-plan.md` Status header を `Phase 11 complete (YYYY-MM-DD)` に更新**（Phase 10 Round 2 R2-CROSS-06 教訓）
- [ ] **F6 マージ後、main の CI workflow が green であることを確認**（mypy strict + targeted pytest、Phase 10 #221 hotfix 経験を踏まえた事前確認）

---

## 5. 設計ドキュメント更新計画（F6）

- **`docs/principles.md`**:
  - §1 Local-first — OneDrive Desktop / Box Drive 両方 FS 扱いを追記
  - §6 External Content Retention — Office 本文も保持対象（ADR-0025 反映）
  - §9 Phased Delivery — Phase 11 行
- **`docs/architecture.md`**:
  - §Connector Layer — Teams / onedrive_drive 追加、box_drive 拡張節
  - 新 §Office Document Extraction Layer（markitdown 経路、fail-safe、size cap）
  - §9 Phased Delivery — Phase 11 行
- **`docs/repository-structure.md`**: 新モジュール `src/opshub/core/document_extract.py`、`src/opshub/connectors/onedrive_drive/`、`src/opshub/connectors/teams/`
- **`docs/decisions-log.md`**: ADR-0025 + ADR-0019 改訂 + ADR-0010 改訂 entry

---

## 6. ユーザー向けドキュメント更新計画（F6）

- **`README.md` / `README.ja.md`**:
  - Phase 11 行追記
  - 新 extras（`office`, `connectors-teams`）を extras 表に追加
  - 新 connector 表（teams / onedrive_drive）
  - 依頼例に「Word/Excel/PPT 探して」「Teams スレッド要約して」追加
  - 「OpsHub に今あるもの」表に Phase 11 行追加
- **`docs/upgrading.md`**: Office 抽出有効化手順（`[office]` extras + `content_extraction = true` opt-in + `[connectors-teams]` extras）
- **`SECURITY.md`**: Office 本文 local 保持の含意 / Teams 取り込みの含意
- **新規 `docs/onedrive-drive-setup.md`**: mount 設定、`root_path` 設定（Box Drive setup と並ぶ）
- **新規 `docs/teams-setup.md`**: User Token 取得（Azure Portal app registration）、auth set フロー、permissions scope（Chat.Read 等）
- **`docs/secretary-agent.md`** 追記: Teams / Outlook 本文 / Office 文書を秘書の文脈源として利用できる旨

---

## 7. テスト計画

### 7.1 単体テスト（unit）

- **`core/document_extract`**: 各形式 (.docx / .xlsx / .pptx) 抽出、失敗 fail-safe（破損ファイル）、50 MB 超 skip、500K chars truncate、Excel 10K cells 超 truncate、PPT ノート抽出含有
- **`connectors/teams`**: auth（keyring + env override）/ fetcher（Graph chat list_messages mock）/ mapper（body + provenance）/ connector（cursor + delta + fallback）
- **`connectors/onedrive_drive`**: scanner（box_drive 同等パターン）/ mapper / connector
- **`connectors/box_drive` 拡張**: scanner の Office 拡張子フック / `content_extraction = false` で従来挙動
- **`connectors/ms365/mapper`**: outlook 本文 deep retention / backward-compat
- **`core/excludes`**: Teams channels selector の再利用

### 7.2 結合テスト（integration）

- **`tests/integration/test_phase11_office_extract.py`**: 実 .docx/.xlsx/.pptx fixture から markitdown 経由抽出 → body persist → recall hit
- **`tests/integration/test_phase11_teams_sync.py`**: Teams connector の取り込み e2e（Graph mock）+ delta cursor 進行 + TTL 失効 fallback
- **`tests/integration/test_phase11_onedrive_drive_sync.py`**: FS-scan e2e（tmp dir + 抽出）
- **`tests/integration/test_phase11_outlook_body_migration.py`**: 既存 outlook source の body deep retention 確認

### 7.3 CldAPI 非 hydration contract test

- **`tests/integration/test_onedrive_drive_no_hydration.py`**: 既存 box_drive 同パターン、`OPSHUB_ONEDRIVE_DRIVE_TEST_ROOT` env 設定時のみ起動

### 7.4 e2e lifecycle テスト

- **`tests/integration/test_phase11_office_lifecycle.py`**: **Office データ統合シナリオ**（meeting-prep 等の skill は Phase 11 scope 外、§9 outlook 参照）を台本 MCP クライアントで再現。本テストは Phase 11 データパイプライン（Teams + Outlook 本文 + Office 文書抽出）の MCP 経由動作確認に限定。
  1. Teams + Outlook + .docx/.xlsx/.pptx を取り込み（tmp dir fixture、markitdown 経由抽出）
  2. MCP `search` / `recall.search` で「●●会議」関連トピック検索 → Teams / Outlook / Office source hit
  3. MCP `recall.search` + `graph.related`/`graph.expand`（Step 1 widening 済）で thread + 関連文書集約
  4. write-back 経路が呼べないこと（write-back 非存在）を確認
- 形A につき opshub 内に頭脳はないので **MCP クライアントが台本どおりツール呼び出し列を再現** して MCP 面と ①コアを検証（実エージェント・実 LLM は不要）

### 7.5 持続検証 / guard

- M6 cold-start guard（teams / onedrive_drive / document_extract module-level import が whitelist 内）
- `time opshub --help` ≤ 300ms 維持
- 暗号化平文リーク検出 CI 常駐継続
- gitleaks / secret scanner 対策: テストフィクスチャは `tests/_secrets.py` から import（連結ビルド規範を Phase 11 でも継続）

---

## 8. Open Questions

> Phase 11 着手時点で**全て解消済み**（2026-05-31 設計セッション）。OQ1〜OQ7 は §1 確定済み事項参照。

着手中に新たな OQ が発生した場合は本節を更新。

### 着手中に追加で詰める実装詳細（forecast）

- markitdown のノート抽出デフォルト挙動の確認（含めない場合は python-pptx 直接呼びでフォローアップ）
- Graph delta query の TTL 実値とエラーレスポンス形（実装時に確認）
- onedrive_drive の WSL2 mount 手順の更新（OS 仕様変化のため）

---

## 9. Phase 12 / 以降 outlook

- **Phase 12 候補（仮）= 統合・検索融合レイヤ**: Phase 10 plan §9 で forecast 済み。RRF 多チャネル検索（sqlite-vec＋FTS5＋graph、ADR-0017 #5 充足）/ dreaming 型 記憶キュレーション（純派生・cron 委譲）/ bi-temporal links（Zep）。
- **能動性**（緊張点②の将来対応、段階案）: 段階 0 期限データ → 1 cron 委譲の冪等コマンド → 2 dreaming 型キュレーション → 3 通知 → 4 filewatch（sidecar/skill のみ、core 不可）。always-on VM はアンチパターン。
- **画像 OCR**（OQ7 で defer）: tesseract / pytesseract 経由で PPT 内画像 + Office 図表のテキスト抽出。視覚抽出レイヤとして独立 ADR 候補。
- **外部書き戻し**（緊張点③の将来対応）: 「下書き生成」と「投稿実行」を別機能に分離、投稿は毎回明示承認必須。Teams への返信投稿は最有力候補。
- **追加コネクタ**: Google Workspace（Docs / Slides / Sheets）= markitdown 経路パターン再利用 / Notion / Jira。
- **Skills 追加**: meeting-prep / research / inbox-triage（Phase 11 Office データで真価を発揮）/ status-update / decision-context / dedupe-check。Step 1（MCP tool widening = brief / graph.*/ source.* / embeddings.find_duplicates / propose.generate）と組み合わせて運用。

---

## 関連

- principles.md §1 (Local-first) / §6 (External Content Retention) / §9 (Phased Delivery)
- architecture.md §Connector Layer / 新 §Office Extraction Layer / §9 (Phased Delivery)
- ADR-0010 (Connector Contract、本 phase で改訂)
- ADR-0014 (SaaS Token Storage、Teams User Token で再利用)
- ADR-0017 (Knowledge Graph、§5 graph hybrid recall は Phase 12+ で対応)
- ADR-0019 (Local-FS-backed Connector、本 phase で改訂＝§例外節 + §汎化)
- ADR-0020 (Full Local Content Retention、Office 本文も対象)
- ADR-0021 (Encryption at Rest、Office 本文も保護対象)
- ADR-0022 (MCP Server Surface、Step 1 widening で brief / graph.*/ source.* / propose.generate を追加予定)
- ADR-0025 (Office Document Content Extraction、本 phase で新規)
- Phase 10 plan §3 Sub-issue F (Phase 11 へ分離した forecast)
- Phase 10 plan §9 outlook (Phase 11 = MS Office 深掘り)
- Phase 11 epic #233、子 sub-issue #234-#239
- Phase 1 #3 / Phase 2 #23 / Phase 3 #43 / Phase 4 #62 / Phase 5 #81 / Phase 6 #99 / Phase 7 #113 / Phase 8 #128 / Phase 9 #187 / Phase 10 #203 (closed)
