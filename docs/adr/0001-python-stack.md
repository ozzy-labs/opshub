# 0001. Python Stack

- Status: Accepted
- Date: 2026-05-16
- Deciders: ozzy

## Context

OpsHub は ozzy-labs における初の本格 Python リポジトリ候補となる。既存 ozzy-labs ファミリー (`forge` / `skills` / `presets` / `starlight` / `road` / `web` / `mcp-knowledge`) は TypeScript / Node エコシステムで統一されており、npm scope `@ozzylabs` に集約されている。

この前提を覆して Python を選ぶ意思決定であるため、根拠は厳密に必要となる。2026 年現在、ADR-0012 (Embedding Strategy) と ADR-0002 (Event-Sourced Architecture) の確定を受け、Python を選ぶ実利的な根拠は次の 4 点に収束する。

1. **sqlite-vec の Python binding が圧倒的に成熟** — Phase 4 (Semantic Layer) で採用する sqlite-vec は author Alex Garcia 自身が Python example を一次資料として整備しており、SQLAlchemy custom type / `load_extension` ハンドリング / 仮想テーブル制約のサンプルが Node / Go / Rust 比で 1 桁多い。Phase 4 着手時に「踏み抜く可能性のある非自明な詰まりどころ」を最小化できる
2. **Alembic の代替が他言語に存在しない** — event-sourced な OpsHub では `events` schema 進化、`tasks` / `decisions` / `embeddings` projection の列追加・index 変更が継続的に発生する。Alembic の auto-generate / branch merge / downgrade / version 管理は TS の drizzle-kit、Go の golang-migrate、Rust の sqlx-cli の追随を許さない (特に branch merge と auto-generate 精度)
3. **Pluggable embedder の local backend を Phase 4 で選択肢として残せる** — ADR-0012 で採択した Pluggable 戦略のうち、local backend (`sentence-transformers`, `BAAI/bge-m3` 等) は Python ファースト。TS の `transformers.js` / `onnxruntime-node` はモデル選択肢が狭く、品質ベンチも限定的。local-first 旗印 (Principles 1) を真に貫くなら local 経路を残すべきで、これは Python のみが現実的に開いている道
4. **AI 駆動開発との相性** — Claude Code / Codex CLI / Gemini CLI / GitHub Copilot CLI のいずれも LLM 訓練データの分布上 Python のコード生成が最も安定する。個人 + 4 vendor agent 前提の OpsHub では、AI に書かせる量が多いほど Python の優位が積み上がる

加えて、event-sourced + Pydantic + SQLAlchemy Core の組み合わせは domain event の immutable / versioned / discriminated union 表現と噛み合いやすく、`frozen=True` + `discriminator` で event 型を自然に書ける。

ozzy-labs の npm scope `@ozzylabs` からは外れるが、OpsHub は CLI バイナリ (`opshub`) として利用者から見え、PyPI vs npm の差はエンドユーザーへの影響が小さい。配布チャネル分離は受容コストとして織り込む。

## Decision

OpsHub を **Python 3.13+** で実装する。中心スタック:

| 用途 | 採用 |
|---|---|
| Runtime | Python 3.13 |
| Package / venv / バージョン管理 | uv |
| CLI | Typer (lazy import 規約あり) |
| DB query | SQLAlchemy 2.x **Core** (ORM 不採用) |
| Validation | Pydantic v2 |
| Testing | pytest + pytest-asyncio + pytest-cov + pytest-xdist |
| Lint / Format | ruff |
| Type Check (local) | pyright (高速、watch mode) |
| Type Check (CI) | mypy (`--strict`) |
| Migration | Alembic |
| Vector storage | sqlite-vec (Phase 4 / **base dep from Phase 8.x** — see §Updates) |
| Local embedding | sentence-transformers (Phase 4、`[local-embedding]` extras) |
| API embedding | openai / voyageai (Phase 4、`[api-embedding-*]` extras) |
| Logging | structlog |
| Templates (markdown) | Jinja2 |
| Tool バージョン固定 | mise |
| Git hooks | lefthook |
| Task runner | just |
| CI/CD | GitHub Actions |
| Release | release-please + uv publish (PyPI Trusted Publishers) |
| Dependency 更新 | Renovate |
| Secret scan | gitleaks |
| Distribution build (任意) | PyInstaller (core-only、最終手段) |

### 依存分離の方針

ADR-0012 と整合する `pyproject.toml` 設計を Phase 1 で確定する。

```toml
[project]
dependencies = [
  # core はピュア Python 中心、heavy ML SDK ゼロ
  "typer", "sqlalchemy", "alembic", "pydantic",
  "jinja2", "structlog",
  "sqlite-vec",                  # Phase 8.x: 昇格 (詳細は §Updates 参照)
]

[project.optional-dependencies]
vector              = ["numpy"]  # Phase 8.x: sqlite-vec を base へ昇格
local-embedding     = ["sentence-transformers"]
api-embedding-openai = ["openai"]
api-embedding-voyage = ["voyage-ai"]
connectors-github   = ["httpx", "PyGithub"]
connectors-slack    = ["slack-sdk"]
connectors-msgraph  = ["msgraph-sdk"]
connectors-box      = ["boxsdk"]
dev                 = ["pytest", "pytest-xdist", "mypy", "pyright", "ruff", ...]
all                 = ["opshub[vector,local-embedding,api-embedding-openai,connectors-github,connectors-slack]"]
```

これにより:

- core install (`uv tool install opshub`) は ~10MB (sqlite-vec ~500KB 含む)
- API embedder を使うユーザーは `uv tool install 'opshub[api-embedding-openai]'`
- フルスタックは `uv tool install 'opshub[all]'`
- 単一バイナリ配布の道 (PyInstaller / Nuitka) は core 限定で残る

### 配布チャネル

| Phase | 主配布 | 副配布 |
|---|---|---|
| 1-3 | `uv tool install --from .` | `mise exec -- opshub` |
| 3 末 | PyPI publish (`uv tool install opshub`) | `uvx opshub` |
| 4 | + Homebrew formula (任意) | + Docker image (ML 用途) |
| 4+ | + GitHub Releases (PyInstaller core-only、任意) | — |

PyPI publish は **OIDC Trusted Publishers** を使う (`PYPI_TOKEN` legacy は禁止)。詳細は知識 MCP `standards/pypi-trusted-publishers` および `standards/npm-trusted-publishers` 同様の方針。

### `__main__.py` を提供する

`python -m opshub` でも `opshub` でも同じ entry point に到達できるように Phase 1 から `src/opshub/__main__.py` を配置する。bundled / non-bundled いずれの配布形態でも一貫させる。

### lazy import 規約

Typer subcommand を `app.command()` の関数内 import で遅延ロードする。top-level で重い依存 (`sentence-transformers` / `sqlite-vec` / connector SDK) を import しない。`opshub` cold start ~200ms を ~120ms 程度に抑える前提条件。

## Consequences

### Positive

1. **sqlite-vec の踏み抜きリスク最小化** — Phase 4 の最大コスト項目に対し、最も成熟した binding を採用できる
2. **Alembic で event-sourced schema 進化を実装コスト最小で扱える** — `events` テーブル、9 種の projection、`embeddings` テーブルそれぞれの追加・列変更が auto-generate でドラフト化される
3. **local embedding の選択肢を Phase 4 まで開いたまま設計できる** — local-first 原則の本気度を技術的に担保
4. **AI 駆動開発の生産性** — 個人 + 4 vendor agent 前提で実利が大きい
5. **uv が Python の DX を JS 級に押し上げ** — `pip` + `virtualenv` + `pyenv` + `pipx` + `poetry` を 1 ツールに統合、wheel install は ~2-5 倍高速
6. **pytest の fixtures / parametrize / plugin 生態系** — event-sourced のテスト (event 列を fixture 化、reducer を parametrize で網羅) と相性◯
7. **Pydantic v2 + SQLAlchemy Core で domain event を自然に表現** — `frozen=True` + `discriminator` で immutable / versioned event 型、Core で aggregate を ORM map せずに reducer に集中できる
8. **依存分離設計** — `[project.optional-dependencies]` で core を ML フリーに保ち、ユーザーは必要な機能だけ install できる

### Negative / Trade-offs

実害の大きさ順に列挙する。緩和策を明示しない項目は ADR-0001 リライト時に書き直す。

1. **CLI 起動時間 ~200ms (cold)** — agent が毎ターン `opshub event append` を叩くワークフローで累積コスト化
   - 緩和: Typer の lazy import 規約、必要に応じて Phase 2-3 で `opshub serve` REPL モード追加、Phase 4 で MCP server 化 (ADR-0006 と整合)
2. **型チェック速度・厳密性が TS に劣る** — `mypy --strict` は OpsHub 規模で 5-10 秒
   - 緩和: pyright を local 一次型チェッカ (~0.5s)、mypy は CI 二次、ruff の type-related lint で構文ミスを前段で除去
3. **単一バイナリ配布が困難** — `uv tool install` は Python install を前提とする (uv が Python install 込み)
   - 緩和: Phase 1-3 は `uv tool install` で充足、Phase 4 で Homebrew formula 追加検討、PyInstaller (core-only) を最終手段として残す
4. **`@ozzylabs` npm scope から外れる** — TS 共通ユーティリティを直接共有できない
   - 緩和: 配布チャネル分離 (PyPI 独立)、将来 TS client が必要なら `@ozzylabs/opshub-client` (CLI 子プロセス起動の薄ラッパー) を別パッケージで後付け
5. **ozzy-labs commons の Node 寄り設定混入** — `biome.json` / `.vscode/extensions.json` / Node ベース Dockerfile が定期 sync で再混入
   - 緩和: `.commons/sync.yaml` の `pinned:` リスト整備で除外。bootstrap で完結する
6. **GIL / 並行性の将来制約** — Phase 3 で複数 connector が並行 fetch するとき I/O 並列度が問題化しうる
   - 緩和: `asyncio` + `aiosqlite` で I/O 並列、必要なら `multiprocessing` で connector 別プロセス。Phase 3 着手時に再評価
7. **依存肥大化** — Python は transitive dep が深くなりがち
   - 緩和: `[project.optional-dependencies]` で機能別分離 (採用済み)、Renovate で dep 更新を自動化、`uv tree` で定期監査
8. **OpsHub をモバイル / Web client に拡張する場合に Python は使えない** — 直接の影響なし
   - 緩和: 境界を CLI / JSON API に置く設計 (ADR-0004 と整合)、UI 層は別言語 (TS) で書く前提

## 軽減策のサマリ

| 課題 | Phase 1 で着手 | 後 Phase で再評価 |
|---|---|---|
| 起動時間 | lazy import 規約 | REPL モード (Phase 2-3) / MCP server 化 (Phase 4) |
| 型チェック | pyright + mypy 二段構え | — |
| 配布 | `[project.optional-dependencies]` 骨格、`__main__.py` | Homebrew (Phase 4) / PyInstaller (任意) |
| ozzy-labs 整合 | `.commons/sync.yaml` `pinned:` 整備 | — |
| 並行性 | — | Phase 3 で再評価 |
| 依存肥大 | optional-dependencies + Renovate | `uv tree` 監査 (定期) |

## Alternatives Considered

### 1. TypeScript / Node + drizzle + zod + better-sqlite3 + commander

却下理由 (2026 年版):

- **sqlite-vec Node binding の成熟度が Python 比で不足** — `better-sqlite3` の `loadExtension()` 経由で動くが、example / blog / 質問数で 1 桁少ない。Phase 4 で踏み抜きリスク
- **Alembic 級の migration ツールが存在しない** — drizzle-kit は良質だが branch merge / downgrade / auto-generate 精度で Alembic に及ばない。event-sourced で schema 進化が連続するため致命的
- **local embedding の選択肢が狭い** — `transformers.js` / `onnxruntime-node` はモデルカタログが limited、日本語 + 英語 multilingual の品質ベンチが不足
- TS SDK の充実 (Anthropic / OpenAI / Google) は事実だが、ADR-0004 で「LLM は agent 側で呼ぶ、OpsHub 自身は呼ばない」と決めているため OpsHub の決定要因にならない
- ozzy-labs エコシステム整合は実利だが、上記 3 点を覆すほどではない

### 2. TypeScript + Bun + libSQL + drizzle

却下理由:

- 単一バイナリ配布 (`bun build --compile`) と libSQL native vector は魅力的だが、`Alembic 不在` 問題は drizzle-kit でも未解決
- libSQL は SQLite fork のため、sqlite-vec の Python binding ほどの参考実装蓄積がない
- Bun は若く、Phase 4 着手時 (~2-3 年後想定) に成熟しているかリスク残

### 3. Rust + sqlx + clap

却下理由:

- 性能・安全性は最高だが、ドメイン (event store / markdown 生成 / agent 連携) で性能要件はそこまで厳しくない
- 開発速度が Python 比で著しく遅い (個人プロジェクトには致命的)
- Phase 4 (semantic 層) で LLM / Embedding を扱う際、Rust エコシステムは未成熟
- AI 駆動開発との相性が Python / TS より低い

### 4. Go + sqlc + cobra

却下理由:

- CLI 開発体験 + 単一バイナリ配布は良いが、Pydantic 相当の validation ライブラリが弱い
- event sourcing パターンの参考実装が Python / Java / TypeScript 比で少ない
- AI / LLM SDK のサポートが限定的
- sqlite-vec Go binding は存在するが example 希少

### 5. Python + Django / FastAPI

却下理由:

- OpsHub は Web フレームワーク不要 (CLI + 内部 SQLite)。Django / FastAPI を入れると不要な依存が増える
- Web 化が必要になった段階で FastAPI を追加検討する余地は残す (ADR-0007 と整合)

### 6. Python + Click (Typer 不採用)

却下理由:

- Click は安定だが、Typer は Click ベースで型ヒント統合を提供する上位互換
- Pydantic v2 と組み合わせると Typer の体験が一段良い

## Open Questions

1. **Phase 3 並行性方針** — `asyncio` + `aiosqlite` で十分か、`multiprocessing` で connector 別プロセス化するか。Phase 3 着手時に再評価
2. **PyInstaller (core-only) 採用判断** — Phase 4 末で OSS ユーザー需要が立てば追加。それまでは `uv tool install` のみ
3. **`opshub serve` REPL の優先度** — Phase 2 で着手するか、Phase 4 の MCP server 化に統合するか
4. **Homebrew formula の維持** — Phase 4 で formula を作るか、`uv tool install` の案内で十分とするか
5. **Python 3.13 `--disable-gil` (PEP 703) の採用タイミング** — preview 期間中はオプション、GA 後に標準採用するかは Phase 3-4 で判断

## Updates

### Phase 8.x — sqlite-vec promotion (2026-05-18)

`sqlite-vec` was promoted from the `[vector]` extras to base `[project.dependencies]`.

**Why**: Phase 4 migration 0013 (`0013_create_embeddings_vec_table`) unconditionally
runs `CREATE VIRTUAL TABLE ... USING vec0(...)` on `opshub init`. Without the
extension loaded, init fails with `OperationalError: no such module: vec0` and
leaves the DB in a half-applied state (subsequent retries hit
`_alembic_tmp_embeddings already exists`). The v0.1.0 release smoke test caught
this against the documented `uv tool install opshub` Quickstart path.

**Why this is consistent with the original "heavy ML deps in extras" stance**:

- sqlite-vec wheel is ~500 KB (vs. sentence-transformers + torch at ~500 MB-2 GB)
- It is a SQLite extension, not an ML framework — closer to "DB engine
  feature" than "embedding model"
- Phase 4-8 are uniformly built on top of vec0 (embeddings + recall + dup
  detect + briefing source_refs + knowledge graph) — the package is
  effectively unusable without it
- ADR-0001 §"配布チャネル" explicitly mentioned "core install ~10 MB" as the
  target; sqlite-vec keeps the core inside that budget

**What's kept in `[vector]`**:

- `numpy>=2.0` — required only by callers that materialise embedding tensors
  outside the embedder path. Most operators using OpsHub through the CLI do
  not need it; `local-embedding` extras pulls it transitively when the local
  embedder is active.

**Backward compat**: `uv sync --extra vector` and `uv tool install
"opshub[vector]"` continue to work (the extras still exists, just narrowed to
`numpy`). Existing CI recipes that pass `--extra vector` to enable sqlite-vec
work without modification because pip / uv resolve unions.

This change is also why this ADR's §"配布チャネル" table no longer mentions
`[vector]` as a Phase-3-end prerequisite — sqlite-vec is now available out of
the box.

### v0.1.0 — PyPI distribution under `ozzylabs-opshub` (2026-05-18)

v0.1.0 ships on **PyPI under the distribution name `ozzylabs-opshub`** — not
the bare `opshub` originally planned. The CLI command, Python import name,
and source directory all stay `opshub`; only the PyPI-side distribution
name has the prefix.

**Why the prefix**:

- The bare `opshub` was rejected by PyPI's Pending-Publisher pre-registration
  (likely a typosquat-prevention reservation — the public index
  (`/simple/opshub/`) returns 404, but the Web UI refused to register the
  name)
- PyPI does **not** support npm-style `@scope/package` namespacing (PEP 708
  is in discussion but not landed); the de facto convention for
  organisation-owned packages is PEP 423's `<owner>-<package>` form
- `ozzylabs-opshub` makes the ownership relationship visible on `pypi.org`
  in the same way `@ozzylabs/foo` does on `npmjs.com`, and is the natural
  prefix if future ozzy-labs Python packages need PyPI distribution

**Implementation footprint**:

- `pyproject.toml [project] name = "ozzylabs-opshub"` — the only "rename"
  touch point
- Wheel filename becomes `ozzylabs_opshub-0.1.0-py3-none-any.whl` (PyPI
  normalises `-` → `_` in distribution filenames)
- Python import: `import opshub` — unchanged
- CLI command: `opshub --version` — unchanged
- README + RUNBOOK + release-notes + CHANGELOG: install command shows
  `uv tool install ozzylabs-opshub`, with `git+https://...@vX.Y.Z` as an
  Alternative install path for unreleased / air-gapped scenarios
- PyPI Trusted Publisher must register with **PyPI Project Name =
  `ozzylabs-opshub`** (not `opshub`); the GitHub org / repo / workflow /
  environment values are unchanged

**Earlier exploration (superseded)**:

A short-lived decision (~30 minutes between PR #157 and this one) was to
ship v0.1.0 via `uv tool install git+https://github.com/...@v0.1.0` and
defer PyPI entirely. That was reverted once the maintainer registered the
PyPI account (`ozzylabs`) and discovered the bare-name block — at which
point PyPI distribution under a prefixed name became cheaper than
maintaining the git-source-only path. PR #157 is preserved in git history
for the rationale + the install-from-git fallback documentation.

**Renovate / Dependabot**:

Downstream consumers using Renovate / Dependabot can pin
`ozzylabs-opshub` via the standard `pypi` datasource — no special config
required. The `git+https://` Alternative install path is also supported
(via `github-releases` / `github-tags` datasource) for users who prefer
not to depend on PyPI.

## 関連

- [Principles 10 (Pythonic but Vendor-Neutral)](../principles.md)
- [Architecture (全体)](../architecture.md)
- [ADR-0002: Event-Sourced Architecture](0002-event-sourced-architecture.md)
- [ADR-0004: Agent Runtime Boundary](0004-agent-runtime-boundary.md)
- [ADR-0006: CLI-first MVP, defer MCP](0006-cli-first-mvp.md)
- [ADR-0007: Single Python Package, defer Monorepo](0007-single-python-package.md)
- [ADR-0011: Ozzy-Labs Ecosystem Adoption](0011-ozzy-labs-ecosystem-adoption.md)
- [ADR-0012: Embedding Strategy](0012-embedding-strategy.md)
- 知識 MCP: `languages/python/python` / `languages/python/uv` / `tools/sqlite-vec` (未収録、追加候補)
