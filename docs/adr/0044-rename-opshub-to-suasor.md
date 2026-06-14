# 0044. Rename opshub → Suasor (deferred implementation)

- Status: Superseded by [ADR-0046](0046-suasor-typescript-successor-product.md) (改名 → 別プロダクト新設に組み替え。opshub は改名せず legacy 凍結し、Suasor は TS で新築する。本 ADR の Suasor 名・根拠・命名最終形は 0046 が継承するが、§Decision の「opshub を改名する」は実行されない) — Supersedes [ADR-0008](0008-naming-opshub.md)
- Date: 2026-06-14
- Deciders: opshub maintainers
- Related: [ADR-0008](0008-naming-opshub.md) (Naming: opshub — superseded by this ADR), [ADR-0004](0004-agent-runtime-boundary.md) (assistant agent direction — the "secretary that advises" the new name personifies), [ADR-0011](0011-ozzy-labs-ecosystem-adoption.md) (ozzy-labs naming rules), [ADR-0022](0022-mcp-server-surface.md) (MCP server name = `opshub`), [ADR-0032](0032-runtime-toml-config-loading.md) (`$XDG_CONFIG_HOME/opshub/` config dir), `pyproject.toml` (`ozzylabs-opshub` dist name / `[project.scripts]` bin)

## Context

opshub の実態は **ローカル優先の operational memory + 秘書エージェント**である ── 散らばった業務文脈 (Slack / メール / 文書 / 予定 / コード / Web) を手元に集めて記憶にし、その上で **要約・検索・下書き・タスク/決定の抽出を「助言・提案」し、適用は人の承認制 (HITL)** で行う ([ADR-0004](0004-agent-runtime-boundary.md)、アシスタント 15 skill)。read 取り込み専用 + 外部に勝手に送らない posture が最大の差別化。

[ADR-0008](0008-naming-opshub.md) で名称を `opshub` に決めた。その ADR は §Negative で自ら **「`OpsHub` は DevOps/ITOps Hub 系と語感が近く没個性」「本質価値 (Operational Memory) が名から読めない」** を認めつつ、§Positive #3 で **「`opshub` という主要プロダクトは存在しない (検索衝突が限定的)」** と仮定していた。

本 ADR の再検討で、**この §Positive #3 の前提が事実と異なる**ことが判明した:

- **`OpsHub, Inc.` (opshub.com) は実在の現役ベンダー**で、製品 **OpsHub Integration Manager (OIM)** は 60〜70 種の ALM / DevOps / ITSM 統合・移行プラットフォーム (G2 レビューあり、2026 年も Archive Manager / Polarion 対応など新製品を出している)。
- つまり `opshub` は (a) **"ops＝DevOps" の誤カテゴリを誘発**し、(b) その誤カテゴリ領域の **実在企業と同名**で、(c) **`opshub.com` も先方が保有**。皮肉にも先方の新製品 (履歴データを文脈ごと保全する集中アーカイブ) は opshub の "operational memory" に概念まで薄く被る。

opshub は個人向けの「助言する秘書エージェント」であって DevOps 統合ではない。**能力でも記憶でもなく「助言する側近 (adviser)」として名乗る**のが実態に最も合う。pre-userbase の今が改名の最安タイミングである ([pre-userbase posture](0008-naming-opshub.md) 同様、互換シム不要)。

## Decision

製品名を **Suasor** (正規読み: **スアソル** / soo-AH-sor) に改名する。ラテン語 *suāsor*「助言・説得する者＝顧問」(動詞 *suādēre*「勧める」の動作主名詞、英 *persuade / suasion* と同根)。opshub の中核ループ **read → remember → advise / propose、決定は人間 (HITL)** を「助言する者」として擬人化する。

**ただし機械的リネームの実装は本 ADR では行わず、専用の top-level Phase に defer する** (pre-userbase → hard flip、互換シムなし)。本 ADR は決定と根拠の記録である。

### (a) 命名の最終形

| 項目 | 旧 | 新 |
|---|---|---|
| 製品名 / displayName | OpsHub | **Suasor** |
| 正規読み | — | **スアソル (soo-AH-sor)**。SWAY-sor は語源バリアント |
| CLI bin | `opshub` | `suasor` |
| Python import package | `opshub` (`src/opshub/`) | `suasor` (`src/suasor/`) |
| PyPI dist | `ozzylabs-opshub` | `ozzylabs-suasor` |
| MCP server name | `opshub` | `suasor` |
| env 接頭辞 | `OPSHUB_*` | `SUASOR_*` |
| config dir | `~/.config/opshub/` | `~/.config/suasor/` |
| Settings class | `OpsHubSettings` | `SuasorSettings` |
| GitHub repo | `ozzy-labs/opshub` | `ozzy-labs/suasor` (redirect 有効) |
| tagline (description) | "Local-first operational memory and execution hub for humans and AI agents" | JP「読み、覚え、助言する秘書エージェント」/ EN "Reads, remembers, advises — you decide." |

### (b) 選定基準

「**同棚 (local-first / 個人 AI 秘書・エージェント・記憶) で生きている同名**」だけを回避し、遠い分野・死蔵・極小の同名は許容する、を採用した。完全無衝突は (下記の通り) 2026 年には非現実的なため。

### (c) 探索の要点 (約 100 候補を screening)

- **2026 年、AI 秘書/エージェント/記憶の命名空間は飽和している**。Aide / Steward / Cairn / Metis / Munshi / Diwan / Stele / Engram / Mnemo / Thoth / Seshat / Nabu / Noema 等、響きの良い実在語・神様名は軒並み **同棚の現役プロダクト**に先取りされており、その多くは opshub にそっくりな local-first AI 秘書/記憶 (例: Thoth / Noema = local-first AI assistant、Engram = Weaviate の AI memory layer (2026-06 GA)、Metis = metaphacts の HITL 知識エージェント) だった。
- ゆえに実在語では「クリーン・独自・誤カテゴリでない」を同時に満たせず、**造語が唯一の現実解**と判断した (CLI/dev ツールでは `uv` / `ruff` / `rg` / `mise` のように短い造語名がむしろ正道)。
- 最終造語 5 候補 (Comita / Suasor / Pistis / Propono / Fidus) はいずれも同棚狙い撃ち検証を通過。その中で **Suasor** を「助言・提案」の意味適合で採用した (Comita は最もクリーンだが「随伴者」止まりで能動性を欠く)。
- **Suasor の同棚検証結果**: 同棚 AI 製品なし。被りは ★1 の趣味メディア推薦 repo (`unfaiyted/suasor`)、休眠のクリエイティブ代理店 `suasor.com`、古い "recommender" 習作数件のみ (いずれも competitor ではない)。PyPI `suasor` 空き、`ozzy-labs/suasor` repo 取得可、CLI バイナリ衝突なし。

### (d) 発音

正規読みを **スアソル (soo-AH-sor、綴りどおり)** に固定し、README/docs に明記する。SWAY-sor (英 *suasion/persuade* 由来) は「語源的にはこうも読める」許容バリアントとして注記のみ。**綴りに逆らう読みは定着しない** (GIF 論争) ため、字面読み + 既存カタカナ「スアソル」と一致させる。

## Consequences

### Positive

1. **誤カテゴリの解消** — "ops＝DevOps" の連想を断ち切り、OpsHub Inc. との同名衝突も解消。
2. **クリーンで ownable** — PyPI / GitHub / 同棚すべてで競合なし。`ozzylabs-suasor` で配布。
3. **意味が実態に合致** — 「助言する者」が「助言・提案する秘書エージェント」([ADR-0004] / 15 skill) と直結。
4. **改名コスト最小のタイミング** — pre-userbase ゆえ互換シム不要の hard flip。

### Negative / Trade-offs

1. *suasor* は無名の古ラテン語で、**意味が自明でない** → tagline / README / 製品が意味を担う (造語路線の宿命、`uv` / `ruff` と同じ割り切り)。
2. **発音がぶれうる** (スアソル / SWAH-sor / SWAY-sor) → 正規読みを「スアソル」に固定して緩和。
3. **`suasor.com` は先行** (クリエイティブ代理店)。`.dev` / `.sh` 等を使用する。
4. dev 界に薄い **"suasor＝recommender"** の足跡 (ラテン語義由来の古い習作)。実害ほぼなし。
5. **"persuade / 説得"** の語感は HITL の控えめさよりやや能動寄り (purist 的指摘)。
6. 実装は機械置換を伴う (別 Phase で実施)。`OPSHUB_` を含むファイルだけで ~188、`opshub` / `OpsHub` 文字列・import path を含めると範囲はさらに広い。

## Deferred Rename Surface (実装は未実施・将来 Phase)

リネーム実装時の機械置換マップ (pre-userbase hard flip、互換シムなし):

| 層 | 現状 | 変更後 |
|---|---|---|
| PyPI dist | `ozzylabs-opshub` | `ozzylabs-suasor` |
| import / package | `src/opshub/` | `src/suasor/` |
| CLI bin (`[project.scripts]` / Typer `name=`) | `opshub` | `suasor` |
| MCP server name (`serve_stdio(server_name=...)` / log namespace) | `opshub` | `suasor` |
| env 接頭辞 (~188 ファイル、`OPSHUB_CONNECTORS__SLACK__WORKSPACES__<ALIAS>__*` / `OPSHUB_CONFIG_DIR` / keyring slot 等の深いネスト含む) | `OPSHUB_*` | `SUASOR_*` |
| config dir (`config.toml` / `excludes.yaml` / `secrets.toml`) | `~/.config/opshub/` | `~/.config/suasor/` |
| Settings class | `OpsHubSettings` | `SuasorSettings` |
| keyring service / slot 名 | `...opshub...` | `...suasor...` |
| GitHub repo / URLs | `ozzy-labs/opshub` | `ozzy-labs/suasor` |
| docs / ADR / README / skills SSOT の `opshub` 文字列 | `opshub` | `suasor` |
| **無影響** | ecosystem 共通 skill `@ozzylabs/skills` (別 namespace) | (触らない) |

注意点:

- リネームは **DB schema / `external_id` を変えない**ため、**DB 再構築は原則不要**。operator 影響は config dir + keyring slot の移行 (→ 再 init / 再 auth) に限られる (再構築の要否最終判断は実装 Phase で確定)。
- **ADR ファイル (本 ADR / 0008 含む) はリネーム対象外** — 履歴として `opshub` 表記のまま残す (ADR immutability)。将来のリネームで ADR 本文を書き換えない。
- 新 architectural pattern ではなく**機械的リネーム**なので、専用 Phase 1 本で完結させる。

**本 ADR 確定時点ではコードは未変更**。`OPSHUB_*` / `opshub` 文字列・config dir・CLI 名は現状維持。

## Alternatives Considered

- **`opshub` を維持** — 却下。"ops＝DevOps" 誤読 + OpsHub Inc. が実在 + `opshub.com` を先方が保有 + 没個性。ただし改名コスト (~188 ファイル) ゆえ最後まで有力な対抗だった (フラット評価では C+〜B−)。pre-userbase でユニークさより誤カテゴリ是正を優先し改名に倒した。
- **同棚被りの実在語を採り、製品で差別化** (Cairn / Steward / Metis / Aide 等) — 却下。改名の目的 (独自性・クリーンさ) に反し、近接双子 (Metis = metaphacts の HITL 知識エージェント、Thoth / Noema = local-first AI 秘書の双子) と音/検索で混同される。
- **他の最終造語** — Comita (随伴者・最もクリーンだが「助言・提案」の能動性を欠く) / Pistis (信頼・やや長い) / Propono (提案・音が反復) / Fidus (忠実・語が混雑)。Suasor を「助言・提案」の意味適合で採用。

## Validation / Follow-up

- リネーム実装は将来の **top-level Phase issue** で行う: 影響範囲チェックリスト + `OPSHUB_*`→`SUASOR_*` 等の機械置換 + テスト更新 + `ozzy-labs/suasor` repo rename + ドメイン (`suasor.dev` 等) 登録 + 商標ざっと確認。
- 本 ADR は [ADR-0008](0008-naming-opshub.md) を supersede する (0008 の Status を更新済み)。
