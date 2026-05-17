# 0012. Embedding Strategy

- Status: Accepted
- Date: 2026-05-17
- Deciders: ozzy

## Context

Phase 4 で semantic recall (vector / 類似検索) を導入する。これに伴い「何を、どのモデルで embedding 化し、どこに保存し、いつ refresh するか」を決める必要がある。決定を Phase 4 着手時まで丸ごと先送りすると、以下が問題化する。

1. **依存設計を後から壊しがち** — `sentence-transformers` (torch を内包、~500-2000MB) を core に入れるか optional にするかは Phase 1 の `pyproject.toml` で決めなければ、Phase 4 で破壊的変更になる
2. **配布戦略との結合** — ADR-0001 で採用した Python Stack は ML 依存が core に入ると単一バイナリ / Homebrew / Docker いずれも実用域を外れる。配布戦略 (Phase 4 で確定予定) の前提条件にあたる
3. **local-first 原則との一貫性** — 「外部 SaaS body は持たない (ADR-0005) のに embedding は API に投げる」と矛盾しかねない。原則を貫くなら local embedder の道を残す必要がある
4. **`embeddings` projection の schema** — モデル変更時に増分 re-embed できる設計でないと、モデル乗り換えコストが指数的になる
5. **抽象境界の不在** — Phase 4 で具象実装に着手してから「やはり Pluggable にすればよかった」と気づくと、`services/` や CLI 全体に影響する

Phase 1 時点で「具体モデル / 次元 / 量子化」までは決められないが、**抽象境界 / 依存分離 / projection schema / refresh トリガ**は Phase 1 で決められるし、決めるべきである。

## Decision

OpsHub の embedding 層を **Pluggable Embedder + Pluggable VectorStore** として設計する。Phase 1 で抽象境界と projection schema を確定し、具象実装は Phase 4 で追加する。

### 1. 抽象境界 (Phase 1 で定義)

```python
# src/opshub/vectors/embedder.py
from typing import Protocol
import numpy as np

class Embedder(Protocol):
    model_id: str          # 例: "bge-m3" / "openai:text-embedding-3-small"
    model_version: str     # 例: "v1" / "2024-10-01"
    dimensions: int

    def embed(self, texts: list[str]) -> np.ndarray: ...
    # 戻り値 shape: (len(texts), dimensions), dtype: float32

# src/opshub/vectors/store.py
class VectorStore(Protocol):
    def upsert(self, entity_type: str, entity_id: str,
               vector: np.ndarray, model_id: str, model_version: str) -> None: ...
    def query(self, entity_type: str, vector: np.ndarray,
              k: int, filter_sql: str | None = None) -> list[tuple[str, float]]: ...
    def delete(self, entity_type: str, entity_id: str) -> None: ...
```

具象実装の想定 (Phase 4):

| 実装 | 種別 | 依存 |
|---|---|---|
| `LocalSentenceTransformerEmbedder` | local | `sentence-transformers` |
| `OpenAIEmbedder` | API | `openai` |
| `VoyageEmbedder` | API | `voyage-ai` |
| `SqliteVecStore` | sqlite-vec backed | `sqlite-vec`, `numpy` |

### 2. 依存分離 (Phase 1 で確定)

`pyproject.toml` の `[project.optional-dependencies]` で embedding 関連依存を core から分離する。

```toml
[project]
dependencies = [
  # core は ML 依存ゼロを維持
  "typer", "sqlalchemy", "alembic", "pydantic",
  "jinja2", "structlog",
]

[project.optional-dependencies]
vector = ["sqlite-vec", "numpy"]
local-embedding = ["sentence-transformers"]
api-embedding-openai = ["openai"]
api-embedding-voyage = ["voyage-ai"]
all = ["opshub[vector,local-embedding,api-embedding-openai]"]
```

install パターン:

```bash
uv tool install opshub                                # core のみ (~10MB)
uv tool install 'opshub[vector]'                      # + sqlite-vec (~40MB)
uv tool install 'opshub[vector,api-embedding-openai]' # + API client (~45MB)
uv tool install 'opshub[vector,local-embedding]'      # + torch (~500MB-2GB)
```

### 3. 設定駆動の backend 切替

`~/.config/opshub/config.toml` で具象 backend を選択する:

```toml
[embedding]
backend = "local"             # "local" | "openai" | "voyage" | "disabled"
model = "BAAI/bge-m3"
dimensions = 1024

[embedding.openai]
model = "text-embedding-3-small"
api_key_env = "OPENAI_API_KEY"
```

デフォルト backend は **Phase 4 着手時に決定** する (本 ADR の Open Question 1)。Phase 1-3 では `backend = "disabled"` (embedding 機能 OFF) が default。

### 4. Embed 対象 (ADR-0005 整合)

**Embed する**:

- `tasks.summary` / `tasks.description`
- `decisions.text` / `decisions.rationale`
- `inbox_items.summary`
- `sources.summary` (full body ではなく要約)
- `briefings.content` (生成済 markdown briefing)
- `extracted_action_items.text`

**Embed しない**:

- `events.payload` の生データ (event 列を直接検索する用途なし)
- external SaaS の full body (ADR-0005 で保持していない)
- code / binary / 機密文書

### 5. `embeddings` projection schema (Phase 1 で骨格作成)

Phase 1 では空テーブルとして migration を作成、Phase 4 で活用する。

| カラム | 型 | 用途 |
|---|---|---|
| `id` | INTEGER PK | rowid |
| `entity_type` | TEXT NOT NULL | `task` / `decision` / `inbox_item` / `source` / `briefing` |
| `entity_id` | TEXT NOT NULL | 対応する entity の ID (ULID) |
| `model_id` | TEXT NOT NULL | embedder の identifier |
| `model_version` | TEXT NOT NULL | 同一 model_id 内のバージョン (re-embed 判定用) |
| `dimensions` | INTEGER NOT NULL | 検証用 |
| `created_at` | TEXT NOT NULL | UTC ISO8601 |
| (vector) | sqlite-vec virtual table 側 | rowid で `embeddings` と JOIN |

`UNIQUE (entity_type, entity_id, model_id, model_version)` 制約で「同一 entity に同一モデルの重複 vector」を防ぐ。

### 6. Refresh トリガ

| トリガ | 動作 |
|---|---|
| **Event 駆動** | `TaskActivated` / `DecisionRecorded` / `InboxItemTriaged` などで projector が embed job を enqueue |
| **Bulk rebuild** | `opshub embeddings rebuild [--entity-type=...] [--model=...]` |
| **Model version 変更** | config の `model_version` が変わったら次回 rebuild で全件 re-embed |
| **Throttling** | 短時間に同 entity が複数回更新されたら最終状態のみ embed (debounce) |

embed 自体は **同期 service ではなく非同期 job queue** で処理する (Phase 4 設計確定時に詳細)。Phase 1 では interface のみ。

### 7. Recall インターフェース (Phase 4)

CLI 設計の方針のみここで決め、詳細は Phase 4 で詰める:

```bash
opshub recall "認証周りの先週の決定"                 # 自然言語検索
opshub similar task <task-id> [--k=10]               # 類似 entity
opshub recall --entity-type=decision --filter='status="active"' "..."  # SQL filter 合成
```

projection と JOIN できる前提を活かし、**vector + SQL filter の hybrid search** を中核機能とする。

## Consequences

### Positive

1. **local-first 原則の一貫性を保てる** — local embedder の道を Phase 4 まで開いたまま設計可能
2. **配布が軽い状態を維持** — core dep が ML フリーなので、Homebrew / PyInstaller / Docker の道が閉じない
3. **backend 乗り換えコストが小さい** — `Embedder` interface に閉じているので、`OpenAI → BGE-M3` 等の移行が config 変更 + rebuild で完結
4. **モデル進化に追従可能** — `model_id` + `model_version` 列で増分 re-embed と新旧並列保持が可能
5. **ADR-0005 違反の予防** — embed 対象を summary 系に限定するルールが Phase 1 から interface に現れる
6. **Phase 4 着手前から `embeddings` テーブル骨格があるため、event 駆動 refresh の hook 配線を Phase 2-3 で先回り可能**

### Negative / Trade-offs

1. **抽象レイヤの先行投資** — Phase 1 時点で使わない `Embedder` / `VectorStore` Protocol を書く。死蔵リスクあり (緩和: 100 行程度の Protocol のみで実装は Phase 4)
2. **default backend を決め切れない** — Phase 4 着手時に再度判断ポイントが立つ (緩和: 本 ADR の Open Question で明示)
3. **embedder ごとに依存セットが増える** — `optional-dependencies` のエントリ数が膨らむ (緩和: `all` extras を用意して install UX を 1 行に)
4. **設定駆動 backend は誤設定リスク** — config と実 vector の `model_id` がズレると recall 結果が劣化 (緩和: query 時に `model_id` 一致を強制、不一致は警告で rebuild 促す)
5. **event 駆動 refresh の負荷** — 高頻度 event でも embed が回ると CPU / API コスト増 (緩和: debounce + batch、Phase 4 で確定)

## 軽減策

1. **Protocol を Phase 1 で書き、CI で interface 安定性を担保** — Phase 4 で実装するとき interface 変更が起きないよう、Protocol を unit test で freeze
2. **`opshub embeddings status` を Phase 1 から CLI に追加** — backend / model / dim / 件数を表示 (空でも OK)。実機能なしでも観測ポイントを先に置く
3. **`config.toml` の `[embedding]` セクションを Phase 1 でパース可能に** — `disabled` だけサポート、Phase 4 で `local` / `openai` を有効化
4. **モデル選定基準を runbook 化** — Phase 4 着手時の判断材料として「日本語/英語 mix の品質」「コスト」「次元数とストレージ」を比較する手順を `docs/runbook/embedding-model-selection.md` に用意 (Phase 3 末)

## Alternatives Considered

### 1. API embedder のみ (OpenAI / Voyage / Cohere に固定)

却下理由:

- local-first 原則 (Principles 1) と衝突。ネットワーク断 / 契約終了で recall が完全停止
- 機密データを含む summary が外部 API に流出するリスク
- コストが規模に比例 (個人 operational memory でも数万件 × re-embed で発生)

### 2. Local embedder のみ (sentence-transformers / BGE-M3 等に固定)

却下理由:

- core install が ~500MB-2GB に肥大化。配布弱点が顕在化 (ADR-0001 Negative)
- CPU 環境では初回 embed が遅い (GPU 前提にできない)
- 品質が API モデルに劣るケース (日本語の細かいニュアンス、code 検索) で逃げ場がない

### 3. 抽象レイヤを作らず、Phase 4 で具象に着手

却下理由:

- Phase 4 で「やはり backend 切替したい」となった時の改修コストが大きい
- `services/` / CLI / projection が具象 embedder にカップリングする
- ADR-0001 の「local embedding の道を残す」根拠が薄れ、Python 採用の合理性が弱まる

### 4. 複数 vector store の並列運用 (sqlite-vec + LanceDB 等)

却下理由:

- ADR-0002 の「単一 SQLite で event store + projection を完結」原則と矛盾
- バックアップ / replay の対象が増える
- OpsHub の規模 (~100 万 vector 想定) では sqlite-vec で十分

### 5. 単一 model + version 列なし (シンプル化)

却下理由:

- モデル変更時に全件 re-embed しか選択肢がなくなる
- 新旧モデルの A/B 比較ができない
- 数千〜数万件規模でも rebuild は数十分単位、ユーザー操作を阻害

### 6. Embed 対象に event payload も含める

却下理由:

- event は immutable で量が多い (10 万 event / 100 万 event 規模)
- event の検索ニーズは projection 検索で代替可能 (`opshub event list --task-id=...`)
- ADR-0002 の「event は authoritative、projection は派生」原則的に、検索は projection を貫くべき

### 7. Hybrid (短期: API、長期 archive: local)

却下理由:

- 同一 entity が時期によって異なる embedder で embed されると recall 結果が不安定
- 切り替えロジックが複雑
- 代わりに本案 (Pluggable + 設定駆動) で十分柔軟

## 決定の確定 (Phase 4 後追い)

Phase 4 sub-issue A-D (PR #63-#74) の実装で本 ADR の Open Questions 1-2 を以下のとおり確定した:

1. **Phase 4 default backend** (旧 Open Q 1) → **`disabled`** を維持。理由は `local` 採択時に `sentence-transformers` (~500MB-2GB の torch を内包) を pull する必要があり、CI / 初回 install 体験を軽く保つには opt-in が妥当との判断 (`docs/phase-4-plan.md` §1 #3)。operator が `~/.config/opshub/config.toml` の `[embedding] backend` を `local` / `openai` / `voyage` に切替えて初めて embedding が有効化される
2. **推奨モデル** (旧 Open Q 2) → backend ごとに以下を default:
    - `local` = `BAAI/bge-m3` (1024-dim、多言語; `LocalSentenceTransformerEmbedder` で `normalize_embeddings=True`)
    - `openai` = `text-embedding-3-small` (1536-dim; `OpenAIEmbedder`)
    - `voyage` = `voyage-3` (1024-dim; `VoyageEmbedder`)

   `[embedding] model_id` と `[embedding] dimensions` を config で上書き可能。dim 切替時は `embeddings_vec_<backend>` 仮想テーブル (本 ADR §1 #5 で resolution 済) が dim 別に分かれているため、運用は backend を切替えて `opshub embeddings rebuild` を再走する手順 (Phase 4 plan §3 機能 #10、PR D1 の `test_phase4_lifecycle.py::test_backend_switch_requires_rebuild` で pinning)

## Open Questions

Phase 4 内で確定しなかった項目 (Phase 5+ に持ち越し):

1. **次元数 / 量子化** — 1024d / 1536d float32 で Phase 4 稼働中。規模が伸びたら int8 / binary 量子化を導入する閾値を決める
2. **長文 chunk 戦略** — 1 entity = 1 vector が default。`briefings` のように長文化しうる対象は chunk + max pooling を Phase 5 で評価 (briefing 自動生成と合わせて確定)
3. **多言語専用 embedder か単一 embedder か** — Phase 4 MVP は multilingual モデル 1 本 (bge-m3) で運用。言語別 embedder への分岐は需要が立ってから
4. **re-embed throttling のパラメータ** — Phase 4 MVP は CLI-driven rebuild のみで throttling 不要。event 駆動自動 embed (Phase 5) で debounce 窓 / batch size / 優先度キューを確定
5. **Recall CLI の hybrid search 構文** — `opshub recall --type X --state Y` は Phase 4 で確定。任意 SQL filter (`--filter ...`) は Phase 5+ で SQL injection 防御方針と合わせて検討
6. **Embedding を CI で再現可能にするか** — Phase 4 では API embedder は network mock、local embedder は `pytest.importorskip` で skip して回避 (`tests/integration/test_phase4_lifecycle.py` は decimal-deterministic な stub Embedder を `monkeypatch` で注入)。長期的に test fixture をどう作るかは Phase 5+

## Validation

Phase 4 sub-issue A-D (PR #63-#74) で 3 backend (local sentence-transformers, OpenAI, Voyage) と sqlite-vec backed VectorStore を実装し、本 ADR の Pluggable Embedder + Pluggable VectorStore 設計が end-to-end で機能することを検証した。Phase 4 MVP では:

- `opshub embeddings rebuild` で task / decision / inbox_item / source の summary を embed (`tests/integration/test_phase4_lifecycle.py::test_embeddings_rebuild_and_status_e2e`)
- `opshub recall "<query>"` で hybrid semantic search (vector + SQL filter)、`--type` / `--state` / `--format json` の各 flag を pin (`test_recall_returns_semantic_hits_e2e`)
- `opshub embeddings find-duplicates` で offline 近傍検索 (`test_find_duplicates_e2e`)
- config の `[embedding] backend` 切替で具象 Embedder factory が自動で切替 (`vectors/factory.py::build_embedder`)、backend 切替直後の recall は `(model_id, model_version)` 不一致を検知して fail-fast (`test_backend_switch_requires_rebuild`)
- Backend ごと dim 別 vec0 table (`embeddings_vec_local` 1024 / `embeddings_vec_openai` 1536 / `embeddings_vec_voyage` 1024) で複数 backend 並列保持の余地を schema レベルで確保 (Phase 5+ で実 routing 配線)

Event-driven 自動 embed (projector hook) / briefing 自動生成 (LLM 呼び出し) / `links` projection 本実装は Phase 5 以降の outlook (`docs/phase-4-plan.md` §6)。

## 関連

- [Principles 1 (Local-first)](../principles.md)
- [Principles 6 (External Content Minimization)](../principles.md)
- [Principles 9 (Phased Delivery)](../principles.md)
- [Architecture 2.6 (Vector Layer)](../architecture.md)
- [ADR-0001: Python Stack](0001-python-stack.md)
- [ADR-0002: Event-Sourced Architecture](0002-event-sourced-architecture.md)
- [ADR-0005: External Content Minimization](0005-external-content-minimization.md)
- [ADR-0007: Single Python Package, defer Monorepo](0007-single-python-package.md)
- 知識 MCP: `tools/sqlite-vec` (該当ナレッジ未収録、追加候補)
