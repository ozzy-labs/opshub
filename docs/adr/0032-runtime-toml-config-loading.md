# 0032. Runtime TOML Config Loading (`$XDG_CONFIG_HOME/opshub/config.toml`)

- Status: Accepted
- Date: 2026-06-03
- Deciders: opshub maintainers

## Context

Phase 1-15 完了時点で `opshub init` は `$XDG_CONFIG_HOME/opshub/config.toml` に starter TOML を生成するが、**実行時にどこからも読み込まれていない**。`OpsHubSettings` (pydantic-settings の `BaseSettings`) は env vars + defaults のみで初期化されており、TOML をパースするコードが `src/` 配下に存在しない (`grep -rn "tomllib\|toml.load" src/` の出力ゼロで pin)。

これは複数の既存 ADR で pin されている operator override 契約と乖離している:

- [ADR-0010 Connector Contract](0010-connector-contract.md) §改訂 (c) — Microsoft Graph delta-link / Drive `changes.list` cursor 失効時の `fallback_window_days` は `opshub.toml` で operator が上書き可能と pin
- [ADR-0012 Embedding Strategy](0012-embedding-strategy.md) §決定 §3 — `[embedding] backend` で `local` / `openai` / `voyage` / `disabled` を選択し、Phase 1 で `[embedding]` セクション parse 可能とすると pin
- [ADR-0019 Local-FS-backed Connector](0019-local-filesystem-backed-connector.md) §決定 (g) — box_drive / onedrive_drive の `exclude_globs` は `[connectors.<name>] exclude_globs` で operator が上書き可能と pin
- [ADR-0021 Encryption at Rest](0021-encryption-at-rest.md) §(b) — `[storage] encryption = true` で SQLCipher 暗号化を opt-in できると pin
- [ADR-0025 Office Document Content Extraction](0025-office-document-content-extraction.md) §決定 (b)(e) — `[office] max_file_size_mb` / `max_extracted_chars` / `[office.excel] max_cells_per_sheet` / `max_cells_per_workbook` を operator が上書き可能と pin

加えて `docs/upgrading.md:44` は「OpsHub's config file (`$XDG_CONFIG_HOME/opshub/config.toml`) is loaded via `pydantic-settings`, which silently ignores unknown keys」と記述しているが、**事実と異なる**。pydantic-settings は TOML を自動 load せず、`SettingsConfigDict(toml_file=...)` 指定 + `settings_customise_sources` override で source を明示的に組み込む必要がある。

本 ADR は実装 (#418) と並行して **判断を pin** することが目的で、loader 種別 / 優先順位 / パス解決 / fail 戦略を確定する。

### pre-userbase スタンス

opshub は Phase 1-15 完了時点で実ユーザー 0 (opshub memory `opshub-pre-userbase-compat-stance.md`)。compat / migration / dual-read / deprecation period は提案しない方針が確定しており、TOML 経路は **初出の正規実装**として直接導入する (env-only fallback の二重実装を残さない)。

## Decision

6 軸で意思決定する。

| # | 軸 | 決定 |
|---|---|---|
| 1 | loader | pydantic-settings 2.x の `TomlConfigSettingsSource` (公式 source、`tomllib` 経由) |
| 2 | 優先順位 | `init args > env > toml > defaults` (pydantic-settings 公式慣習) |
| 3 | TOML パス解決 | `OPSHUB_CONFIG_DIR` env var > `default_config_dir()` (= `$XDG_CONFIG_HOME/opshub`)、filename は固定で `config.toml` |
| 4 | ファイル不在時 | silent fallback (`FileNotFoundError` を投げない、defaults / env で構築) |
| 5 | 不正値 | 既存 `ConfigError` 経路で fail-fast (pydantic validation がそのまま効く) |
| 6 | ADR 番号 | 0023 / 0024 は欠番。直近採番 0031 (CLI command surface) の次なので **0032** |

### (1) loader = pydantic-settings 2.x `TomlConfigSettingsSource`

pydantic-settings 2.x が公式に提供する `TomlConfigSettingsSource` を採用する。自前で `tomllib.load` を呼んで dict を `OpsHubSettings(**data)` に渡す経路ではなく、`OpsHubSettings.settings_customise_sources` classmethod 経由で **sources tuple に組み込む**。

採用理由:

- **pydantic-settings の validation を継承** — TOML 由来値も env / init args と同じ pydantic field validator / `model_validator` / `ConfigError` 経路を通る。自前 `tomllib.load` 経路だと validation pass を別途維持する必要があり、`[embedding] backend = "grok"` 等の不正値で fail する経路が二重化する
- **nested section 表現が pydantic-settings 標準と一致** — `[storage] db_path` / `[connectors.box_drive] root_path` / `[office.excel] max_cells_per_sheet` 等の TOML nested section は `SettingsConfigDict(env_nested_delimiter="__")` の env var 規約 (`OPSHUB_STORAGE__DB_PATH` 等) と 1:1 対応し、operator が同じメンタルモデルで TOML / env を行き来できる
- **`tomllib` を直接呼ぶより少コード** — `settings_customise_sources` override + `TomlConfigSettingsSource(settings_cls, toml_file=path)` の組み込みは ~30 行で済む (`#418` 実装側)

### (2) 優先順位 = `init args > env > toml > defaults`

pydantic-settings 公式慣習 (`init args > env > dotenv > file_secret > defaults`) に TOML を **env と defaults の間** に挿入する。

```text
init args (テスト / 明示注入)  ← 最強
  ↓
env vars (OPSHUB_*)            ← CI / headless 環境で従来通り動く (後方互換)
  ↓
toml file (config.toml)        ← operator override の正規経路
  ↓
defaults (Field default 値)    ← 最弱
```

採用理由:

- **env が TOML に勝つ後方互換** — CI / 一時環境で `OPSHUB_EMBEDDING__BACKEND=openai` を export すれば TOML を編集せずに override 可能。Phase 1-15 で env-only 運用してきた既存 setup が **無改修で動く** (TOML 不在環境では §決定 (4) の silent fallback で defaults に解決)
- **init args が最強で test 用 injection を保つ** — `OpsHubSettings(embedding=EmbeddingSettings(backend="voyage"))` 明示が常に最優先で、test / fixture が TOML / env を意識せずに値を pin できる
- **TOML が defaults に勝つ正規 override** — `opshub init` で生成された `config.toml` を operator が編集すると、env を export しなくても次回 `opshub <cmd>` 実行で反映される (本 ADR の主目的)

### (3) TOML パス解決 = `OPSHUB_CONFIG_DIR > default_config_dir()`

TOML ファイルパスは以下 2 段で解決する:

```text
1. OPSHUB_CONFIG_DIR env var が設定されている場合: <OPSHUB_CONFIG_DIR>/config.toml
2. それ以外: default_config_dir() / "config.toml"
   = $XDG_CONFIG_HOME/opshub/config.toml
   = ~/.config/opshub/config.toml (XDG_CONFIG_HOME 未設定時)
```

filename は **固定で `config.toml`** (pydantic-settings の慣習に揃え、operator が複数ファイル名を覚える負担を増やさない)。

採用理由:

- **`OPSHUB_CONFIG_DIR` で dir 単位の差し替え** — CI / 一時環境 / multi-tenant test 等で config dir 全体を差し替えたい operator は `OPSHUB_CONFIG_DIR=/tmp/x` を export すれば `/tmp/x/config.toml` を読みに行く。DB path / keyring path 等の他の `default_config_dir()` 依存パスも同 dir 配下に揃う設計と整合
- **`default_config_dir()` は既存** — `src/opshub/core/paths.py` (Phase 1) で確立済の `default_config_dir()` を再利用する (XDG Base Directory Spec 準拠)。本 ADR で新規パス解決ロジックを足さない
- **`OPSHUB_CONFIG_DIR` は TOML 読込より先に解決** — TOML 読込前にパスを確定する必要があるため、`settings_customise_sources` 内で **`os.environ` を直接読む** (`OpsHubSettings.config_dir` field を信用しない循環依存回避、`#418` 実装側)

### (4) ファイル不在時 = silent fallback

`config.toml` が存在しない場合、**`FileNotFoundError` を投げず** defaults / env から構築する。pydantic-settings の `TomlConfigSettingsSource` デフォルト挙動 (file 不在時は empty dict を返す) をそのまま採用する。

採用理由:

- **`opshub init` 未実行の環境で `OpsHubSettings()` が即座に動く** — テスト / CI / 初回起動 / `opshub init` 自身の冪等再実行で TOML 不在時 fail-fast すると、初期化経路が機能しない (`opshub init` が `OpsHubSettings()` を構築する前に TOML を作る順序になり、循環)
- **env-only 後方互換と整合** — env で全 setting を渡している operator は TOML を作らずに運用できる
- **operator が TOML を書いたかどうかは別経路で観測** — `opshub init` 後に config 状態を確認したい operator は `opshub skills install --print-paths` (Phase 16-B `--print-paths` flag、`opshub init` 自体には flag 無し) や `docs/troubleshooting.md` §3.10 config 優先順位 で経路を辿れる

### (5) 不正値 = 既存 `ConfigError` で fail-fast

TOML 由来値が pydantic validation で reject された場合 (例: `[embedding] backend = "grok"` のような未定義 Literal 値) は、既存 `ConfigError` 経路でそのまま fail-fast する。新規エラーハンドリングは追加しない。

TOML 構文エラー (TomlDecodeError) は actionable error message に wrap して raise する (`#418` 実装側で `f"config.toml syntax error at line {n}: {detail}"` 形式)。

採用理由:

- **pydantic validation を信頼** — `EmbeddingSettings.backend: Literal["local", "openai", "voyage", "disabled"]` 等の field 制約は env / init args / TOML のどの source から来ても同じく effective。validation 経路を二重化しない
- **operator UX の一貫性** — env var で不正値を渡したときと TOML で不正値を書いたときで同じ `ConfigError` が出るため、operator のトラブルシュート手順が 1 つに揃う
- **silent ignore は不採用** — pydantic-settings の `case_sensitive=False` / `extra="ignore"` 既存設定で未定義 **キー**は silent ignore されるが、**定義済 field の不正値** (Literal 違反 / 型違反) は fail-fast する。これは本 ADR で変更しない既存挙動

### (6) ADR 番号 = 0032

採番ルール:

- 0023 / 0024 は欠番 (実在せず、`docs/adr/` directory listing で確認)
- 直近採番は 0031 ([ADR-0031 CLI Command Surface Organization](0031-cli-command-surface-organization.md))
- 次の連番は 0032

採用理由:

- ADR 番号は monotonic increasing、欠番は再利用しない (採番衝突回避)
- 0023 / 0024 を後付で埋めない (歴史的経緯 / 採番衝突回避、ADR-0000 §採番方針と整合)

## 既存 ADR との関係

本 ADR は 5 つの既存 ADR の operator override 契約を **実装可能化** する。各 ADR の責務範囲は不変:

- **[ADR-0010 Connector Contract](0010-connector-contract.md) §改訂 (c)** — Microsoft Graph delta-link / Drive `changes.list` cursor 失効時の `fallback_window_days` は本 ADR の TOML 読込経路で operator が override 可能となる。connector 本体契約 (Protocol / 責務 / 禁止事項) は不変
- **[ADR-0012 Embedding Strategy](0012-embedding-strategy.md) §決定 §3** — `[embedding] backend` / `[embedding] model` / `[embedding] dimensions` / `[embedding.openai]` 等の section parse が本 ADR で実装される。Embedder / VectorStore Protocol (§決定 §1) は不変
- **[ADR-0019 Local-FS-backed Connector](0019-local-filesystem-backed-connector.md) §決定 (g)** — box_drive / onedrive_drive の `[connectors.<name>] exclude_globs` / `root_path` / `content_extraction` 等の operator override が本 ADR で実装される。`open()` ban (§決定 (b)) / fingerprint 戦略 / identity 戦略は不変
- **[ADR-0021 Encryption at Rest](0021-encryption-at-rest.md) §(b)** — `[storage] encryption = true` / `[storage] db_path` 等の operator override が本 ADR で実装される。SQLCipher 採用 (§(a)) / keyring key 名 / extras 隔離 (§(d)) は不変
- **[ADR-0025 Office Document Content Extraction](0025-office-document-content-extraction.md) §決定 (b)(e)** — `[office] max_file_size_mb` / `max_extracted_chars` / `[office.excel] max_cells_per_sheet` / `max_cells_per_workbook` 等の operator override が本 ADR で実装される。markitdown 採用 (§決定 (a)) / source_type 分割 (§決定 (d)) / fail-safe (§決定 (c)) は不変

## non-goals

本 ADR の外側に明示的に置く事項:

- **YAML / JSON config への切り替え** — ADR-0012 / 0021 / 0025 で TOML が pin 済み、一貫性を取って TOML 継続。`config.yaml` / `config.json` 等の代替フォーマットは導入しない
- **multi-file config** — `config.toml` 1 ファイルに集約。`config.d/*.toml` 等の overlay 機構は導入しない (operator のメンタルモデル 1 ファイルに保つ)
- **runtime reload / watch** — TOML 編集後に opshub プロセスが自動 reload する経路は持たない。operator が `opshub <cmd>` を再起動する経路に揃える (long-running daemon 化は別 ADR の scope)
- **`opshub config edit` / `opshub config show` CLI** — TOML を CLI 経由で編集 / 表示する subcommand は本 ADR では設計しない。operator は通常の text editor で直接編集する (`opshub init` 生成 starter TOML に comment で各 section の用途を記載する経路に揃え、CLI subcommand 表面を増やさない)
- **`pyproject.toml` 統合** — opshub 自体の dev config を operator config と混同しないため、`config.toml` は opshub repo の `pyproject.toml` とは別ファイル (operator の `~/.config/opshub/` 配下) に保つ

## 採用しなかった代替

### 1. 自前 `tomllib.load` + `OpsHubSettings(**data)` 経路

却下理由:

- pydantic-settings の validation フックを通らず、env / init args と TOML で validation 経路が二重化する
- nested section ↔ env var 規約 (`OPSHUB_STORAGE__DB_PATH`) との 1:1 対応が崩れ、operator が TOML / env で別のキー命名を覚える必要が出る
- `settings_customise_sources` 経由なら ~30 行で済むが、自前経路だと `model_validator` + `tomllib.load` + dict merge + nested section walk で ~80 行以上になる

### 2. TOML を諦め env-only に統一

却下理由:

- 既存 actionable error / setup docs / ADR-0010 §改訂 (c) / ADR-0012 §決定 §3 / ADR-0019 §決定 (g) / ADR-0021 §(b) / ADR-0025 §決定 (b)(e) を全部書き換える必要があり、影響範囲が広い (`#416` 本文の影響範囲リスト参照)
- operator UX が悪化 — `opshub.toml` に書ける `[connectors.box_drive] exclude_globs = [...]` 相当を env で表現すると `OPSHUB_CONNECTORS__BOX_DRIVE__EXCLUDE_GLOBS='["..."]'` のような JSON 埋め込み string 形式になり、可読性 / diff 性が低い
- ADR-0012 §決定 §3 の illustrative TOML block (`[embedding] backend = "local"`) と `~/.config/opshub/config.toml` 案内が無効化され、ADR 信頼性が低下する

### 3. 新規 YAML / JSON config フォーマット

却下理由:

- ADR-0012 §決定 §3 / ADR-0021 §(b) / ADR-0025 §決定 (b)(e) で TOML が pin 済み。フォーマット変更は 3 ADR の同時改訂を要する
- pydantic-settings 2.x は TOML / JSON / YAML を等しく扱える source を提供するが、operator のメンタルモデルを 1 つに保つには既存 pin 済 TOML を継続するのが最低コスト
- TOML は section header (`[storage]` / `[connectors.box_drive]`) で nested 構造を表現するため、operator が「どの section に何が入るか」を線形に追える。YAML の indent ベース nesting より誤編集に強い

### 4. `OPSHUB_CONFIG_FILE` 環境変数で **ファイルパス**を渡す (dir ではなく)

却下理由:

- `OPSHUB_CONFIG_DIR` 経路 (本 ADR §決定 (3)) は DB path / keyring path 等の他の `default_config_dir()` 依存パスと **同 dir に揃う**ことが operator メンタルモデル的に自然
- file path を直接渡す経路を採ると、operator が「config.toml は別 dir、DB は別 dir、starter TOML 生成は別 dir」のような複雑な構成を組めるが、Phase 1-15 の運用想定 (`~/.config/opshub/` 1 dir に集約) と乖離する
- file path 指定は将来の `--config <path>` CLI flag で表現する余地を残す (本 ADR では env var 経由の dir 指定のみ pin)

## Consequences

### Positive

1. **既存 ADR の operator override 契約が実装可能化** — ADR-0010 / 0012 / 0019 / 0021 / 0025 で pin されている TOML override が初めて実際に効くようになる
2. **env-only 後方互換** — Phase 1-15 で env vars (`OPSHUB_*`) のみで運用していた setup は無改修で動く (env が TOML に勝つ優先順位、TOML 不在時 silent fallback)
3. **operator UX 向上** — `opshub init` で生成された starter TOML を text editor で編集すれば次回 `opshub <cmd>` で反映される。env vars 多数を export する必要がなくなる
4. **validation 経路 1 本化** — env / init args / TOML どの source も pydantic validation を通り、不正値は同じ `ConfigError` で fail-fast する
5. **`OPSHUB_CONFIG_DIR` で dir 単位の差し替え** — CI / multi-tenant test 等で config dir を差し替えたい operator は env 1 つで全 config (TOML / DB / keyring path) を別 dir に振れる

### Negative / Trade-offs

1. **pydantic-settings 2.x への依存度上昇** — `settings_customise_sources` override は pydantic-settings の internal API 寄りで、major version upgrade で signature が変わる可能性がある。`#418` 実装側で version pin を強める (`pydantic-settings>=2.x,<3`)
2. **`docs/upgrading.md:44` の嘘ドキュメント修正が必要** — 「pydantic-settings が自動 load する」と書かれているが事実と異なる。`#418` (実装 PR) で同時修正
3. **`opshub.toml` 編集後の retest 経路を operator に案内する必要** — TOML を編集した直後の動作確認手順 (`opshub <cmd>` 再実行で反映、daemon 化していないので reload なし) を `docs/troubleshooting.md` (`#419` で追加予定) に記載

## 関連

- [ADR-0010 Connector Contract](0010-connector-contract.md) — §改訂 (c) の `fallback_window_days` operator override が本 ADR で実装可能化
- [ADR-0012 Embedding Strategy](0012-embedding-strategy.md) — §決定 §3 の `[embedding]` section parse が本 ADR で実装可能化
- [ADR-0019 Local-FS-backed Connector](0019-local-filesystem-backed-connector.md) — §決定 (g) の `exclude_globs` operator override が本 ADR で実装可能化
- [ADR-0021 Encryption at Rest](0021-encryption-at-rest.md) — §(b) の `[storage] encryption` operator override が本 ADR で実装可能化
- [ADR-0025 Office Document Content Extraction](0025-office-document-content-extraction.md) — §決定 (b)(e) の `[office]` operator override が本 ADR で実装可能化
- [Phase 18 Tracking Issue (epic)](https://github.com/ozzy-labs/opshub/issues/416) — 本 ADR は Phase 18 PR 1 (#417) の成果物。PR 2 (#418、実装) / PR 3 (#419、operator docs) と並列実行
