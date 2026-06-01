# 0028. FTS5 sources_fts tokenizer choice (trigram + short-query LIKE fallback)

- Status: Accepted
- Date: 2026-06-02
- Deciders: opshub maintainers

## Context

Phase 10 で導入した `opshub search` (migration 0019、FTS5 仮想テーブル `sources_fts` + 3 trigger) が、日本語入力で実質ヒットしない問題が operator 実証で確定した。原因は `sources_fts` の tokenizer 設定 `unicode61 remove_diacritics 2` (`src/opshub/db/migrations/versions/0019_create_sources_fts.py:90` 周辺) が**日本語の形態素分割も n-gram 分割もしない**こと。区切り (空白 / 記号 / 英数 ↔ 日本語境界) がない日本語自然文は本文全体を一連の単一トークンに固めるため、入力文字列がそのトークンの **完全な接頭辞〜全体** と一致しない限り FTS5 MATCH が当たらない。

### 観測された事例 (operator 実証、2026-06-02)

| クエリ | 結果 | 原因 |
|---|---|---|
| `opshub search "boxの権限" --connector slack` | 0 hits | 本文 = `boxの権限きれてそうなのですが...`、`boxの権限` 単独ではトークン全体と一致しない |
| `opshub search "boxの権限*" --raw --connector slack` | hits | prefix 演算子で前方一致するため当たる |
| `opshub search "進捗記入"` | 0 hits | 同上 (本文中の長いトークンの中間部分) |
| `opshub search "CDKの"` | 0 hits | 同上 |
| `opshub search "依頼"` | 0 hits | 同上 (2 文字短クエリ) |
| `opshub search "DailyMeeting"` | hits | 英数連続 = unicode61 で 1 トークン化し、トークン全体と一致 |
| `opshub search "権限"` | hits | 本文中で「権限」が独立トークン (空白 or 記号で区切られている) になっていれば当たる |
| `opshub search "box 権限"` | hits | 空白で 2 token に分割され、それぞれが本文中の独立トークンと一致 |

通常モード (`--raw` なし) では `SearchService._phrase_quote` (`src/opshub/services/search_service.py:237-249`) が入力を `"..."` で囲み FTS5 の literal phrase として MATCH するため、unicode61 が固める長い日本語トークンに対しては **「完全一致 or 前方一致 (prefix `*` 明示時)」しか動作しない**。これは operator の直感 (部分一致 = 当たる) と乖離する。

設計上の制約:

- **immutable migration 規範 (Phase 1 以降)**: migration 0019 を遡及書き換えしない。tokenizer 張り替えは新 migration 0028 で supersede する。
- **MCP `search` tool 契約 (ADR-0022)**: `raw_query` は host 側で hard-coded `false`。tool 引数表面を変えず、SearchService 内部処理で問題を解決する必要がある。
- **ADR-0020 (Full Local Content Retention)**: `sources.body` は本文を保持する正規ストア。FTS5 はあくまで派生 index で、`body` 側の改変は不要。
- **ADR-0001 cold-start**: `opshub --help` ≤ 300ms 予算。tokenizer 張り替えで新規依存を追加しない (`trigram` は SQLite 3.34+ の built-in)。
- **operator workaround は受け入れない**: 現状の `"...*" --raw` 経路は operator に FTS5 文法理解を要求するため non-raw mode のデフォルト体験として不適切。

## Decision

### (a) tokenizer を `trigram` に張り替え

`sources_fts` の tokenizer を FTS5 built-in `trigram` tokenizer に張り替える (SQLite 3.34+、外部依存ゼロ)。trigram は本文を 3 文字 substring に分解した index を持つため、3 文字以上の入力に対して substring match (部分一致) が自然に動く。日本語自然文の通常モードでも operator の直感 (「boxの権限」入力 → 「boxの権限きれてそう」hit) どおりに動作する。

物理的な張り替えは新 migration 0028 で行う:

- upgrade: drop trigger 3 → drop `sources_fts` → recreate with `tokenize='trigram'` → back-fill from `sources.body` → recreate 3 triggers (内容は migration 0019 と同型)
- downgrade: drop trigger 3 → drop `sources_fts` → recreate with `tokenize='unicode61 remove_diacritics 2'` → back-fill → recreate 3 triggers

migration 0019 (immutable) は touch しない。本 ADR は migration 0019 docstring 内に閉じていた tokenizer 選定根拠を独立 ADR に昇格させる。

### (b) 1-2 文字短クエリは `body LIKE` fallback

`trigram` tokenizer は **3 文字未満をインデックスしない** ため、「PR」「依頼」「Q4」等の 2 文字短クエリは 3 文字 substring を持たず 0 hit になる。これは operator UX として不適切なので、SearchService 側で短クエリ判定を入れて `body LIKE '%q%' ESCAPE '\\'` 経路にルーティングする。

- 閾値: 空白 strip 後の長さ ≤2 文字 (3 文字境界の扱いは S3 着手時にベンチで確定、本 ADR では 1-2 文字を MVP 確定値とする)
- ranking: BM25 計算不可のため固定値 (例: 1.0 で統一)、または `length(body)` 逆数等の単純 ranking
- escape: LIKE 用 escape (`%` / `_` / `\\`) を適用
- `connector_name` filter / `limit` / ORDER BY は適用 (短クエリでも filter は効かせる)
- `--raw` モード (raw_query=True) では fallback **無し** (operator が FTS5 文法を直接書く前提)

長クエリ (≥3 文字) は trigram 化後の FTS5 MATCH で自然に substring match が動くため、従来経路 (`_phrase_quote` + MATCH) を維持する。fallback は短クエリ専用 (長クエリで常時併用すると BM25 ranking が無効化されるため)。

### (c) dual index (unicode61 + trigram) は不採用

trigram 単独で英語 / コード token も大半は動くため、unicode61 と trigram の 2 本立て (dual index) は採用しない。ストレージ 2 倍 + trigger 倍化 + クエリ二重発行が opshub 規模 (operator 1 名前提) で overkill。BM25 ranking は英語 token で若干劣化する (trigram で「DailyMeeting」が `Dai`/`ail`/`ily`/... に分解されて IDF が変わる) が、operator 体験として受容範囲内 (hit/miss が反転する性質ではない)。

### (d) `--raw` フラグは維持

trigram tokenizer 化後も operator が FTS5 boolean / prefix を直接書きたいケース (例: `box* AND 権限*`) は残るため、`opshub search ... --raw` フラグは維持する。default = non-raw + trigram で大半が解決し、power user は `--raw` で full control。MCP `search` tool は ADR-0022 で `raw_query` を hard-coded `false` にしているためツール契約は不変。

### (e) `opshub projections rebuild` での自動 rebuild

migration 0028 で 1 回 back-fill する以外に、`opshub projections rebuild` (ADR-0022) が `sources` を作り直すケースでは `sources_fts` も再 build する。専用 CLI (`opshub search rebuild-index` 等) は切らない (実装増分を抑える方針、必要が顕在化したら別 issue で切り出す)。

## Consequences

### Positive

- operator が日本語自然文を default mode で部分一致 query できるようになる (旧 `"...*" --raw` workaround 不要)。秘書 use case の探索体験が大幅改善。
- find-document / `search` MCP tool 経由の skill (`personal-brief` / `next-actions` / `meeting-prep` / `research` 等) も同じ恩恵を受ける (SearchService 1 箇所改修で全 host LLM が改善を享受)。
- migration 0019 の immutable 規範を維持しつつ supersede (新 migration 0028) で物理張り替え。alembic round-trip (upgrade → downgrade → upgrade) で元 tokenizer に戻せるため operator も安全に試せる。
- 新規依存ゼロ (`trigram` は SQLite 3.34+ の built-in)。cold-start guard / `opshub --help` ≤ 300ms に影響なし。
- MCP `search` tool 契約 (ADR-0022) は不変。host (Claude Code / Codex / Copilot CLI) 側の修正不要。

### Negative / Trade-offs

- **index size が約 3 倍**に増える (trigram は本文を 3 文字 substring に分解するため、unicode61 比でストレージ消費が増える)。operator DB が現状数 MB 規模なら誤差、数 GB 規模で目立つ可能性 (S3 ベンチで実測)。
- **英語 token の BM25 ranking が若干劣化**する (「DailyMeeting」が `Dai`/`ail`/`ily`/... に分解されて IDF が変わる)。hit/miss が反転する性質ではなく、ranking 順がやや揺れる程度。
- **1-2 文字短クエリは full scan path** (`body LIKE '%q%'`) になり、`sources` row 数が大きいと性能境界に到達する。S3 で「~1M rows で <1s」を許容ラインとし、超えたら別 issue で B-tree index 追加 or FTS5 short-query auxiliary index を評価。
- **形態素 tokenizer (Lindera / SudachiPy / MeCab) を採用していない**ため、語境界を意識した相関性の高い ranking には到達しない。trigram は「依頼」と「依頼者」を同じ trigram `依頼` で混ぜて hit させるため、operator 体験として「広く取る」方向。語境界を厳密にしたければ別 Phase で再評価 (本 ADR §Alternatives (b))。

### Neutral

- `--raw` モードは維持されるため、power user 経路は不変。
- `sources` projection の schema / 本文保存は触らないため ADR-0020 整合維持。

## Alternatives Considered

### (a) unicode61 維持 + クエリ側 auto-`*` 付与

SearchService 側で入力に `*` を自動付与 (`"boxの権限"` → `"boxの権限"*`) して prefix 検索にする案。

却下理由: 「boxの権限」は本文中の長いトークンの**中間**に位置するため prefix match では当たらない (`boxの権限きれてそう` というトークンの prefix は `box` から始まる must match)。「boxの権限」を入力した operator が hit を得るためには prefix ではなく substring 機能が要る。auto-`*` では根本解決にならない。

### (b) 形態素 tokenizer 採用 (Lindera / SudachiPy / MeCab)

日本語の語境界を辞書ベースで判定する tokenizer (Lindera = Rust / 純粋 build、SudachiPy = Python、MeCab = C 拡張) を sources_fts に導入する案。

却下理由: 本 Phase では overkill。(i) いずれも辞書同梱で install size が数十〜数百 MB 増 (Lindera UniDic で 130MB+)、(ii) SQLite FTS5 custom tokenizer は C 拡張前提で apsw 等への移行が要る (sqlite3 標準 module では tokenize hook を Python から登録できない)、(iii) trigram で実用上 operator の現課題 (日本語部分一致 0-hit) は解消する見込み。trigram でも足りない operator 体験が確認できた場合に別 Phase で再評価 (ADR-0028 改訂 + 形態素 tokenizer 評価)。本 ADR §Consequences でも trade-off (語境界を意識した ranking には到達しない) として明示。

### (c) dual index (unicode61 + trigram)

`sources_fts_uni` (unicode61) + `sources_fts_tri` (trigram) の 2 本立てを持ち、クエリ層でマージする案。

却下理由: ストレージ 2 倍 + trigger 倍化 (insert / update / delete で 2 index に書き込み) + クエリ二重発行 + ranking マージ logic が必要で、opshub 規模 (operator 1 名前提) で overkill。trigram 単独で英語 / コード token もカバーできるため、unicode61 を残す価値が小さい。BM25 ranking の劣化は trade-off として受容する。

### (d) 全 query 常時 LIKE (`body LIKE '%q%'`)

FTS5 を使わず全 query を `body LIKE '%q%'` で処理する案。

却下理由: BM25 ranking が完全に失われ、`sources` row 数増加に対してスケールしない (full scan が常時発生)。FTS5 + trigram で index 検索が効く長クエリ経路を捨てる理由がない。短クエリ専用 fallback として LIKE を限定使用する本 ADR §決定 (b) のスタンスを取る。

## 関連

- [ADR-0001 Python Stack](0001-python-stack.md) — cold-start 予算。新規依存ゼロを担保。
- [ADR-0020 Full Local Content Retention](0020-full-local-content-retention.md) — `sources.body` を本文の正規ストアとして保持。FTS5 はあくまで派生 index。
- [ADR-0022 MCP Server Surface](0022-mcp-server-surface.md) — `search` tool 契約は不変 (`raw_query` hard-coded false)。
- 元 Phase: Phase 10 #210 (FTS5 sources_fts 初期実装、migration 0019)
- 本 Phase: Phase 15 epic #338 / [docs/phase-15-plan.md](../phase-15-plan.md) (SSOT)
- migration: 0019 (Phase 10、本 Phase で supersede) → 0028 (本 Phase 新規、S2)
- 参照: SQLite FTS5 trigram tokenizer — <https://www.sqlite.org/fts5.html#the_trigram_tokenizer>
