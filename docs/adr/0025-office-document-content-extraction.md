# 0025. Office Document Content Extraction

- Status: Accepted
- Date: 2026-05-31
- Deciders: ozzy

## Context

Phase 10 (epic #203) で OpsHub は本文ローカル保持 (ADR-0020) + SQLCipher 暗号化 (ADR-0021) + provenance タグ (ADR-0020 §(e)) の 3 層で「外部 SaaS 本文を operational memory に直接持つ」設計に転換した。Slack message / Outlook mail / GitHub issue body / Box event 等のテキスト経路は Phase 10 で本文取り込みが完了したが、**Office 系バイナリ文書 (Word `.docx` / Excel `.xlsx` / PowerPoint `.pptx`)** は本文経路が未整備のまま残った。

Phase 11 (epic #233) は「MS Office 深掘り」を目的とし、Teams chat + Outlook 本文 deep retention + Word/Excel/PowerPoint 文書抽出を `body + provenance` 付きで取り込めるようにする。本 ADR は **Office 系バイナリ文書を OpsHub の Source として取り込む際の本文抽出契約** を pin する。

Office 抽出の論点は以下:

1. **抽出ライブラリの選定** — Word/Excel/PowerPoint のいずれも独自バイナリ形式 (Office Open XML、ZIP container + XML parts) を持ち、Python だけでもファイル形式ごとに別ライブラリ (`python-docx` / `openpyxl` / `python-pptx`) が必要。連結すると `core/document_extract.py` の API 表面が形式ごとに分岐して肥大化する。逆に「多形式 → markdown」を 1 API で扱える wrapper を採れば caller (mapper / FS scanner) は形式を意識せず本文を得られる
2. **巨大ファイル対策** — Box Drive / OneDrive Desktop には 100MB 超の Excel / 1000 slide 超の PPT が混在し得る。抽出後テキストも非圧縮 markdown で MB 級に膨れ得る。**取り込み前のファイルサイズ上限** と **抽出後テキスト長上限** の 2 段で防御しないと SQLite row size (`PRAGMA max_page_count` / `PRAGMA page_size`) や agent context window (Claude 200K / 1M tokens) を破壊する
3. **抽出失敗時の semantics** — 壊れた `.docx` / パスワード保護 `.xlsx` / mac リソースフォーク混入 `.pptx` 等で markitdown が例外を投げる場合、FS scanner 全体が止まると本文抽出を有効化した瞬間に Box Drive 数万 file の scan が 1 file の破損で塞き止められる。**抽出失敗を SourceObserved 発行を止めない fail-safe で処理** することが運用上必須
4. **source_type の粒度** — Phase 7-9 で確立した `source_type` discriminator (`slack_message` / `box_event` / `box_drive_file` 等、ADR-0010 §責務 2) は **形式横断 1 タイプ (`office_document`)** か **形式別 3 タイプ (`word_document` / `excel_spreadsheet` / `powerpoint_slide_deck`)** かの選択。前者は projection / recall の query 表面が単純になる一方、後者は「Excel だけ recall」「PPT スライドだけ daily-brief 対象」等の operator ユースケースを backward-compat に表現できる
5. **Excel 抽出の範囲** — 数百シート × 数万 cell の workbook を全 cell 抽出すると markitdown 出力が MB 級になる。**シートあたり cells 上限 / workbook 全体 cells 上限** を 2 段で挟む防御層が必要
6. **PowerPoint 抽出の範囲** — 各 slide には本文と speaker notes が併存し、speaker notes は agent reply-draft / meeting-prep にとって本文と同等以上の context source となる。一方で画像 OCR (slide 内画像のテキスト抽出) は **重い (tesseract 依存)** + **品質が安定しない** + **opshub の cold-start guard を破壊する**
7. **ADR-0019 §不変条件 (b) `open()` ban との緊張** — ADR-0019 は box_drive scanner が `open()` / `read_bytes()` / magic bytes を呼ばないことを test invariant として pin している。本文抽出は必ず file content を読むため、この invariant と直接矛盾する。**明示的 extractor lib 経路** に限定して例外を切らないと、本 ADR の意義 (本文抽出) と ADR-0019 の意義 (CldAPI hydration 抑止 / IT policy 順守) が両立しない

本 ADR は上記 7 論点に 1 ADR で結論を与え、後続の `core/document_extract.py` 実装 (Phase 11 Sub-issue F2、#235) / box_drive scanner 改修 (F4-a、#237) / onedrive_drive 新設 (F4-b、#237) が依拠できる契約を確立する。

## Decision

OpsHub に **Office Document Content Extraction Layer** を追加し、`src/opshub/core/document_extract.py` を 1 module として `[office]` extras に閉じて配置する。以下 9 つの決定を pin する。

### (a) 抽出ライブラリ = **markitdown** 1 本

Microsoft 公式の Python ライブラリ [`microsoft/markitdown`](https://github.com/microsoft/markitdown) を採用する。pyproject.toml に `[project.optional-dependencies]` extras として `office = ["markitdown>=0.1"]` を追加し、**`[office]` extras なしで opshub をインストールした場合、markitdown 関連の import は cold-start 経路に登場しない**。

採用理由:

- **多形式統一 API** — Word `.docx` / Excel `.xlsx` / PowerPoint `.pptx` / PDF / 画像 / 音声まで markitdown が 1 API で markdown 化する。`core/document_extract.py` の caller は形式を意識せず `extract(path) -> ExtractResult` 1 経路で本文を取得できる
- **markdown 出力** — agent context (LLM input) / recall (FTS5 / embedding) のいずれも markdown を natively 扱える。HTML / plain text にすると table / list 構造が劣化し、再パースの実装コストが発生する
- **Microsoft 公式** — Office Open XML 仕様への追従が保証され、長期メンテナンスを期待できる (`python-docx` / `openpyxl` / `python-pptx` を自前連結する選択肢は §Alternatives #1 で却下)
- **cold-start guard 順守** — markitdown は `[office]` extras に閉じ込め、`opshub --help` ≤ 300ms の M6 guard を維持する (`core/document_extract.py` 内の `import markitdown` は関数内 deferred import で M6 whitelist 違反を回避)

### (b) ファイルサイズ上限 = **50 MB** / 抽出後テキスト長上限 = **500K chars**

抽出を 2 段で防御する:

- **(b-1) ファイルサイズ上限**: 取り込み対象ファイルの `os.stat().st_size` が **50 MB (50 × 1024 × 1024 bytes)** を超える場合は抽出を **skip** し、`body=None` + warning log で `SourceObserved` を発行する
- **(b-2) 抽出後テキスト長上限**: markitdown 出力 (markdown 文字列) が **500K chars** を超える場合は head-truncation + 末尾に `\n\n[truncated: original=<N> chars, limit=500000]` 注記を追記して `body` に格納する

両上限は `opshub.toml` で operator が上書き可能:

```toml
[office]
max_file_size_mb = 50          # default 50; 0 = unlimited (非推奨)
max_extracted_chars = 500000   # default 500_000; 0 = unlimited (非推奨)
```

採用理由:

- **50 MB** — Box Drive / OneDrive Desktop に存在し得る通常の Office 文書を概ね covers (社内文書は中央値で 1-5 MB)。100 MB 超は scan / inbox の throughput を顕著に劣化させる経験則を Phase 9 box_drive 運用から outlook
- **500K chars** — Claude / GPT-4o の 200K-1M token window のうち、安全マージンを取って単一 source が context の 1/4 を超えないラインに設定 (1 token ≒ 4 chars 仮定)。embedding 側は head-truncation で先頭から十分な意味情報が取れる (ADR-0012 改訂版 §4 head-truncation 戦略と整合)
- **2 段防御** — ファイルサイズだけだと「100 sheets × 1KB cell」の中サイズ workbook がテキストでは 5MB 級に膨れるケースを取りこぼす。抽出後テキスト長で再防御する
- **operator override** — 個人 / 部門ごとに「Excel 大きめ容認」「PPT スライド全文必須」等の需要差を `opshub.toml` で吸収

### (c) 抽出失敗 fail-safe = **warning log + `body=None`** で SourceObserved 発行継続

markitdown が例外を投げる (ファイル破損 / パスワード保護 / 未対応サブ形式 / OOM 等) 場合、`core/document_extract.py` は例外を catch し:

1. `structlog.warning` で `event="document_extract.failed"`, `path=<rel_path>`, `reason=<exception class + sanitised message>` をログ出力
2. `ExtractResult(body=None, truncated=False, extraction_skipped=True, skip_reason=<short_tag>)` を caller に返す
3. caller (mapper) は `body=None` のまま `SourceObserved` を append する (metadata だけは取り込まれた状態)
4. `SourceObserved.summary` に `"[extraction skipped: <reason>]"` を含める (200 char cap 内、ADR-0005 互換)

採用理由:

- **scan が止まらない** — 1 file の破損で Box Drive 数万 file の scan が塞き止められる経路を構造的に防ぐ
- **observability** — warning log で operator は「N 件抽出失敗、内訳は…」を後追い可能。`opshub source list --extraction-skipped` 系 CLI は Phase 11.x 候補
- **metadata は保たれる** — file 存在 / size / mtime / path 等の metadata は `SourceObserved` に乗るため、後で operator が手で抽出再試行 (extractor 改善後) する経路が残る
- **token / PII を log に漏らさない** — `sanitise_error_message` (Phase 1 確立、ADR-0014 系) を通して exception message を redact してからログ出力

### (d) source_type = **形式別 3 種** (`word_document` / `excel_spreadsheet` / `powerpoint_slide_deck`)

Office 文書を取り込む際の `source_type` discriminator を 3 種類に分割する:

| Extension | source_type | 想定用途例 |
|---|---|---|
| `.docx` | `word_document` | 仕様書 / 議事録 / 提案書 |
| `.xlsx` | `excel_spreadsheet` | 集計 / 一覧 / トラッキング表 |
| `.pptx` | `powerpoint_slide_deck` | プレゼン資料 / 講演資料 |

旧形式 (`.doc` / `.xls` / `.ppt`) は markitdown が変換可能な範囲で同 source_type にマッピング。markitdown が未対応の形式は `[extraction skipped: unsupported_format]` で fail-safe (§決定 (c))。

採用理由:

- **operator UX** — `opshub source list --type excel_spreadsheet` で Excel だけ拾う、`opshub recall --type powerpoint_slide_deck` でプレゼン資料に絞った semantic search 等、形式ごとの操作粒度が成立
- **agent skill 設計の余地** — Phase 12+ で meeting-prep skill が PPT を優先 weight する、reply-draft skill が Word 議事録に高 affinity を割り当てる等、source_type discriminator で粒度を表現できる
- **`source_type` 自由文字列 (max 50 chars)** — `domain/events/source.py` の `SourceObserved.source_type` は free-form string (Literal 制約なし) で運用されているため、enum migration を伴わない 3 種追加が可能 (F2 で `Literal` を導入する場合は同時に登録)

### (e) Excel 抽出範囲 = **全シート** + シートあたり 10K cells / workbook あたり 50K cells 上限

Excel は全シートを抽出対象とするが、以下 2 段の cells 上限で防御:

- **(e-1) シートあたり 10K cells** — 1 シート内の non-empty cells が 10K (1万) を超えた場合、超過分を truncate し `[truncated: sheet "<name>" had <N> cells, kept first 10000]` 注記をシート末尾に追加
- **(e-2) workbook あたり 50K cells** — 全シート合計 cells が 50K を超えた場合、シート単位で残り cells を順次 skip し `[truncated: workbook total cells <N> exceeded 50000 limit]` を末尾に追加

両上限は §(b) と同様 `opshub.toml` で上書き可能:

```toml
[office.excel]
max_cells_per_sheet = 10000  # default 10000
max_cells_per_workbook = 50000  # default 50000
```

採用理由:

- **全シート対象** — 多くの社内 Excel は「Sheet1 = ダッシュボード / Sheet2-N = 詳細」構造で、Sheet1 だけだと recall の網羅性が劣化する
- **10K + 50K cells** — 50K cells ≒ 250 行 × 200 列 ≒ 通常業務 Excel の上位 95%ile を covers。これを超える Excel (1M cells 級の data dump) は recall 対象として有用性が低く、防御層で truncate して運用可能
- **truncate 注記** — operator / agent が「ここで truncated されている」事実を `body` 内テキストから読み取れるようにする (sanitised state を hidden にしない)

### (f) PowerPoint 抽出範囲 = **本文 + ノート**両方含む / 画像 OCR は Phase 12+ defer

PPT 抽出は以下の方針:

- **(f-1) 本文 (slide content)** — 各 slide の title / body text / table / list を markitdown 経由で markdown 化
- **(f-2) speaker notes** — 各 slide の speaker notes も markitdown 経由で抽出し、slide 本文の直後に `> [speaker notes]\n\n<note text>` blockquote 形式で append
- **(f-3) 画像 OCR** — slide 内画像のテキスト抽出 (tesseract / pytesseract) は **Phase 11 では実装しない**、Phase 12+ で視覚抽出レイヤとして独立 ADR で再評価

採用理由:

- **speaker notes は agent にとって本文と同等以上** — reply-draft / meeting-prep skill では「発表者が何を伝えたかったか」が speaker notes に書かれていることが多く、本文 + notes 両方が取り込まれていないと context が劣化する
- **blockquote 形式** — markdown structure 内で「ここは notes」と明示することで、後段 agent prompt が `<context_source>` 内で notes 部分を分離して扱える
- **画像 OCR defer 理由** — tesseract 依存は OS-specific binary install を opshub に持ち込み、ADR-0001 配布制約 (`uv tool install opshub`) を破る。`pytesseract` 経由でも cold-start 経路に重い C extension が現れ M6 guard 違反リスク。Phase 12+ で画像 OCR の本格採用時には `[ocr]` extras + 専用 ADR で再評価する
- **markitdown が一括対応** — markitdown は `.pptx` 内 slide + notes を `convert()` 1 呼び出しで markdown 化する経路を持つ (バージョンにより挙動差あり、F2 実装時に確認)

### (g) ADR-0019 §不変条件 (b) `open()` ban との緊張解消

ADR-0019 (Local-FS-backed Connector) は §決定 (b) で box_drive scanner が `open()` / `read_bytes()` / `read_text()` / magic bytes を呼ばないことを test invariant として pin している。本文抽出は file content を必ず読むため、この invariant と矛盾する。

本 ADR は ADR-0019 を **改訂** し (本 PR で同時改訂)、§決定 (b) に **opt-in 例外節**を追加する。要点:

- **既定挙動は変えない** — `[connectors.<name>] content_extraction = false` (default false) の状態では §不変条件 (b) は変わらず維持 (scanner は `stat()` のみ、`open()` 禁止)
- **opt-in で例外** — `[connectors.<name>] content_extraction = true` を operator が明示設定した場合に限り、`core/document_extract.extract(path)` 経路 (= markitdown 経由) **のみ** open 許可
- **diff path は不変** — `f"{size}:{mtime_ns}"` fingerprint 計算は stat() のみで完結する経路を維持 (本 ADR で変更しない)。`open()` 許可は extraction stage 限定で、scan diff 判定 stage には及ばない
- **CldAPI / FSE hydration 抑制ガイドライン継続** — content_extraction = true の場合でも、`content_extraction` が `true` のファイルに対する単発抽出 (= 1 file 1 open) のみが許され、scan walk の中で magic bytes / shebang / SHA-256 全 file hash 等の全 file 走査型 open は依然禁止

採用理由:

- **構造的境界の保全** — 「open ban を解除する」ではなく「`core/document_extract.extract(path)` 経路に限定して open 許可」と境界を絞ることで、ADR-0019 が防いでいた IT policy 違反 / Box Drive cache 肥大化 / OS notification 暴発を最小化したまま本文抽出を導入できる
- **opt-in default false** — Phase 9 で box_drive を運用してきた operator は Phase 11 アップグレードでも挙動が変わらない (`content_extraction = true` を明示設定するまで stat() のみ contract が保たれる)
- **test invariant の継承** — ADR-0019 改訂後も `test_scanner_never_opens_files` は `content_extraction = false` の経路で継続 (本 ADR §決定 (g) と整合)。`content_extraction = true` 経路は別の test (`test_scanner_opens_only_via_extractor`) で「open は `core.document_extract.extract` 経由のみ」を pin する (F2/F4 実装時に追加)

### (h) provenance / 暗号化との整合

抽出された body は ADR-0020 (Full Local Content Retention) / ADR-0021 (Encryption at Rest) と整合する:

- **provenance**: `provenance_origin="external"` / `provenance_trust="untrusted"` を `SourceObserved` に付与 (Office 文書は外部由来テキストとして prompt injection 防御層に乗せる、ADR-0020 §(e))
- **暗号化**: `sources.body` が SQLCipher 暗号化対象 DB に書かれることで、抽出された Office 本文も保存時暗号化される (ADR-0021 §(a))
- **excludes**: ADR-0019 §決定 (g) の `exclude_globs` で機密 Office 文書を operator が事前除外可能 (例 `secrets/*.xlsx`)

採用理由:

- 本 ADR は ADR-0020 / 0021 / 0019 の3層 (provenance / 暗号化 / excludes) に乗ることで「外部本文取り込み」の安全弁を二重化する。本 ADR 独自に追加防御層は持たない (重複防御は複雑化と test 表面の肥大化を招くため)

### (i) 抽出キャッシュなし (Phase 11 MVP、Phase 11.x 候補)

Phase 11 MVP では 抽出結果を **キャッシュしない**。同一 fingerprint (`f"{size}:{mtime_ns}"`) で `SourceObserved` が skip される (ADR-0019 §決定 (d)) ため、再 scan で同じ file が抽出され直すことはない。`projections rebuild` 時のみ全 file の再抽出が発生する。

Phase 11.x 候補:

- **抽出キャッシュ table** (`document_extracts` projection、`fingerprint -> body`) を新設して rebuild 高速化
- **incremental rebuild** で fingerprint 変更分のみ再抽出

採用理由:

- Phase 11 MVP の主目的は「抽出経路を持つ」こと自体で、rebuild 性能は secondary
- キャッシュ層を MVP に含めると schema migration が増え、本 PR の scope (ADR のみ 1 PR) で吸収不能
- rebuild は operator が明示的に走らせる稀イベントで、初期は 100k files で 数十分かかっても許容

## Consequences

### Positive

1. **Office 文書が秘書 context の一級市民になる** — Word/Excel/PPT の本文が `sources.body` に乗ることで Phase 4 semantic recall / Phase 5 brief / Phase 6 propose (含む reply_draft) / Phase 8 link traversal が Office 由来の context を automatic に活用できる
2. **markitdown 1 本で形式分岐を吸収** — `core/document_extract.py` の API は `extract(path) -> ExtractResult` の 1 経路に統一され、caller (FS scanner / mapper) は形式を意識しない
3. **fail-safe で scan が止まらない** — 1 file の破損が Box Drive 数万 file の scan を塞き止めない (§決定 (c))
4. **既存 connector への影響ゼロ** — `[office]` extras + `content_extraction = false` default で Phase 9 box_drive を含む既存 connector の挙動は 1 byte たりとも変わらない (operator が明示 opt-in しない限り)
5. **形式別 source_type で operator/agent の表現力が増す** — `opshub recall --type excel_spreadsheet` 等の形式絞り込みが可能になり、Phase 12+ skill 拡張余地も拡がる
6. **cold-start guard 順守** — `[office]` extras なしの opshub install では markitdown が一切 import されず、`opshub --help` ≤ 300ms の M6 guard を維持

### Negative / Trade-offs

1. **markitdown のバージョン追従コスト** — Microsoft 公式とはいえ若いライブラリで、breaking change リスクは中規模。緩和: `markitdown>=0.1` の SemVer 緩い pin と CI integration test で抑止
2. **50 MB / 500K chars 上限のしきい値判断** — 上限以下でも上限超過でも完璧な閾値は存在しない。緩和: `opshub.toml` operator override で個別調整可能 (§決定 (b))
3. **fail-safe で「抽出されなかった事実」が見えにくい** — warning log だけだと operator が後で「N 件失敗していた」を能動的に確認するコスト。緩和: Phase 11.x で `opshub source list --extraction-skipped` CLI を予定 (本 ADR §決定 (c) consequences)
4. **画像 OCR 欠如** — slide 内画像 / Excel 内 chart / Word 内図表のテキストは抽出されない。緩和: Phase 12+ で `[ocr]` extras + 専用 ADR (§決定 (f-3))
5. **ADR-0019 invariant 緩和** — `open()` ban が opt-in 例外で穴を開く。緩和: `core/document_extract.extract(path)` 経路に限定して例外を局所化 (§決定 (g))、test invariant を「open は extractor 経由のみ」に進化 (F2/F4 で pin)
6. **抽出キャッシュなしで rebuild が遅い** — `opshub projections rebuild` 時に全 Office 文書の markitdown 再呼び出しが発生。緩和: Phase 11.x で抽出キャッシュ projection (§決定 (i))
7. **Excel cells 上限の暴発リスク** — 50K cells 上限で大規模 dump Excel が truncate される。緩和: 上限 override + truncate 注記で operator / agent が「ここで切れている」を読める (§決定 (e))

## 軽減策

1. **`core/document_extract.py` の deferred import** — `def extract(path) -> ExtractResult` 内で `import markitdown` を遅延実行し、`[office]` extras なし環境では M6 cold-start guard を破らない
2. **`extras_required` actionable error** — `[office]` extras 未インストールで `content_extraction = true` を設定した場合、`opshub connector sync` 開始時に `ConfigError("content_extraction = true requires [office] extras; install with 'uv pip install opshub[office]'")` を fail-fast で raise
3. **markitdown CI matrix** — Phase 11 F2 で `tests/integration/test_phase11_office_extract.py` を CI に常駐、3 形式 × 正常 / 失敗 / 上限超過 / 暗号化対象 file 等で markitdown の挙動を持続検証
4. **`opshub.toml` override の validation** — `max_file_size_mb < 0` / `max_extracted_chars < 0` 等の不正値は `ConfigError` で fail-fast、`0` (= unlimited) は許容するが warning log で「上限解除は非推奨」を伝える
5. **抽出失敗の event log 漏らし禁止** — exception message の sanitisation を Phase 1 確立の `core/sanitise.sanitise_error_message` で必ず通し、token / PII / 個人情報を log に漏らさない

## Alternatives Considered

### 1. `python-docx` + `openpyxl` + `python-pptx` を自前連結

形式ごとに個別ライブラリを直接 import し、`core/document_extract.py` 内で形式分岐する案。

却下理由:

- 3 ライブラリ × 各々独自 API で `core/document_extract.py` の実装が肥大化 (3 module + dispatcher が必要)
- 出力形式が ライブラリごとに違い (`python-docx` は `Document` object、`openpyxl` は `Workbook` object、`python-pptx` は `Presentation` object) markdown 化のための変換層を opshub 内で書く必要がある (markitdown が解決済みの問題を再実装)
- 各ライブラリの仕様追従コスト (Office Open XML 仕様変化に 3 ライブラリで追随) を opshub が背負う
- Microsoft 公式の markitdown が同等カバレッジを 1 API で提供する以上、自前連結は明確な over-engineering

### 2. `unstructured.io` (`unstructured` library) 経由で多形式抽出

`unstructured` は OSS の document extraction library で、Office / PDF / HTML / 画像を 1 API で抽出する。LLM RAG ecosystem で広く採用されている。

却下理由:

- **依存が重い** — `unstructured` は `nltk` + `pillow` + `pdfplumber` + 多数の transitive deps を引き、`[office]` extras 単独でも 100MB 超のインストールサイズが見込まれる
- **OCR 依存が default on** — `unstructured` は内部で OCR (tesseract) を呼ぶ経路を持ち、`[office]` extras に含めると ADR-0001 配布制約に抵触する経路が増える
- **出力が element list で markdown 化に追加工程要** — `unstructured` の出力は `Element` (title / list_item / table / image) のリストで、markdown 化を opshub 側で書く必要がある
- markitdown が Microsoft 公式で同等以上のカバレッジを軽量に提供する以上、`unstructured` の選定理由が乏しい

### 3. source_type を形式横断 1 タイプ (`office_document`) に統一

projection / recall query が単純になる利点がある。

却下理由:

- **operator UX が痩せる** — 「Excel だけ recall」「PPT スライドに絞った検索」等の自然な操作粒度が失われる
- **agent skill の表現力欠如** — meeting-prep skill が PPT 優先 weight を割り当てる、reply-draft skill が Word 議事録に高 affinity を持つ等の Phase 12+ 設計余地を狭める
- **`source_type` 自由文字列のため 3 種追加コストはほぼゼロ** — 3 タイプ採用のオーバーヘッドが小さく、横断 1 タイプの利点を上回らない

### 4. 抽出失敗時に SourceObserved を発行しない (fail-fast skip)

抽出に失敗した file は SourceObserved を一切発行しない (取り込みから除外する) 案。

却下理由:

- **metadata 取り込みも諦めることになる** — file 存在 / size / mtime / path は valid な情報で、抽出失敗で全て捨てるのは過剰
- **後追い再試行ができない** — 抽出 lib 改善後に operator が手で再抽出したい場合の起点 (= source row) が存在しない
- **inbox に「抽出失敗した file」が出ない** — operator が「この file が取り込めなかった」を inbox で気づけない
- §決定 (c) の `body=None` + warning log + summary 注記の経路の方が情報を失わない

### 5. 抽出キャッシュ table を Phase 11 MVP に含める

`document_extracts` projection (`fingerprint -> body`) を MVP で導入し、`projections rebuild` 高速化を初期から提供する案。

却下理由:

- MVP scope が肥大化 (新 projection + migration + projector + test fixture)
- Phase 11 MVP の主目的は「抽出経路を持つ」ことで、rebuild 性能は secondary
- Phase 11.x で incremental rebuild 設計と同時に持ち込むほうが design coherence が高い (§決定 (i))
- 100k files で初回 rebuild が数十分かかっても、operator は稀イベントとして許容可能

### 6. ファイルサイズ上限を 100 MB / 抽出後テキスト 1M chars に緩和

operator override 不要で「ほぼすべての Office 文書」を取り込めるラインに設定する案。

却下理由:

- **default を緩めると agent context が劣化する** — 1 source が 1M chars (≒ 250K tokens) を占有すると 200K token context window では収まらず、recall 全体が破綻
- **embedding cost が膨張** — 100MB Excel の全 cell embed は OpenAI / Voyage API 経由で operator に予期しないコスト発生
- 50 MB / 500K chars は中央値の業務文書を covers し、超過は operator override で例外対応するほうが「安全 default + 自由な escape hatch」設計と整合

## Validation

本 ADR の決定 (a)-(i) は Phase 11 sub-issue F2 (#235) の実装で test pin される。本 ADR 起票時点 (Phase 11 sub-issue F1、本 PR) では実装はまだ存在しない。F2 完了後に本セクションを Phase 9 ADR-0019 同様に「実テストファイル名を列挙する」形で update する予定 (Phase 9 ADR-0019 §Validation と同 pattern)。

予定する pin 経路 (F2 で確定):

- (a) markitdown 採用 + deferred import — `tests/unit/core/test_document_extract.py::test_markitdown_deferred_import` (cold-start で markitdown が import されないこと)
- (b) 50MB / 500K chars 上限 — `tests/unit/core/test_document_extract.py::test_file_size_limit` / `test_extracted_text_limit` + `opshub.toml` override test
- (c) fail-safe — `tests/unit/core/test_document_extract.py::test_extraction_failure_returns_body_none` + warning log assertion
- (d) source_type 3 種 — `tests/unit/connectors/box_drive/test_mapper.py::test_office_extension_dispatches_correct_source_type` (.docx → word_document 等)
- (e) Excel cells 上限 — `tests/unit/core/test_document_extract.py::test_excel_cells_per_sheet_limit` / `test_excel_cells_per_workbook_limit`
- (f) PPT notes 含有 — `tests/unit/core/test_document_extract.py::test_pptx_includes_speaker_notes`
- (g) ADR-0019 invariant 共存 — `tests/unit/connectors/box_drive/test_scanner.py::test_scanner_opens_only_via_extractor` (content_extraction=true 経路の open は extract() 経由のみ) + `test_scanner_never_opens_files` を `content_extraction=false` 経路で継続
- (h) provenance / 暗号化 — `tests/integration/test_phase11_office_extract.py` で `provenance_origin="external"` / `provenance_trust="untrusted"` + SQLCipher 暗号化対象 DB に body が書かれることを e2e で確認
- (i) キャッシュなし — Phase 11 MVP には抽出キャッシュ test を **追加しない** ことが pin (test の不在自体が決定の reflection)、Phase 11.x で別 ADR / 別 PR

## 関連

- [Principles 6 (External Content Retention)](../principles.md) — Office 本文も保持対象 (本 ADR で Word/Excel/PPT を明示的に追加)
- [Principles 7 (Connector Contract)](../principles.md) — 本 ADR は ADR-0010 §責務 6 (body minimization) を Office 文書経路に延伸
- [Principles 9 (Phased Delivery)](../principles.md) — Phase 11 = MS Office 深掘り (本 ADR + ADR-0019 / ADR-0010 改訂で確定)
- [Architecture §Connector Layer](../architecture.md) — box_drive / onedrive_drive scanner の Office extension hook (F4 で実施)
- [Architecture §Office Document Extraction Layer (新規)](../architecture.md) — 本 ADR の pattern を architecture docs に節として追加 (F6 で実施)
- [ADR-0001: Python Stack](0001-python-stack.md) — `uv tool install opshub` 配布制約、`[office]` extras による cold-start guard 順守の根拠
- [ADR-0005: External Content Minimization](0005-external-content-minimization.md) — Phase 10 で ADR-0020 が supersede 済。本 ADR は ADR-0020 整合で本文取り込みを Office 文書に延伸
- [ADR-0010: Connector Contract](0010-connector-contract.md) — 本 PR で Phase 11 改訂 (Teams / 本文抽出契約 / delta + fallback / User Token principal)。本 ADR §決定 (a)-(c) は ADR-0010 §責務 6 (body minimization) の Office 文書経路への延伸
- [ADR-0012: Embedding Strategy](0012-embedding-strategy.md) — Phase 10 改訂版 §4 の `COALESCE(sources.body, sources.summary)` embed で Office 本文も自動的に embedding 対象
- [ADR-0019: Local-FS-backed Connector](0019-local-filesystem-backed-connector.md) — 本 PR で同時改訂 (`content_extraction = true` opt-in 例外節 + onedrive_drive 汎化)。本 ADR §決定 (g) で緊張解消
- [ADR-0020: Full Local Content Retention](0020-full-local-content-retention.md) — 本文ローカル保持の根拠、provenance タグ (`origin` / `trust`) の運用
- [ADR-0021: Encryption at Rest](0021-encryption-at-rest.md) — 抽出後 body は SQLCipher 暗号化対象 DB に書かれることで保存時保護
- [ADR-0022: MCP Server Surface](0022-mcp-server-surface.md) — Office 本文も `recall.search` / `source.get` 経由で agent context に流れる際の policy-as-data (annotation) 規律を継承
- [Phase 11 Plan §1 確定済み事項 + §2 ADR 構成 + §3 Sub-issue F1](../phase-11-plan.md)
- [Phase 11 epic #233 / Sub-issue F1 #234](https://github.com/ozzy-labs/opshub/issues/234)
- [microsoft/markitdown (GitHub)](https://github.com/microsoft/markitdown) — 採用ライブラリの公式リポジトリ
