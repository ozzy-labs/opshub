# 0045. 言語選定の再評価とゼロベース参照アーキテクチャ

- Status: Accepted (言語は Python 維持を再確認 / ゼロベース最適 = TypeScript を参照記録 / 改修・移行の実装は別 Phase に defer)
- Date: 2026-06-14
- Deciders: opshub maintainers
- Reassesses: [ADR-0001](0001-python-stack.md) (Python Stack — 4 本柱を現実の重みで再採点する。**Python 採用の決定自体は覆さない**が、§配布チャネル表 / §Open Questions / 採用根拠の重み付けを本 ADR が更新する)
- Amends: [ADR-0012](0012-embedding-strategy.md) (Embedding Strategy — embedding を「foundational base」から「optional enhancement」へ posture 変更 + Ollama サイドカー backend を追加する方針), [ADR-0001](0001-python-stack.md) §配布チャネル (npm veneer / 単一バイナリ / Docker 全部入り / MCP registry を追記、Homebrew・PyInstaller core-only を drop)
- Realized by: [ADR-0046](0046-suasor-typescript-successor-product.md) (本 ADR のゼロベース TS 結論を、opshub の in-place 移行ではなく**別プロダクト Suasor の新築**として実現する。opshub は Python のまま legacy 凍結)
- Related: [ADR-0011](0011-ozzy-labs-ecosystem-adoption.md) (ozzy-labs は全 TypeScript/`@ozzylabs` — 本 ADR の「外周優位は TS」論の土台), [ADR-0002](0002-event-sourced-architecture.md) (event-sourced — projection 再構築可が Alembic 優位を薄める), [ADR-0004](0004-agent-runtime-boundary.md) / [ADR-0022](0022-mcp-server-surface.md) (MCP = エージェント境界・消費面), [ADR-0037](0037-browser-read-layer-playwright.md) / [ADR-0025](0025-office-document-content-extraction.md) (Playwright / markitdown — connector SDK 比較材料), [ADR-0044](0044-rename-opshub-to-suasor.md) (Suasor 改名 — 配布名は本 ADR の名前空間議論と連動)

## Context

opshub は [ADR-0001](0001-python-stack.md) で **Python 3.13+** を採用し、配布は PyPI 単一 (`ozzylabs-opshub`) で運用している。本 ADR は「配布戦略の再検討」から始まった一連の検討 — 配布 → npm 配布の可否 → 言語選定そのもの → ゼロベースの最適アーキテクチャ — を記録し、**現実の opshub の姿に照らして ADR-0001 の採用根拠を再採点**する。

検討時点 (Phase 24-25) の opshub の実態:

- **ローカル優先の operational memory + 秘書エージェント**。read 取り込み専用・外部に勝手に送らない・適用は HITL ([ADR-0004](0004-agent-runtime-boundary.md))
- **主たる消費面は CLI ではなく MCP**。利用者は人間ではなく agent host (Claude Code / Codex / Claude Desktop)
- **置かれている生態系は 100% TypeScript / `@ozzylabs` npm scope** ([ADR-0011](0011-ozzy-labs-ecosystem-adoption.md))。opshub だけが唯一の Python repo
- **pre-userbase**。互換シム不要、end-state を直接追える
- core は ML フリー、重い ML (torch 等) は extras に隔離 ([ADR-0001](0001-python-stack.md) の依存分離方針)

ADR-0001 は **当時いちばん難しかった技術問題 (Phase 4 semantic 層: sqlite-vec + Alembic + local embedding) に最適化**した判断だった。それは局所的に妥当だったが、**「opshub が system レベルで何になるか (MCP で消費される single-user ツールが all-TS 生態系に住む)」を過小評価**していた、というのが本再評価の出発点である。

## Decision

### (a) 言語の実務スタンス: **Python を維持** (rewrite しない)

現時点で TypeScript への移行は **行わない**。理由:

- ゼロベースの差は **僅差** ((c)(d) 参照)
- Python の負けは**すべて「外周」(配布・生態系整合)** であり、**核 (event store / projection / connector / MCP) は問題なく動いている**
- 外周の痛みは **(g) の配布チャネルで縁から吸収できる** — 核を捨てる対価に見合わない
- 24 phase 稼働した設計という**経験的検証**は、抽象的な「TS の方が良い」を上回る重みを持つ ([pre-userbase](0008-naming-opshub.md) でユーザー移行コストはゼロでも、エンジニアリングコストは実在する)

### (b) ゼロベース結論を参照として記録: **TypeScript (Bun) が最適**

現実装・改修コストを度外視してゼロから構築する場合、**TypeScript が最適**である (Go / Rust は (Alternatives) で却下)。これは ADR-0001 の Python 決定を覆すものではなく、**判断根拠を現実の重みで再採点した結果としての参照値**である。成立条件は (e) の不変条件を保つこと。

### (c) ADR-0001 の 4 本柱の再採点

ADR-0001 が Python を選んだ根拠を、現実の opshub に照らして再採点する:

| ADR-0001 の採用根拠 | 再採点 | 残るか |
|---|---|---|
| ① sqlite-vec の Python binding 成熟 | author 本人が公式 JS binding を維持し、`vec0` の DDL/KNN は言語同一。Node (`better-sqlite3` / `bun:sqlite`) でも一次対応 | ✕ 解消 |
| ② Alembic に代替なし | opshub は **event-sourced で projection を replay 再構築できる** ([ADR-0002](0002-event-sourced-architecture.md)) ため「migrate より drop+rebuild」で済み、autogenerate/branch-merge 優位の効きが落ちる | ✕ ほぼ消滅 |
| ③ local embedding (sentence-transformers) | privacy を保つ local 埋め込みの定石は **localhost の model server サイドカー (Ollama 等)** で**言語非依存**。さらに (f) で embedding 自体を optional に格下げ | ✕ 解消 |
| ④ AI 駆動開発との相性 | 唯一残るが **僅差**。しかも opshub の実ドメイン (MCP / connector / CLI / event store) は **TS も非常に強いゾーン**。Go/Rust に対する優位であって TS に対する優位ではない | △ 僅差で残る |

**技術フィットを主張した ①②③ がいずれも opshub 自身の設計 (event-sourced / サイドカー / FTS-first) で軟化し、実際にプロジェクトを運んだ ④ は TS でも同程度成立する。**

### (d) 「硬い差 / 軟らかい差」による評価

メリデメを数で数えると互角に見えるため、**差の寿命**で重み付けする:

- **硬い差** = 設計で消せない (言語/生態系の構造に根ざす)。**TS 側: MCP 中心性 (消費面と同言語) / `@ozzylabs` 整合 (組織構造) / 配布 (npm + 単一バイナリ)** — いずれも opshub の都合で動かない
- **軟らかい差** = opshub 固有事情で溶ける。**Python 側: ②③ (自設計で軟化) / ④ (僅差) / 稼働実績 (コスト由来で、(a) の "気にしない" 前提では除外される)**

→ TS の優位は硬く、Python の優位は軟らかい。この**非対称**がゼロベース判定を TS に倒す。「硬い差」と「決定的な差」は別概念であり、TS の配布優位は硬いが **(g) で迂回可能 = 非決定的**であることが (a) の維持判断を支える。

### (e) 不変条件: **opshub は in-process ML を持たない (thin orchestrator)**

(b) の TS 結論が成立する前提を**不変条件として明文化**する: opshub は ML 計算を自プロセス内で行わず、**LLM 推論 / embedding / OCR / 音声書き起こし / 形態素解析を、すべて外部 (API・ローカル model server サイドカー・ネイティブ binding) に委譲**する。

- これは ADR-0001 の「core は ML フリー」原則の延長であり、**この posture を保つ限り言語選定は ML 中立** = (b) が成立する
- 逆に opshub が **in-process ML 基盤に化けた場合** (torch を自プロセスで回す / 内製 reranker・fine-tune / コーパス上の本格データ分析)、numpy/pandas/torch の Python エコシステムが決定的になり (b) は**反転して Python が最適**になる ((h) の trigger)

### (f) retrieval posture: FTS-first / embedding は optional な Ollama サイドカー

- **FTS5 (trigram, [ADR-0028](0028-fts5-japanese-tokenizer.md)) + agent の反復駆動を default の検索経路**とする。既知アイテム検索・多くの topical 検索はこれで足りる
- **embedding は optional な enhancement** に格下げ ([ADR-0012](0012-embedding-strategy.md) reframe)。FTS が原理的に越えられない壁 — **言語跨ぎ (JA↔EN) と語彙ミスマッチ** — のためにのみ要る
- embedding backend に **`ollama` を追加**する方針 (localhost `/api/embed`、torch 不要、bge-m3 等の多言語モデル、既存 `llm-ollama` の httpx パターン再利用)。in-process torch (`local` backend) は「daemon ゼロにしたい人向けの重い選択肢」に降格
- `recall` は backend=disabled 時に **hard error をやめ FTS に graceful 劣化** (空結果 + シグナルで host が `search` に寄る)
- `vec0` は base dep のまま維持 (500KB と安価、on/off は backend 選択であって schema 変更ではない)

→ これにより **embedding が「完全に optional かつ言語非依存サイドカー」**になり、③ の Python ハードピンが消える ((c) と整合)。

### (g) 配布 posture

| チャネル | 現 (Python) | ゼロベース (TS) | 判定 |
|---|---|---|---|
| 言語レジストリ | PyPI `ozzylabs-opshub` (canonical) | **npm `@ozzylabs/opshub`** (名前空間回収・`npx` で MCP 起動、改名後は `@ozzylabs/suasor` — [ADR-0044](0044-rename-opshub-to-suasor.md)) | 維持/反転 |
| 単一バイナリ | (不可: PyInstaller core-only は extras と衝突) | **Bun `--compile` → GitHub Releases** (ランタイム前提ゼロ) | TS で開通 |
| Docker 全部入り | ML 用途で予約のみ | **opshub + Ollama 同梱** = local embedding 込みの batteries-included | 格上げ (local embedding を中核に置くなら必須) |
| MCP-native | — | MCP registry 掲載 + 一発 config snippet (DXT は init/OAuth がステートフルなため部分的) | 追加 |
| Homebrew / 純単一バイナリ(現Python) | Open Question | — | **drop** (extras と衝突・audience は既に uv/npm 持ち) |

npm は Python のままでも「名前空間 veneer (`uvx` を `npx` で薄く包む)」として出せるが、**Python 前提は消えない** (実体は PyPI)。本格的に Python 前提を消すのは TS + 単一バイナリの道。

### (h) 再評価 trigger

- **in-process ML 基盤化** ((e) 逸脱) → (b) 反転、Python が最適化される
- **multi-user / Web・モバイル surface 化** → 境界が CLI/MCP を超え、TS 移行の再評価価値が上がる
- **MCP-TS 生態系との断絶が運用実害化** (Python MCP SDK が周辺化する等) → 同上
- trigger **未発火の現状では移行しない** ([trigger 未発火なら動かさない](0008-naming-opshub.md) と同じ姿勢)

### (i) 実装の defer と、今着手できる言語非依存改修

機械的な言語移行は本 ADR では行わない。一方 **(f)(g) の一部は現 Python のまま今着手でき**、外周の痛みを縁から減らせる:

- FTS-first 化 (recall の graceful 劣化)
- `ollama` embedding backend 追加 (torch 配布痛を言語に関係なく軽減)
- 配布チャネル拡張 (npm veneer / Docker 全部入り / MCP registry)

これらは新 architectural posture を伴うため、専用の top-level Phase で切る ([新 pattern は新 Phase](0006-cli-first-mvp.md) 同様の運用)。

## Consequences

### Positive

1. **判断根拠が現実の重みで再採点・記録された** — 将来の言語/配布議論が ADR-0001 を空目で再 derive せず、本 ADR の再採点表 (c) と硬/軟分析 (d) から再開できる
2. **言語非依存の改修 (i) を現 Python のまま着手でき、外周の痛みを縁で減らせる** — rewrite なしで配布・retrieval の posture を改善
3. **不変条件 (e) が将来の言語自由度を保つ** — thin orchestrator を保つ限り、いつでも (b) の TS 結論に移れる余地が残る
4. **rewrite の誘惑に明確な trigger (h) を設定** — 「ゼロベースなら TS」という結論が、衝動的な書き直しではなく条件付き判断として固定される

### Negative / Trade-offs

1. **ゼロベース最適 (TS) と実装 (Python) が乖離したまま残る** — 「最適を知りつつ直さない」状態は一種の負債。trigger (h) で管理するが、乖離自体は解消しない
2. **不変条件 (e) は in-process ML の選択肢を縛る** — 将来「品質のためにモデルを自プロセスで回したい」需要が出たとき、(e) を破るか言語判断をやり直すかの分岐になる (embedding/reranker の品質上限を sidecar 品質に律速させる)
3. **(b) の TS 結論は前提付きで陳腐化しうる** — Bun の成熟度・MCP 生態系の言語分布・connector SDK 勢力図に依存する。trigger (h) 評価時に (b) を再 review する必要がある
4. **(f) の FTS-first は per-query コストを agent 側に寄せる** — embedding 無効時、agent の反復検索でトークン/レイテンシ/tool 往復が増える (インフラ簡素化とのトレードオフ)

## Alternatives Considered

### 1. 今すぐ TypeScript / Bun に rewrite

却下。ゼロベース最適ではあるが ((b))、差は僅差・Python の負けは外周のみ・外周は (g) で吸収可能。24 phase / 11 connector / 19 MCP tool / 15 skill の書き直しは pre-userbase でもエンジニアリングコストが過大で、機能開発を止める。

### 2. Python 据え置き (再採点も記録もしない)

却下。ADR-0001 の採用根拠が現実とズレたまま放置されると、将来の議論が同じ机上比較を再生産する。**維持の判断 (a) と、根拠の再採点 (c)(d) は両立する** — 維持するからこそ「なぜ維持か / 何が変わったか」を残す価値がある。

### 3. Go / Rust

却下 (前段の 4 言語比較より)。**Go** は配布 (単一バイナリ) と cold start で勝つが、**sum 型がなく event-sourced の discriminated union 表現が弱い** (核アーキテクチャと喧嘩する) + connector SDK が一段薄い。**Rust** は event 表現 (enum=直和) と correctness が最良だが、**AI 駆動開発速度が最遅** + **主要 SaaS の公式 Rust SDK がほぼ無く connector を手書きする量が膨大**。両者とも opshub の重み (AI 速度・MCP・connector・event 表現) で取りこぼす。

### 4. npm veneer のみ (Python のまま `@ozzylabs/opshub` 薄皮)

部分採用。`@ozzylabs` 名前空間回収と `npx` config snippet には有効だが、**Python 前提 (3.13 / extras) は消えない** (実体は PyPI)。配布 posture (g) の一構成要素として位置づけ、言語判断 (a)(b) とは独立に扱う。

### 5. embedding を完全廃止 (FTS のみ)

却下。FTS-first は採るが ((f))、**言語跨ぎ (JA↔EN) と語彙ミスマッチは FTS が原理的に越えられない壁**で、agent の反復でも代替できない。opshub は混在言語コーパス前提なので、embedding を optional enhancement として残す。
