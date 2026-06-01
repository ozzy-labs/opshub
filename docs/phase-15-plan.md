# Phase 15 Implementation Plan

> Status: **Phase 15 着手中 (2026-06-02、epic #338)**. Scope: **Search 品質改善 (FTS5 日本語 tokenizer trigram 化 + 短クエリ LIKE fallback)** = Phase 10 で導入した `opshub search` (migration 0019、`sources_fts`) が日本語自然文で実質ヒットしない問題を、`sources_fts` の tokenizer を `unicode61 remove_diacritics 2` から `trigram` (FTS5 built-in、SQLite 3.34+) に張り替え + SearchService 層で 1-2 文字短クエリを `body LIKE '%q%'` にルーティングする fallback を入れて根治する。形 A (能動性なし) + 外部書き戻しなしを Phase 10〜14 から継承。
>
> Sub-issue は **S1〜S4 の 4 つ**（親 epic #338、子は S1 から順次起票）。**新規 ADR 1 本** (ADR-0028 = FTS5 sources_fts tokenizer choice)、**改訂 ADR ゼロ**。Phase 11 / 12 / 13 / 14 流の単一トピック集中パターンを継承しつつ、本 Phase は新規 ADR 1 本ルート (Phase 11 = 1 新規 + 2 改訂、Phase 15 = 1 新規 + 0 改訂)。本 plan が SSOT であり、各 sub-issue body は要点抜粋。
>
> 本ドキュメントは **planning skeleton** であり、各 sub-issue の詳細設計・不変条件・最終 DoD は着手前に本 plan 内で確定する。実装契約 (uow_factory / `EventStore.append` / `Projector.apply` / registry SSOT / cold-start guard / `core/sanitise.sanitise_error_message` / Pluggable backend Protocol freeze / Connector framework / `tests/_secrets.py` 連結ビルド規範 等、Phase 1-14 で確立) は Phase 15 も全て継承する。**immutable migration 規範** = migration 0019 を遡及書き換えしない、新 migration 0028 で物理張り替え。

Phase 15 の目的は、opshub の秘書 use case で頻出する「日本語キーワード検索」体験を operator 直感に揃えることにある。具体的には Phase 10 で導入した `opshub search` (FTS5 `sources_fts`) が日本語自然文 (例: 「boxの権限」「進捗記入」「依頼」) で 0 hit になる問題を、`sources_fts` の tokenizer を built-in `trigram` に張り替え + SearchService 短クエリ LIKE fallback を入れて根治する。MCP `search` tool 経由で動く秘書 14 Skill (`find-document` / `personal-brief` / `next-actions` / `meeting-prep` / `research` / etc.) も同じ恩恵を受ける (SearchService 1 箇所改修)。

設計の中心は **scope を絞った 2 点改修**: (i) migration 0028 で tokenizer 物理張り替え、(ii) SearchService 短クエリ LIKE fallback。形態素 tokenizer (Lindera / SudachiPy / MeCab) や dual index (unicode61 + trigram) は overkill のため不採用 (ADR-0028 §Alternatives で却下記録)。MCP `search` tool 契約 (ADR-0022) も触らない。

---

## 1. 確定済み事項

### Open Question 決定 (2026-06-02 設計セッション、親 epic #338 §決定事項)

| # | 論点 | 決定 |
|---|---|---|
| OQ1 | tokenizer 選択 | **FTS5 built-in `trigram` tokenizer に張り替え** (SQLite 3.34+、≥3 文字 substring match が自然に動く、外部依存なし)。形態素 tokenizer は overkill のため却下、ADR-0028 §Alternatives で trade-off 記録 |
| OQ2 | 短クエリ (1-2 文字) | **SearchService 側で `body LIKE '%q%'` にルーティング**。trigram は 3 文字未満をインデックスしないため、UX として「依頼」「PR」等の 2 文字短クエリを 0 hit にしないための補完 |
| OQ3 | dual index 採否 | **不採用** (unicode61 + trigram の 2 本立て)。ストレージ 2 倍 + trigger 倍化 + クエリ二重発行が opshub 規模で overkill |
| OQ4 | migration 戦略 | **新規 migration 0028 で supersede**。`sources_fts` を `DROP TABLE` → `CREATE VIRTUAL TABLE ... USING fts5(... tokenize='trigram')` で作り直し、`INSERT INTO sources_fts(rowid, body) SELECT rowid, body FROM sources` で back-fill。trigger 3 本も再作成 (内容は migration 0019 と同型) |
| OQ5 | `--raw` フラグの扱い | **維持**。trigram tokenizer 化後も operator が FTS5 boolean / prefix を直接書きたいケースは残る (例: `box* AND 権限*`) |
| OQ6 | MCP `search` tool 契約 | **無変更**。ADR-0022 で `raw_query` は hard-coded false。fallback は SearchService 内部処理として透過、tool 契約 (引数 / response schema) は不変 |
| OQ7 | ADR 構成 | **新規 ADR-0028 1 本、改訂ゼロ**。migration 0019 docstring 内に閉じていた tokenizer 選定根拠を独立 ADR に昇格させ、将来 (形態素 tokenizer 評価 / SQLite version up) の判断起点とする |

### Phase 10〜14 から継承する不変方針

1. **形 A**: opshub は MCP server + Agent Skills のみ提供、頭脳 (runtime) は外部ホスト
2. **能動性なし**: リクエスト駆動のみ。本 Phase は `sources_fts` rebuild を CLI の頻繁実行 path に組み込まない (rebuild は migration 0028 で 1 回 + `opshub projections rebuild` への hook 経由のみ)
3. **外部書き戻しなし**: 取り込み + ローカル context 生成のみ
4. **本文ローカル保持** + provenance (ADR-0020)。`sources.body` は本文の正規ストア、FTS5 はあくまで派生 index
5. **SQLCipher 丸ごと暗号化 opt-in** (ADR-0021、keyring 経由)
6. **HITL boundary**: read tools は host LLM 自律 OK
7. **immutable migration 規範**: migration 0019 を遡及書き換えしない、新 migration 0028 で supersede
8. **M6 cold-start guard / `opshub --help` ≤ 300ms / plaintext-leak 検出 CI 常駐**

### Phase 番号

**Phase 15** (top-level)。新 ADR 1 本 (ADR-0028) + 新 migration 1 本 (0028) + SearchService 内部改修を伴う「Search 品質改善」が主目的。Phase 11 流の単一トピック集中パターンに揃え、Phase 14 plan §9 で forecast していた「Phase 15+ 候補」の一部 (operator 体験改善系) を **Phase 15 = Search 品質改善** に再評価した。メモリ方針 [[phase-numbering-new-arch-pattern]] に整合 (新 ADR + 新 migration を伴う作業 = Phase X+1 で起票)。

---

## 2. 新規 / 改訂 ADR

> **新規 1 本 (ADR-0028)、改訂ゼロ**。Phase 11 流の単一 ADR 起票路線。

| ADR | 種別 | タイトル | 主要内容 |
|---|---|---|---|
| **ADR-0028** | 新規 | FTS5 sources_fts tokenizer choice | §Context = 日本語自然文 0-hit 問題と発生条件 (unicode61 が日本語を分割しない)。§Decision = (a) `sources_fts` tokenizer を `trigram` に張り替え (新 migration 0028 で物理 supersede) / (b) 1-2 文字短クエリは SearchService 内で `body LIKE '%q%'` fallback / (c) dual index 不採用 / (d) `--raw` フラグ維持 / (e) `opshub projections rebuild` で sources_fts も自動 rebuild。§Alternatives = (a) unicode61 維持 + クエリ側 auto-`*` / (b) 形態素 tokenizer Lindera / SudachiPy / (c) dual index / (d) 全 query 常時 LIKE。§Consequences = index size 約 3 倍、BM25 ranking が英語 token で若干劣化、1-2 文字短クエリは full scan path、operator は default mode で日本語部分一致が直感どおり動く |

---

## 3. Commit 順序 (Sub-issue 骨子)

> 各 sub-issue を 1 PR に割る。S1 から順次起票する (Phase 13/14 のように先に全部 sub-issue を切ってしまう運用も可能だが、Phase 15 は scope が小さいため逐次起票を default とする)。依存順に並べる。

### Sub-issue S1: ADR-0028 + phase-15-plan + index 更新 (#338 直下、本 PR)

- **依存: なし** (entry)
- `docs/adr/0028-fts5-japanese-tokenizer.md` 新規起草 (Context / Decision / Alternatives / Consequences、上表のとおり)
- `docs/phase-15-plan.md` 新規起草 (本ファイル、PR 4 本 scope / OQ 一覧 / 検証手順 / downgrade 戦略 (§S2 + §Alternatives #5 参照))
- `docs/adr/README.md` に ADR-0028 行追加 (索引同期)
- `AGENTS.md` の Phase status の「次の候補は Phase 15+」記述に `Search 品質改善 (FTS5 日本語 tokenizer 修正、ADR-0028)` を追記 (Phase 15 着手 / 候補化を可視化)
- ADR / plan / index のみで実装変更なし、CI green

**PR S1** `docs: ADR-0028 (FTS5 Japanese tokenizer trigram) + phase-15-plan + index 追加`

### Sub-issue S2: migration 0028 (trigram 張り替え + back-fill + trigger 再作成)

- **依存: S1**
- `src/opshub/db/migrations/versions/0028_rebuild_sources_fts_trigram.py` 新規
  - upgrade: drop trigger 3 + drop `sources_fts` → create with `tokenize='trigram'` → back-fill from `sources.body` → re-create 3 triggers
  - downgrade: drop trigger 3 + drop `sources_fts` → create with `tokenize='unicode61 remove_diacritics 2'` → back-fill → re-create 3 triggers
- `tests/integration/test_phase15_fts_trigram_migration.py` 新規
  - upgrade 後の tokenizer 検証 (`sqlite_master.sql` から tokenize オプションを assert)
  - back-fill 件数が `sources` row 数と一致
  - trigger 3 本が存在
  - `INSERT/UPDATE/DELETE on sources` で `sources_fts` が追従
  - 日本語 body fixture (`boxの権限きれてそうなのですが`) を insert → `MATCH "boxの権限"` で hit
  - downgrade 後に元の tokenizer に戻る
- 既存 `tests/integration/test_phase10_fts_migration.py` は touch せず維持 (immutable migration の不変性確認)
- CI green、`uv run alembic upgrade head` → `downgrade base` → `upgrade head` round-trip OK

**PR S2** `feat(db/migrations): 0028 rebuild sources_fts with trigram tokenizer`

### Sub-issue S3: SearchService LIKE fallback + 日本語 unit / integration test

- **依存: S2**
- `src/opshub/services/search_service.py` 改修
  - `SearchService.search()` に短クエリ判定 (≤2 文字、空白 strip 後の長さ) を追加
  - 短クエリ → `body LIKE '%q%' ESCAPE '\\'` path (LIKE 用 escape は `%` `_` `\\`)、`connector_name` filter / `limit` / ORDER BY は適用、score は固定値 (BM25 計算不可) または `length(body)` 逆数等の単純 ranking
  - 通常クエリ (≥3 文字) は従来どおり FTS5 MATCH (trigram 化後は自然に substring match が動く)
  - `--raw` モード (raw_query=True) では fallback **無し** (operator が FTS5 文法を直接書く前提、OQ11 default)
- `tests/unit/services/test_search_service.py` に日本語ケース追加
  - 「boxの権限」 → hit (trigram path)
  - 「進捗記入」 → hit
  - 「CDKの」 → hit
  - 「依頼」 (2 文字) → hit (LIKE fallback path)
  - 「PR」 (2 文字英字) → hit (LIKE fallback path)
  - 「DailyMeeting」 → hit (trigram path、英数連続 token)
  - LIKE escape 文字 (`%`, `_`, `\\`) を含む短クエリ → escape 正しく適用、SQL injection ガード
  - `--raw` 短クエリ → fallback 無し
- `tests/unit/cli/test_search.py` に同等の smoke test 1-2 件追加 (CLI 経由でも fallback path が動く)
- `tests/unit/mcp/test_phase12_handlers.py` の `search` tool テストは contract 不変なので touch 不要
- CI green、coverage 既存ライン維持

**PR S3** `feat(services/search): trigram path + short-query LIKE fallback`

### Sub-issue S4: Phase 15 closeout (docs)

- **依存: S3**
- `docs/troubleshooting.md` に「日本語 search で hit が少ない場合」節を追加 (旧 workaround `--raw + *` から trigram default への変化、`--raw` の出番が減ったことを記載)
- `AGENTS.md` の Phase status を `Phase 1-15 complete` に更新、Phase 15 の MVP 文を Phase 13/14 同型で追記 (ADR-0028 + migration 0028 + SearchService LIKE fallback + 日本語検索 hit 改善)
- `CLAUDE.md` の `opshub search` 言及箇所 (短行) を「日本語自然文は default mode で部分一致する。`--raw` は FTS5 boolean / prefix が必要な場合のみ」と修正
- `opshub search --help` 出力文の `--raw` 説明を「日本語部分一致は default で動く。`--raw` は AND / OR / prefix 等の FTS5 文法を直接書く power-user 向け」に更新 (`src/opshub/cli/search.py` の click help text)
- `docs/phase-15-plan.md` Status header を `Phase 15 complete (YYYY-MM-DD)` に更新
- CHANGELOG (release-please 経由) は migration / fix 系コミットメッセージで自動生成のため明示変更不要
- CI green

**PR S4** `docs: phase 15 closeout (troubleshooting + AGENTS + CLAUDE + search --help)`

### Wave 配置 (依存 DAG)

```text
Wave 1: S1                ← entry (#338)
Wave 2: S2                ← S1 (migration 0028)
Wave 3: S3                ← S2 (SearchService 改修)
Wave 4: S4                ← S3 (closeout)
```

drive 例: `/drive --merge S1 -> S2 -> S3 -> S4` (各 PR は前 PR の green を待つ sequential)

**並列性の根拠**: migration 張り替えと SearchService 改修は依存関係があるため逐次。S4 は S3 後の closeout のみ。Phase 11 / 13 / 14 の wave 構造と同形 (entry → 基盤 → 改修 → closeout)。

---

## 4. 各 Sub-issue の Definition of Done (骨子)

> 着手前に各項目を具体化。ここでは代表項目のみ。

### S1 — ADR-0028 + phase-15-plan + index

- [ ] `docs/adr/0028-fts5-japanese-tokenizer.md` 起草 (Status: Accepted、Context / Decision / Alternatives / Consequences、上表のとおり)
- [ ] `docs/phase-15-plan.md` 起草 (本ファイル、SSOT として OQ1-7 + 残 OQ8-12 + PR 分割 + 各 Sub DoD + テスト戦略 + Alternatives 網羅)
- [ ] `docs/adr/README.md` に ADR-0028 行追加 (索引同期)
- [ ] `AGENTS.md` の「次の候補は Phase 15+」記述に `Search 品質改善 (FTS5 日本語 tokenizer 修正、ADR-0028)` を追記
- [ ] 新規 ADR 1 本 + 改訂ゼロ方針が plan §2 に明記
- [ ] ADR / plan / index のみで実装変更なし、CI green

### S2 — migration 0028 (trigram 張り替え)

- [ ] `src/opshub/db/migrations/versions/0028_rebuild_sources_fts_trigram.py` 新規 (upgrade + downgrade)
- [ ] `tests/integration/test_phase15_fts_trigram_migration.py` 新規 (tokenizer 検証 + back-fill + trigger + 日本語 MATCH + downgrade)
- [ ] 既存 `tests/integration/test_phase10_fts_migration.py` は touch せず維持
- [ ] CI green、`uv run alembic upgrade head` → `downgrade base` → `upgrade head` round-trip OK

### S3 — SearchService LIKE fallback + 日本語 test

- [ ] `src/opshub/services/search_service.py` に短クエリ判定 + LIKE fallback path を追加
- [ ] `tests/unit/services/test_search_service.py` に日本語 8 ケース + LIKE escape 1 ケース + `--raw` 短クエリ 1 ケース
- [ ] `tests/unit/cli/test_search.py` に日本語 smoke 1-2 件
- [ ] OQ8 (3 文字境界 fallback の閾値) を本 PR 着手時にベンチで確定 (1-2 文字 fallback で十分か、3 文字を含めるか)
- [ ] OQ9 (case sensitivity / NFC 正規化) を本 PR 着手時に確定 (trigram default と揃える)
- [ ] CI green、coverage 既存ライン維持

### S4 — closeout

- [ ] `docs/troubleshooting.md` に日本語 search 節追加
- [ ] `AGENTS.md` Phase status 行 Phase 15 complete + Phase 15 MVP 文追記
- [ ] `CLAUDE.md` の `opshub search` 短行更新
- [ ] `opshub search --help` の `--raw` 説明更新 (`src/opshub/cli/search.py`)
- [ ] `docs/phase-15-plan.md` Status header を `Phase 15 complete (YYYY-MM-DD)` に更新
- [ ] CI green

---

## 5. 設計ドキュメント更新計画 (S4)

- **`docs/troubleshooting.md`**: 「日本語 search で hit が少ない場合」節を追加 (旧 workaround `--raw + *` → trigram default、`--raw` の出番縮小)
- **`AGENTS.md`**: Status 行 Phase 15 complete + Phase 15 MVP 文 (ADR-0028 + migration 0028 + SearchService LIKE fallback)、`source_type` projection / connector 表は無変更 (本 Phase は新 source_type / connector を追加しないため)
- **`CLAUDE.md`**: `opshub search` 言及箇所 (短行) を default 日本語部分一致対応に更新
- **`src/opshub/cli/search.py`**: `--raw` flag の click help text を power-user 向け説明に更新

---

## 6. ユーザー向けドキュメント更新計画 (S4)

- **`docs/troubleshooting.md`**: 日本語 search 節追加
- **`opshub search --help`**: `--raw` の help text を power-user 向けに変更 (default で日本語部分一致が動く旨を明示)
- **README ja/en**: 本 Phase は user-facing API surface 不変 (CLI 引数 / MCP tool 契約) のため大幅変更なし。「日本語自然文検索が改善」程度の短行追記を S4 で判断 (operator UX の改善通知が必要なら)

---

## 7. テスト計画

### 7.1 単体テスト (unit)

- **`services/search_service`** (S3): 日本語 8 ケース (boxの権限 / 進捗記入 / CDKの / 依頼 / PR / DailyMeeting / 権限 / box 権限) + LIKE escape 1 ケース + `--raw` 短クエリ 1 ケース
- **`cli/search`** (S3): 日本語 smoke 1-2 件 (CLI 経由でも fallback path が動く)

### 7.2 結合テスト (integration)

- **`tests/integration/test_phase15_fts_trigram_migration.py`** (S2): migration upgrade / back-fill / trigger / 日本語 MATCH / downgrade を 1 file 集約 (Phase 14 D1 統合パターン継承)
- 既存 `tests/integration/test_phase10_fts_migration.py` は touch せず維持 (immutable 検証)

### 7.3 手動 / E2E (S4 closeout 時)

- `uv run opshub init` で fresh DB → migration head までの一発 upgrade で trigram tokenizer 適用確認
- 既存 DB ある operator は `uv run opshub db migrate` で 0028 を順当に適用、back-fill 自動実行
- `uv run opshub search "boxの権限" --connector slack` で hit (旧 0 hit → 解消)
- `uv run opshub search "依頼"` で hit (LIKE fallback path)
- `uv run opshub search "DailyMeeting"` 既存 hit を維持 (regression なし)
- `uv run opshub search "box* AND 権限*" --raw` 既存 power-user path 維持

### 7.4 持続検証 / guard

- M6 cold-start guard 維持 (`services/search_service.py` の module-level import に重い依存を追加しない)
- `time opshub --help` ≤ 300ms 維持
- 暗号化平文リーク検出 CI 常駐継続

### 7.5 性能

- S3 で `sources` row 数 = 現状 (operator の actual DB) + 1M row 想定 fixture で LIKE fallback 実行時間を測定。許容 = 短クエリで <1s。超えるならコメント残して別 issue 切る (B-tree index 追加 / FTS5 short-query auxiliary index 等)

---

## 8. Open Questions (残)

> Phase 15 着手時に確定済の OQ1-7 は §1 確定済み事項参照。以下は **S2 / S3 着手時に決定** する実装詳細。

| # | 論点 | 決定時期 |
|---|---|---|
| OQ8 | LIKE fallback の **閾値** (現案 = 1-2 文字)。3 文字ジャストの境界ケース (trigram が 1 個しか作れない) も fallback 含めるか、純 trigram で十分か | S3 着手時、実測ベンチで確定 |
| OQ9 | LIKE fallback の **case sensitivity / NFC 正規化**。trigram tokenizer の `case_sensitive` option との挙動差を埋めるか trigram と同じ挙動 (default = case-insensitive) に揃えるか | S3 着手時、trigram default と揃える方針で確定見込み |
| OQ10 | LIKE fallback の **connector filter / limit 適用順**。`body LIKE '%q%' AND connector_name = ?` + `LIMIT n` で済むが、index hit 不可なため full scan。`sources` row 数増加に伴う性能境界 (~1M rows で許容できるか) を S3 で測定、超えたら別途 issue で索引追加 | S3 着手時 |
| OQ11 | `--raw` モードでの LIKE fallback の扱い (現案 = `--raw` のときは fallback **無し** = operator 責任で FTS5 文法を書く前提) | S3 着手時 |
| OQ12 | `opshub projections rebuild` (ADR-0022) との連動。projection rebuild 時に sources_fts も自動 rebuild するか、別 CLI (`opshub search rebuild-index` 等) を切るか。MVP は projection rebuild に乗せる方針だが S2 で確定 | S2 着手時 |

着手中に新たな OQ が発生した場合は本節を更新。

---

## 9. Phase 16+ outlook

**Phase 16 候補** (Phase 14 §9 / 本 Phase で deferred):

- 形態素 tokenizer 採用 (Lindera / SudachiPy / MeCab)。本 Phase の trigram で operator 体験が不足する場合に再評価 (ADR-0028 改訂 + tokenizer 評価 + 辞書同梱 trade-off + apsw 移行検討)
- dual index (unicode61 + trigram)。本 Phase で却下したが、BM25 ranking の劣化が operator 観測されたら再評価
- `opshub search rebuild-index` 専用 CLI (projection rebuild 経由で十分か別 CLI を切るかは OQ12 で判断)
- MCP `search` tool 契約改訂 (`raw_query` を operator 露出するか等は ADR-0022 改訂事項)
- 検索結果の semantic re-rank (vector との hybrid score、別 ADR)
- snippet() ハイライト (Phase 10.x 候補に既出)
- 検索クエリの NFKC 正規化 / 全角半角統一 (operator 観測されてから別 issue)

**Phase 14 §9 から繰り越し** (本 Phase で touch しないが Phase 16+ で残る):

- multi-machine sync / 能動性段階 1-4 = cron 委譲 / 記憶キュレーション / 通知 / filewatch / Gmail push / Calendar push 再評価
- 画像 OCR (PPT 内画像 + Office 図表、tesseract / pytesseract)
- Drive Comments / Suggestions 取り込み
- Gmail 添付 / Calendar 添付の本文抽出 (markitdown 経路、ADR-0025 拡張)
- 追加コネクタ Notion / Jira / Linear / Confluence
- 外部書き戻し (Teams 返信送信 / Gmail send / Calendar event create + HITL)
- Calendar instance 展開 projection (master + RRULE → instance dynamic、ms365 / google 両 calendar 同時)
- `ozzy-labs/skills` 配布完成 (ADR-0004 §決定 (c) 復権 + Renovate preset 整備)

---

## X. 設計選択の trade-off

Phase 15 着手時に検討した主要な設計選択について、採用案と却下案の trade-off を明示する (ADR-0028 §Alternatives で詳細記録済の項目は本節では要点のみ)。

### X.1 tokenizer 選択: trigram vs 形態素 (Lindera / SudachiPy) vs dual index

ADR-0028 §Alternatives (b)(c) で trade-off 記録済。要点:

- trigram (採用) = built-in、外部依存ゼロ、cold-start 影響なし、3 文字以上で substring match、operator の現課題を最小コストで解消
- 形態素 (却下) = install size 数十〜数百 MB、apsw 移行 / C 拡張必要、辞書同梱、Phase 15 scope では overkill
- dual index (却下) = ストレージ 2 倍 + trigger 倍化 + クエリ二重発行、opshub 規模で overkill

### X.2 短クエリ fallback: LIKE vs trigram-only vs unicode61 残置

| 観点 | LIKE fallback (採用) | trigram only (≥3 文字のみ動く) | unicode61 残置 |
|---|---|---|---|
| 1-2 文字短クエリの hit | 動く (full scan) | 動かない (trigram は 3 文字未満を index しない) | 動かない (元々の課題と同じ) |
| 実装複雑度 | 低 (SearchService 内 1 分岐) | ゼロ (何もしない) | 中 (dual index 必要) |
| 性能影響 | 短クエリで full scan、長クエリ無影響 | 影響なし | クエリ二重発行 |
| operator UX | 「依頼」「PR」等の 2 文字 query が hit する | 短クエリは 0 hit のまま | 短クエリは 0 hit のまま |

**採用根拠**: 短クエリ (1-2 文字) は operator の自然な探索パターン (「PR」「Q4」「依頼」「会議」)。trigram only で残すと体験不連続 (3 文字以上は動くが 2 文字は 0 hit)、LIKE fallback で連続体験を担保。性能は `sources` row 数で full scan が問題化しうるが、operator 1 名前提の規模では受容範囲内 (S3 でベンチ確定)。

### X.3 `--raw` モードでの LIKE fallback: 付ける vs 付けない

**採用案**: `--raw` モードでは LIKE fallback を **付けない** (operator 責任で FTS5 文法を書く前提)。

**却下案**: `--raw` でも LIKE fallback を効かせる。

**trade-off**: `--raw` は FTS5 boolean / prefix / column を直接書く power-user 向け契約 (現行 `SearchService.search(raw_query=True)`)。LIKE fallback を勝手に効かせると「`--raw` モードで明示的に書いた MATCH 式が無視される」体験不連続が生じる。`--raw` は operator が「FTS5 を直接叩いている」mental model を維持し、fallback は default モードの体験補完に限定する。

---

## Alternatives (却下した選択肢と理由)

### 1. tokenizer 張り替えなし、auto-`*` で済ます

却下理由: 「boxの権限」は本文中の長いトークンの**中間**に位置するため prefix match では当たらない。auto-`*` (prefix 演算子の自動付与) では「boxの権限きれてそう」というトークンの prefix が `box` から始まる must match のため、`boxの権限` 入力に対して hit にならない。auto-`*` では根本解決にならず、tokenizer 張り替えが必要 (ADR-0028 §Alternatives (a))。

### 2. 形態素 tokenizer (Lindera / SudachiPy / MeCab) を本 Phase で採用

却下理由: install size + apsw 移行 + C 拡張 + 辞書同梱 trade-off で本 Phase 範囲を大きく超える。trigram で実用上 operator の現課題は解消する見込み。trigram でも足りない operator 体験が観測されたら別 Phase で再評価 (ADR-0028 §Alternatives (b))。

### 3. dual index (unicode61 + trigram)

却下理由: ストレージ 2 倍 + trigger 倍化 + クエリ二重発行が opshub 規模で overkill。BM25 ranking の trade-off は trigram 単独でも受容範囲内 (ADR-0028 §Alternatives (c))。

### 4. 全 query 常時 LIKE (`body LIKE '%q%'`) で FTS5 を使わない

却下理由: BM25 ranking が完全に失われ、`sources` row 数増加に対してスケールしない (full scan が常時発生)。長クエリは FTS5 + trigram で index 検索が効くため、短クエリ専用 fallback として LIKE を限定使用する (ADR-0028 §Alternatives (d))。

### 5. migration 0019 を遡及書き換え

却下理由: immutable migration 規範 (Phase 1 以降) に抵触。既に main / operator DB に適用済の migration を書き換えると、新規 operator と既存 operator で schema が divergence する。新 migration 0028 で supersede し、downgrade で元 tokenizer に戻せる safety を担保する。

### 6. MCP `search` tool 契約 (ADR-0022) で `raw_query` を operator 露出

却下理由: ADR-0022 で `raw_query` は hard-coded `false` (host LLM が誤って FTS5 boolean を直叩きしないためのガード)。trigram tokenizer 化で日本語 default 体験が改善されれば、operator (host) 側で `--raw` を意識する必要がさらに減る。MCP `search` tool 契約変更は別 Phase の論点 (本 plan §9 / Phase 16+)。

### 7. `opshub search rebuild-index` 専用 CLI を切る

却下理由 (現段階): rebuild は migration 0028 で 1 回 + `opshub projections rebuild` (ADR-0022) への hook 経由で済む。専用 CLI を切るほど頻繁に rebuild する operation pattern が顕在化していない。必要が出てきたら別 issue で切り出す。

### 8. ADR を改訂で吸収 (migration 0019 docstring を ADR-0019 / ADR-0020 等の改訂条文に書く)

却下理由: tokenizer 選定は migration 0019 docstring 内に閉じていた暗黙判断で、独立 ADR に昇格させて「将来の見直し起点」を明示する価値がある (形態素 tokenizer 評価 / SQLite version up / 検索 ranking ストラテジ 全般の起点)。Phase 11 / 12 / 13 / 14 流の単一改訂路線とは趣旨が異なるが、本 Phase は新規 ADR 1 本路線を採る。

---

## 関連

- principles.md §1 (Local-first) / §6 (External Content Retention、`sources.body` 本文保持)
- architecture.md §Search Layer / §9 (Phased Delivery)
- ADR-0001 (Python Stack、cold-start 予算)
- ADR-0020 (Full Local Content Retention、`sources.body` は本文の正規ストア、FTS5 は派生 index)
- ADR-0022 (MCP Server Surface、`search` tool 契約 = `raw_query` hard-coded false、本 Phase では不変)
- ADR-0028 (新規、本 Phase で起草) — FTS5 sources_fts tokenizer choice (trigram + short-query LIKE fallback)
- migration: 0019 (Phase 10、本 Phase で supersede) → 0028 (本 Phase 新規、S2)
- Phase 10 #210 (FTS5 sources_fts 初期実装)
- Phase 14 plan §9 (本 Phase 15 候補移送元)
- Phase 15 epic #338、子 sub-issue S1 から順次起票
- 参照: SQLite FTS5 trigram tokenizer — <https://www.sqlite.org/fts5.html#the_trigram_tokenizer>
