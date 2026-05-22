# Phase 9 Implementation Plan

> Status: Draft (planning). Last reviewed: 2026-05-23. Scope: Local-filesystem-backed Connector Layer = ADR-0018 (新規) + `sources.fingerprint` 列 (migration 0017) + `SourceObserved.fingerprint` field (backward-compat 追加) + `box_drive` connector (scanner + mapper + connector) + `core/platform.py` (WSL2 検出 helper) + `opshub connector sync box_drive` 経路 + closeout。Box Drive Web API が使えない operator (mountvol で `/mnt/b` にマウント済の WSL2 / macOS) を対象。Multi-machine sync (principles.md §Open Q #5) は Phase 10 候補にスライド。

Phase 9 の目的は **ローカル FS にマウントされた SaaS sync client を source として扱う新パターン** を Phase 1-8 の foundation 上に追加すること。第一弾として Box Drive を実装する。Phase 7 で Box を Web API 経由 (`box_event`) で取り込む経路を確立済だが、developer app 登録不能 / OAuth 不可な企業環境では Web API が使えず、**Box Drive デスクトップクライアントは使える** ケースがある。Phase 9 はその受け皿。

同パターンは Dropbox / OneDrive client / Google Drive for desktop / iCloud Drive にも自然に汎化できるが、**MVP は Box Drive 専用**。2 vendor 目の実装時に `local_drive/` 共通基底を抽出する判断は Phase 9.x に持ち越し（premature abstraction 回避）。

## 1. 着手前に解消する TODO

Phase 9 着手前に解消が必要な事項は **なし**。Phase 1-8 で確立した実装契約 (uow_factory / `EventStore.append(event, conn)` / `Projector.apply(event, conn)` / `projections/registry.all_projections()` SSOT / `AllEvent` discriminated union / `cli/* import` whitelist / atomic failing-projector test / `core/secrets` + ADR-0014 token storage / Pluggable backend Protocol freeze / `core/sanitise.sanitise_error_message` / Connector framework + 4 connectors + `links` projection) は Phase 9 も全て継承する。

**確定済み事項** (Phase 9 着手前に確定):

1. **Phase 番号**: Phase 9（top-level）。Phase 8.x 枠ではない（新 ADR + sources projection schema 変更 + 新 connector category のため）
2. **Scope の絞り込み**: Phase 9 MVP = ADR-0018 + `sources.fingerprint` 列 + `box_drive` connector + `core/platform.py` + `opshub connector sync box_drive` + closeout。**Watch mode (filewatch / inotify / FSEvents / CldAPI callback) は Phase 9 scope 外**（scan-only で identity 戦略を pin する）。Multi-machine sync / 追加 FS connector / `local_drive/` 共通基底 / `opshub source list --stale` は Phase 9.x / 10
3. **Operator precondition**: Box Drive がインストール済みかつ OS から FS として可視であること。WSL2 では `mountvol B: \\?\Volume{GUID}\` + `wsl --shutdown` を operator が事前に実施し `/mnt/b` を出現させる（opshub は automate しない）。手順は `docs/box-drive-setup.md` (C1 PR で新設) に記載
4. **Identity strategy**: `external_id = rel_path`（root_path 相対パス、SHA hash しない grep 可能形式）。rename / move は「旧 path の SourceObserved が止まり、新 path で SourceObserved 発火」として観測される MVP 制限を ADR-0018 に明記。xattr / ADS ベースの安定 Box item ID は Phase 9.x 候補
5. **Diff detection**: スキャナは事前に `sources` projection から `(connector_name="box_drive", external_id, fingerprint)` を一括 SELECT → in-memory dict 構築 → walk 中の各 file について `fingerprint = f"{size}:{mtime_ns}"` を計算 → prior fingerprint と一致しない / 不在の file のみ `SourceObserved` を発行。**変更なし file の event noise を抑える**
6. **削除検知**: Phase 9 MVP では **追跡しない**。Drive から消えた file は次回スキャンで再 observe されないだけ。`sources` projection に stale row が残るのは event-sourced append-only の自然な帰結。`opshub source list --stale` で炙り出す機能は Phase 9.x
7. **`stat()` 非 hydrate**: Microsoft CldAPI / macOS File Provider Extension の documented contract に依拠（`os.stat` は metadata access のみで CldAPI hydration を triggered しない）。**`open()` / magic bytes 読み出しは禁止** を ADR §不変条件で pin、`tests/integration/test_box_drive_no_hydration.py` を `OPSHUB_BOX_DRIVE_TEST_ROOT` env 設定時のみ実行する CI 持続検証として配置
8. **Cursor**: `connector_cursors` projection に `box_drive:last_scan` キーで「最終 scan の ISO timestamp」を記録（informational only、resume logic には使わない。resume は sources projection との diff で行う）
9. **Excludes 統合**: ADR-0005 で言及されている `~/.config/opshub/excludes.yaml` は現時点で未実装。Phase 9 は `opshub.toml` 内 `[connectors.box_drive] exclude_globs = [...]` で inline 配置。`excludes.yaml` の共通機構化は Phase 9.x で全 connector 横断のリファクタとして
10. **`source_type`**: `box_drive_file`（既存 `box_event` と分離、二重取り込みを許容、Phase 8 `links` projection で束ねる将来余地）
11. **`root_path` platform default**:

    | Platform | default | 備考 |
    |---|---|---|
    | Linux (WSL2 検出: `/proc/sys/kernel/osrelease` に `microsoft` 含む) | `/mnt/b` | operator が事前 `mountvol B: \\?\Volume{GUID}\` 設定済の前提 |
    | macOS | `~/Box` | Box Drive デフォルトインストール先 |
    | Linux native | 未対応 (`ConfigError` で fail-fast) | Box Drive Linux client なし |

    Windows native は opshub 全体の前提（POSIX-only、`pyproject.toml` classifier `Operating System :: POSIX`）により対象外
12. **`enabled = false` default**: Phase 7 全 connector と同様、`[connectors.box_drive] enabled = false` をデフォルト。operator が明示 opt-in
13. **`auth set connector:box_drive` の挙動**: paste-code flow を持たないので、実行時に「box_drive は opshub.toml の `[connectors.box_drive] root_path` 設定のみで OK」と actionable error メッセージで reject。`opshub connector list` には表示
14. **新 event family は導入しない**: `Phase9Event` は作らない。`SourceObserved` への `fingerprint: str | None = None` field 追加のみ（ADR-0002 §4 「新 field 追加は OK」の backward-compat 範囲）。schema_version は据え置き 1
15. **Migration**: head は現在 0016 (links projection)。Phase 9 最初の migration は **0017** で `sources.fingerprint TEXT NULL` を追加

## 1.1 Prep PR (Phase 1-8) で確立した実装契約 (Phase 9 全 PR が継承)

- 新規 service は `uow_factory: Callable[[], ContextManager[Connection]] | None = None` を constructor で受け、event append + projection apply を 1 transaction にまとめる (PR #26 契約)
- 新規 projection / projection 変更は `projections/<entity>.py` で Table を `opshub.db.schema.metadata` に登録 + `projections/registry.all_projections()` に追記 (本 phase は projection 追加なし、既存 sources projection に列追加のみ)
- 新 event family は作らない (本 phase は SourceObserved への field 追加のみ、`AllEvent` 変更なし)
- 新規 CLI subcommand module は不要 (本 phase は既存 `cli/connector.py` の sync 経路に乗る、registry 登録のみ)
- `connectors/<name>/` の module-level import は `__future__` / `typing` / `pathlib` / stdlib のみ。`os` / `hashlib` は stdlib なので OK、SDK 依存なし
- 新規 connector は registry 登録 + atomicity test 追加
- `core/platform.py` (新設) は `__future__` / `pathlib` / `sys` のみ import (cold-start budget 維持)

## 2. Phase 9 Commit 順序

Conventional Commits 準拠。1 step = 1 PR = 1 commit (squash 後) を厳守。**Phase 9 plan PR (本ドキュメント commit) は Phase 9 着手前の prep PR として別途**。

### 2.1 Sub-issue A: Foundation (2 PR)

| # | Commit | 概要 | Sub-issue |
|---|---|---|---|
| A1 | `docs(adr): adr-0018 local-filesystem-backed connector` | `docs/adr/0018-local-filesystem-backed-connector.md` 新設。Status: Accepted。決定 9 件: (a) `Connector` Protocol は変えず auth layer を OS-level Box Drive 認証への依存に置換、(b) Source は `os.stat()` metadata のみ、`open()` / magic bytes read 禁止（CldAPI hydration 防止）、(c) Identity = `rel_path`（path-as-id、rename = 旧停止+新発火）、(d) Diff detection は `sources` projection の `fingerprint` 列でスキャナ in-memory 比較、(e) 削除追跡なし（stale row として残す、Phase 9.x `--stale` flag 候補）、(f) `root_path` platform-aware default (WSL2=`/mnt/b` / macOS=`~/Box` / Linux native 未対応)、(g) Excludes は `opshub.toml` inline、`~/.config/opshub/excludes.yaml` 共通機構化は Phase 9.x、(h) Operator precondition (`mountvol` 設定) は opshub 範囲外、(i) Watch mode は Phase 9.x 持ち越し（scan-only で identity 戦略を pin）。decisions-log.md entry | A |
| A2 | `feat(sources): add fingerprint column + migration 0017` | Migration `0017_add_fingerprint_to_sources.py` (revision `0017`, down_revision = `0016`) で `sources.fingerprint TEXT NULL` を追加。`SourceObserved` event に `fingerprint: str \| None = None` field を追加（schema_version 据え置き 1、ADR-0002 §4 「新 field 追加は OK」）。`SourceService.observe(..., fingerprint: str \| None = None)` keyword arg 追加。`SourcesProjection` projector が `fingerprint` を upsert (`fingerprint=None` は NULL 書き込み、既存 connector は挙動変化なし)。`tests/unit/projections/test_sources.py` に既存 connector backward-compat + 新規 fingerprint write の 2 test 追加。`tests/integration/test_phase4_lifecycle.py` 等の既存 recall flow が壊れないことを CI で pin | A |

### 2.2 Sub-issue B: Scanner + Mapper + Connector (2 PR)

| # | Commit | 概要 | Sub-issue |
|---|---|---|---|
| B1 | `feat(core): platform detection + box_drive scanner` | `src/opshub/core/platform.py` 新設: `detect_platform() -> Literal["wsl2", "macos", "linux", "unsupported"]` (`/proc/sys/kernel/osrelease` の `microsoft` 文字列 + `sys.platform` で判定)、`box_drive_default_root_path(platform) -> Path \| None` (WSL2=`/mnt/b`、macOS=`~/Box` 展開、その他=None)。`src/opshub/connectors/box_drive/__init__.py` + `src/opshub/connectors/box_drive/scanner.py` 新設: `BoxDriveScanner(root_path, *, exclude_globs, max_depth, follow_symlinks, max_files, prior_fingerprints: dict[str, str])`。`scan() -> Iterator[ScannedFile]` が `os.scandir()` 再帰 walk → `ScannedFile(rel_path, size, mtime_ns, fingerprint)` を yield → prior_fingerprints と比較し変更ありの file のみ含める。`open()` 禁止 invariant を test で pin (`unittest.mock.patch("builtins.open", side_effect=AssertionError("forbidden"))` で scan 全 path 実行)。symlink loop / max_depth / EACCES / `root_path` 不存在 / 巨大 tree (`max_files` 上限) の各経路を unit test で網羅。CldAPI 非 hydration の contract test (`tests/integration/test_box_drive_no_hydration.py`、`OPSHUB_BOX_DRIVE_TEST_ROOT` 設定時のみ実行 = real env opt-in) を追加 | B |
| B2 | `feat(connectors/box_drive): mapper + connector + settings` | `src/opshub/connectors/box_drive/mapper.py` + `src/opshub/connectors/box_drive/connector.py` 新設。`map_scanned_file -> SourceObserved` (`source_type="box_drive_file"`, `external_id=rel_path`, `summary=f"path: {rel_path}"` 200 char cap, `url=f"file://{abs_path}"`, `actor="box_drive:local"`, `fingerprint=f"{size}:{mtime_ns}"`, `occurred_at=mtime` を `datetime.fromtimestamp` 経由 UTC 化)。`BoxDriveConnector` が `Connector` Protocol を実装: (1) settings から `root_path` 解決 (None なら `box_drive_default_root_path`)、(2) `sources` projection から prior fingerprints を SELECT、(3) `BoxDriveScanner.scan(prior_fingerprints=...)` を回す、(4) 各 `ScannedFile` を mapper 経由で `SourceObserved` 化し `context.source_service.observe(..., fingerprint=...)` で append (atomic UoW、PR #26 契約)。`core/config.py` に `BoxDriveConnectorSettings` (`enabled: bool=False`, `root_path: Path \| None = None`, `max_depth: int = 16`, `max_files: int = 100_000`, `follow_symlinks: bool = False`, `exclude_globs: list[str] = []`) を追加し `ConnectorSettings.box_drive` を生やす。`connectors/_registry.py` への登録、`cli/connector.py` の `auth set connector:box_drive` 経路で actionable error 返却 (paste-code 不要、`opshub.toml` 設定を案内)。`tests/unit/connectors/box_drive/` 一式 + atomic failing-projector test | B |

### 2.3 Sub-issue C: CLI integration + Phase 9 closeout (1 PR)

| # | Commit | 概要 | Sub-issue |
|---|---|---|---|
| C1 | `feat(cli): box_drive sync + phase 9 closeout` | `opshub connector sync box_drive` が既存 `cli/connector.py` の sync 経路で動作することを e2e で pin（新 CLI module 不要、registry 経由 dispatch）。`opshub connector list` に `box_drive` が `enabled=false` で表示されることを pin。`tests/integration/test_phase9_lifecycle.py`: tmp dir を `root_path` として `BoxDriveConnector.sync` を 2 回呼び (1 回目で全 file が SourceObserved、2 回目で 1 file modify + 1 file 追加 + 1 file 削除 → modify+追加のみ event 発火、削除は無視) `sources` projection が正しく追従することを e2e で pin。Real Box Drive 環境は使わず tmp dir で skipless test。docs: README に `opshub connector sync box_drive` 追記 + `docs/box-drive-setup.md` 新設 (WSL2 `mountvol` 手順 + qiita 記事 link + macOS 既定パス)。AGENTS.md / CLAUDE.md / docs/principles.md (§9 Phase 9 = ✅ Complete、§Open Q #5 Multi-machine sync を「Phase 10+ 候補」に表現変更) / docs/architecture.md (§2.1 Connector Layer に FS-backed vendor 行追加 + §2.11 (新規) Local-filesystem-backed Connector Pattern 節を追加) / docs/repository-structure.md (`[P9]` annotation: `core/platform.py` / `connectors/box_drive/` / migration 0017) / docs/decisions-log.md (Phase 9 entry) / ADR-0018 Validation 追記 (test ファイルへの reference)。Phase 7 `box` connector との関係 (二重取り込み許容、`source_type` 分離) を ADR-0018 §関連で明示 | C |

= 合計 **5 PR** (A 2 + B 2 + C 1)。

**Wave 構成** (DAG):

```text
Wave 1: A1 ADR-0018 → 1 並列 (sequential foundation)
Wave 2: A2 sources.fingerprint + migration 0017 → 1 (A1 依存)
Wave 3: B1 core/platform + box_drive scanner → 1 (A2 依存)
Wave 4: B2 mapper + connector + settings → 1 (B1 依存)
Wave 5: C1 CLI + closeout → 1 (B2 依存)
```

= 5 wave、**全 sequential**。Phase 7 connector 3 並列 (4 wave / 10 PR) や Phase 8 (6 wave / 9 PR) と比べてコンパクト。Connector category が単一で並列化余地が少ないため。

## 3. 各 Sub-issue の Definition of Done

### Sub-issue A — Foundation

- [ ] ADR-0018 Accepted + decisions-log.md entry
- [ ] Migration 0017 で `sources.fingerprint TEXT NULL` 列を追加、`alembic upgrade head` で apply 可能 + `alembic downgrade -1` で revert 可能
- [ ] `SourceObserved` に `fingerprint: str | None = None` field 追加 (schema_version 据え置き 1)
- [ ] `SourceService.observe` に `fingerprint=None` keyword arg 追加
- [ ] `SourcesProjection` projector が `fingerprint` を upsert する (既存 connector は None で挙動変化なし)
- [ ] 既存 4 connector (github / slack / ms365 / box) の test が全通過 (backward-compat)
- [ ] 既存 recall flow integration test (`test_phase4_lifecycle.py` 等) が全通過

### Sub-issue B — Scanner + Mapper + Connector

- [ ] `core/platform.py` の `detect_platform()` が WSL2 / macOS / linux / unsupported を正しく返す (mock based unit test)
- [ ] `box_drive_default_root_path()` が WSL2=`/mnt/b`、macOS=`~/Box` 展開、その他=None を返す
- [ ] `BoxDriveScanner.scan()` が `os.scandir()` walk で `ScannedFile` を yield、prior_fingerprints 一致は skip
- [ ] `open()` 禁止 invariant test pass (`unittest.mock.patch("builtins.open", side_effect=AssertionError("forbidden"))` で scan 全 path 実行して fail しないこと)
- [ ] symlink loop / max_depth / EACCES / 不存在 root_path / max_files 上限の各経路 unit test
- [ ] CldAPI non-hydration contract test が `OPSHUB_BOX_DRIVE_TEST_ROOT` 設定時に pass、未設定時は skip
- [ ] `map_scanned_file` が `SourceObserved` を生成、`summary` 200 char cap pin、`fingerprint=f"{size}:{mtime_ns}"` 形式
- [ ] `BoxDriveConnector.sync` が `Connector` Protocol に適合、`registry` 登録
- [ ] `connectors/_registry.py` 経由で `BoxDriveConnector` が `name="box_drive"` で取得可能
- [ ] `BoxDriveConnectorSettings` が `enabled=False` default、`root_path=None` の場合は platform default を解決
- [ ] `auth set connector:box_drive` が actionable error で reject (paste-code 不要のため)
- [ ] atomic failing-projector test 1 件追加（observe 失敗時に scan 全体が rollback すること）

### Sub-issue C — CLI + closeout

- [ ] `opshub connector sync box_drive` 動作（既存 dispatcher 経由、新 CLI module なし）
- [ ] `opshub connector list` に `box_drive` が表示される
- [ ] `tests/integration/test_phase9_lifecycle.py` が tmp dir e2e で 2-pass sync の差分検出 (modify / 追加検知 + 削除無視) を pin
- [ ] M6 cold-start guard 順守 (`connectors/box_drive/*` + `core/platform.py` の module-level import が whitelist 範囲)
- [ ] docs: README / `docs/box-drive-setup.md` / AGENTS.md / CLAUDE.md / principles.md / architecture.md / repository-structure.md / decisions-log.md
- [ ] ADR-0018 Validation 追記 (test ファイルへの reference)
- [ ] `time opshub --help` ≤ 300ms 維持 (M6 guard)

## 4. Open Questions

Phase 9 着手時点で未確定、本 plan 内で確定すべきもの:

1. **`url = file://...` vs `url = None`** — Box Drive は `file://` でローカルパスを開けるが、機密性のあるパスを projection に保持してよいかは ADR-0005 (External Content Min) の解釈次第。**現案**: `file://` を入れる（path は既に summary に入っているので二重露出にならず、`opshub source open <id>` の UX を保つ）。A1 ADR で確定
2. **`fingerprint` の構成要素** — `f"{size}:{mtime_ns}"` を default。OS の clock skew や手動 `touch` で false positive のリスク。**現案**: size + mtime_ns で十分（agent が観測する観点では「mtime が動いた = 変更」で正解）。SHA-256 など内容 hash は本文 read を要するため ADR-0018 §不変条件 (b) 違反、選ばない
3. **`root_path` 複数指定** — operator が Box Drive 外に「外部共有された Box フォルダ」を別パスでマウントするケース。**現案**: Phase 9 MVP は単一 `root_path` のみ。複数対応は `[connectors.box_drive.<name>]` 形式で Phase 9.x

Phase 9 内では確定しなくてよい (Phase 9.x / 10 持ち越し):

1. **Watch mode** (`watchdog` / inotify / FSEvents / CldAPI callback) — scan mode で identity 戦略を pin してから着手
2. **xattr / ADS ベースの安定 Box item ID** — rename を identity 保持して扱いたくなった段階で再評価
3. **`local_drive/` 共通基底抽出** — 2 vendor 目 (OneDrive client / Dropbox / etc.) を実装する時点で
4. **`opshub source list --stale`** — `sources` projection と FS の age gap を炙り出す CLI
5. **`~/.config/opshub/excludes.yaml` 共通機構** — 全 connector 横断の exclude 設定統合
6. **Multi-machine sync** (principles.md §Open Q #5 closeout) — Phase 10 候補

## 5. Phase 9.x / 10 outlook

Phase 9 完了直後の候補:

- **Watch mode** (filewatch backend): Phase 9.x。inotify / FSEvents / CldAPI callback を抽象化
- **追加 FS-backed connector**: OneDrive client / Dropbox / Google Drive for desktop / iCloud Drive → 2 vendor 目で `local_drive/` 共通基底抽出
- **xattr-based stable identity**: rename 保持が必要になった段階で
- **`opshub source list --stale`**: sources projection の age による stale 検出
- **`~/.config/opshub/excludes.yaml` 共通機構**: 全 connector 横断 (Phase 7 Box の event filtering / Phase 9 box_drive の path filtering 統合)
- **Multi-machine sync** (principles.md §Open Q #5 closeout、Phase 10 候補): litestream / Turso / event-sourced export-import + ADR-0019

Phase 9.x / 10 着手時に連動して見直すべき docs: principles.md §1 (Local-first、FS-backed connector が増えた場合の前提整理) / §6 (External Content Min、`fingerprint` メタデータの保持範囲再確認) / ADR-0010 (Connector Contract、FS-backed pattern の Validation 拡張) / ADR-0018 (本 phase で新設、Phase 9.x で Validation 追記)。

## 6. 参考: Spike 不採用の根拠

Phase 9 は **spike なしで設計確定** している。各 open question を spike 経由ではなく documented OS contract / 設計選択で潰した:

| 元 open question | spike なしで決められる理由 |
|---|---|
| WSL2 から Box Drive 参照可能か | qiita 記事 (https://qiita.com/himacreation/items/e375e010d670d756e754) で `mountvol B: \\?\Volume{GUID}\` + `wsl --shutdown` → `/mnt/b` 経路が確立。**operator setup として `docs/box-drive-setup.md` に外出し** |
| `stat()` が CldAPI placeholder を hydrate するか | Microsoft / Apple の OS contract が documented (metadata access では non-hydrating)。**contract test を CI に常駐させて持続検証** (B1 PR の `tests/integration/test_box_drive_no_hydration.py`) |
| Box item ID xattr 取得可否 | **設計から外す**。`external_id = rel_path` 一本で MVP、rename = 旧停止+新発火 制限を ADR で明記 |
| 100k+ files scan throughput | order-of-magnitude reasoning で 5-10 秒。`max_files` を escape hatch に。実 user で問題化したら chunking PR |

## 関連

- principles.md §9 (Phased Delivery)、§Open Q #5 (Multi-machine sync → Phase 10+ 候補に表現変更予定)
- architecture.md §2.1 (Connector Layer、FS-backed vendor 追加予定) / §2.11 (新規、Local-filesystem-backed Connector Pattern)
- ADR-0001 (Python Stack、POSIX-only 前提)
- ADR-0002 (Event-Sourced、§4 「新 field 追加は OK」backward-compat)
- ADR-0005 (External Content Min、`stat` のみ + `open()` 禁止 不変条件)
- ADR-0010 (Connector Contract、`Connector` Protocol 再利用 + auth layer を OS 依存に置換)
- ADR-0018 (Local-filesystem-backed Connector、本 phase A1 で新設)
- Phase 1 #3 / Phase 2 #23 / Phase 3 #43 / Phase 4 #62 / Phase 5 #81 / Phase 6 #99 / Phase 7 #113 / Phase 8 #128 (全 closed)
