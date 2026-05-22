# 0019. Local-filesystem-backed Connector (box_drive)

- Status: Accepted
- Date: 2026-05-23
- Deciders: ozzy

## Context

Phase 7 (epic #113) で OpsHub は GitHub / Slack / Microsoft 365 / Box の 4 connector を **vendor Web API 経由** で実装し、`Connector` Protocol (ADR-0010) + `core/secrets` + keyring 経路 (ADR-0014) + External Content Minimization (ADR-0005) を確立した。Box (`connectors/box/`) は Box Platform API + OAuth refresh token で events stream を取り込み、`sources` projection に `source_type="box_event"` で persist する経路を確立済。

しかし以下の企業環境では、上記 Web API 経路が **そもそも使えない**:

1. **Developer app 登録不能** — Box admin が「外部 developer app の登録を承認しない」 IT policy を敷いている。OAuth client_id 取得が阻止される
2. **OAuth grant 不可** — Enterprise Box の workspace policy が外部 OAuth grant を拒否し、refresh token を取得できない
3. **Network egress 制限** — `api.box.com` への egress が white-list policy で拒否されている

これらは「Slack / GitHub の場合は user token / PAT で同等の脱出経路があるが、Box は OAuth 一択」という Box 特有の状況と組み合わさり、Phase 7 `box` connector を運用に乗せられない operator が出る。

一方で、**Box Drive デスクトップクライアントは使える**ケースが多い。Box Drive は Microsoft CldAPI (Cloud Files API) / macOS File Provider Extension 経由で「Box workspace 全体をローカル FS にマウントする」官公庁・大企業 IT で広く許可されている同期クライアントで、内部的に Box Platform API を OS-level 認証で叩く (operator が Web Box にログインしている状態で OS daemon が token を保持)。operator は Web API 経路を持たなくても、`/mnt/b` (WSL2) / `~/Box` (macOS) を walk すれば Box の content metadata に到達できる。

この経路を OpsHub の connector framework に乗せる第一の論点は **「ファイル本文を read しない」契約**である。CldAPI placeholder (Microsoft) / File Provider Extension placeholder (macOS) は metadata access では non-hydrating だが、`open()` / magic bytes / shebang 検査などの本文 read で **hydration が triggered** され (= 内容が SaaS 側からダウンロードされ)、(i) network egress を発生させて IT policy 違反、(ii) Box Drive cache を肥大化、(iii) OS notification を発火、(iv) ADR-0005 (External Content Minimization) の full body 非保持原則に違反する。`os.stat()` の metadata-only contract に依拠して **scanner が `open()` / magic bytes read を構造的にできない** 設計を pin する必要がある。

第二の論点は **identity 戦略**である。Box の安定 item ID は Box Platform API 経由でしか取れず (xattr / Alternate Data Streams で OS 側に persist する経路は Box Drive がサポートしない)、Phase 9 MVP では使えない。`external_id = rel_path` (root_path 相対パス) を採択するが、rename / move で identity が壊れる (旧 path で SourceObserved が止まり、新 path で別 source として SourceObserved 発火) MVP 制限を明示的に pin する必要がある。安定 ID へのマイグレーションは Phase 9.x 候補。

第三の論点は **diff detection** である。100k+ files の scan で毎回全 file を SourceObserved すると event log noise が膨大になるため、scanner が「変更ありの file のみ」event 化する仕組みが必要。`fingerprint = f"{size}:{mtime_ns}"` を `sources` projection に列追加して保存し、scan 開始前に prior fingerprints を一括 SELECT → walk 中に比較する経路を採択する (本文 read 不要、stat() のみで完結)。

第四の論点は **削除追跡**である。Drive から file が消えた場合に SourceDeleted のような event を発行するか否か。Phase 9 MVP では追跡しない選択を取る。理由: (i) 削除検知は scan 開始時の `sources` projection 全 row と walk 結果の symmetric diff が必要で、scan 単純性を破壊する、(ii) event-sourced append-only の自然な帰結として「過去観測された source が現在の Drive に無い」状態は stale row として残せる、(iii) operator が炙り出したいケースは Phase 9.x `opshub source list --stale` で対応可能。Phase 9 MVP は scan-only / additive。

第五の論点は **platform default**である。WSL2 で Box Drive を参照するには `mountvol B: \\?\Volume{GUID}\` + `wsl --shutdown` の operator setup が必要で ([qiita 記事](https://qiita.com/himacreation/items/e375e010d670d756e754))、これ自体は OpsHub の自動化範囲外。一方で「設定済前提で `/mnt/b` を default」「macOS では `~/Box` を default」「Linux native は Box Drive client がない」を `core/platform.py` で判定する経路は必要。Windows native は opshub 全体の前提 (POSIX-only、`pyproject.toml` classifier `Operating System :: POSIX`) で対象外。

第六の論点は **Watch mode との関係**である。inotify / FSEvents / Microsoft CldAPI callback による push 駆動更新は理想だが、Phase 9 MVP では **scan mode** で identity / fingerprint 戦略を pin する。Watch mode を先に実装すると identity を rename event を保持する形に設計せざるを得ず、本 ADR の rel_path 一本 + rename = 旧停止+新発火 制限が表現できなくなる。scan mode で 1 vendor (box_drive) 分の運用知見を蓄えてから、Phase 9.x で filewatch backend を抽象化する順序を採択。

## Decision

OpsHub に **Local-filesystem-backed Connector pattern** を追加し、第一弾として **box_drive connector** を実装する。`connectors/box_drive/` package を Phase 7 の 4 connector と並列に配置し、`Connector` Protocol (ADR-0010) は変えずに **auth layer を OS-level Box Drive client への依存に置換**する。以下に 9 つの決定を pin する。

### (a) `Connector` Protocol 再利用 + auth layer を OS 依存に置換

`box_drive` connector は ADR-0010 で確立した `Connector` Protocol (sync entry point + `ConnectorContext` 受領 + `SourceService.observe` 経由の event append) をそのまま実装する。Protocol signature は変更しない。

`SaaSAuth` / `core/secrets` / keyring 経路は **使わない**。box_drive は Box Drive デスクトップクライアントが OS 上で稼働して認証済であることを前提とし、OS 側で `/mnt/b` / `~/Box` に FS が visible であれば動作する。

`opshub connector auth set box_drive` の挙動: paste-code flow を持たないため、実行時に「box_drive は `opshub.toml` の `[connectors.box_drive] root_path` 設定のみで OK。OS 側 (WSL2 mountvol / macOS Box Drive install) は opshub 範囲外。詳細は `docs/box-drive-setup.md` を参照」という actionable error message で reject する。`opshub connector list` には `name=box_drive, enabled=<bool>` として表示される。

採用理由:

- Phase 7 4 connector で確立した Protocol を 1 vendor のために変えない (Protocol freeze 維持、ADR-0010 §決定 (a))
- OS-level 認証への依存は connector 1 つ分の特殊化に閉じ込め、framework 全体には漏らさない
- paste-code 不要の actionable error は Phase 7 で確立した auth flow と整合 (auth が要らない vendor も同じ CLI 経路で「不要」と伝える)

### (b) Source は `os.stat()` metadata のみ参照、`open()` / magic bytes read 禁止

`BoxDriveScanner` および mapper は file の **本文を一切 read しない**。`os.scandir()` で directory walk → `entry.stat()` で metadata 取得 (size / mtime_ns / mode / inode) のみ参照する。以下を **不変条件** として ADR レベルで pin:

- `open()` / `read()` / `read_bytes()` / `read_text()` / magic bytes 検査 / shebang 検査 / file content hash (SHA-256 等) を一切実行しない
- file extension / path string / size threshold 等の metadata-only heuristics に限定する
- 本不変条件は Phase 9 B1 PR で `unittest.mock.patch("builtins.open", side_effect=AssertionError("forbidden"))` を scan 全 path に適用する test で構造的に pin する (scan が `open()` を呼んだ瞬間 test が fail)

根拠は ADR-0005 (External Content Minimization) の延長:

- ADR-0005 §決定で「full body を Operational Memory に保持しない」「Connector は summary 化 → event 生成」を pin 済
- box_drive は LLM summary 化経路を持たない (本文 read 自体が禁止) ため、`SourceObserved.summary` は **path string** ベースで生成する (`f"path: {rel_path}"` を 200 char cap、ADR-0005 §決定の summary ≤ 200 chars enforce と整合)
- CldAPI / File Provider Extension は metadata access では non-hydrating contract が documented (Microsoft / Apple OS contract)。`stat()` で hydration が triggered されないことは CI に常駐する contract test (`tests/integration/test_box_drive_no_hydration.py`、`OPSHUB_BOX_DRIVE_TEST_ROOT` env 設定時のみ実行 = real env opt-in) で持続検証する

採用理由:

- IT policy / network egress / Box Drive cache 肥大化 / OS notification 発火 / ADR-0005 違反 のいずれも構造的に発生不能になる
- 本文 read を「やってはいけない」regex review に頼るのではなく、test で `open()` 呼び出しを fail させる構造 invariant にする方が長期的に robust
- SHA-256 等の内容 hash は本文 read を要するため本不変条件違反、選ばない (代替は §決定 (d) fingerprint で解決)

### (c) Identity = `rel_path` (path-as-id、grep 可能形式、rename = 旧停止+新発火 制限)

`SourceObserved.external_id` は `root_path` 相対パス (`rel_path`) を文字列としてそのまま使う。例: `root_path=/mnt/b` で `/mnt/b/projects/specs/api.md` を観測 → `external_id="projects/specs/api.md"`。SHA hash しない (grep / `opshub source open` / 人間 debug を可能にするため)。

rename / move の semantics:

- `projects/old.md` → `projects/new.md` への rename: 旧 path での SourceObserved は次回 scan から発火しない (file が消えた扱い、§決定 (e) で event 化されない) + 新 path で `external_id="projects/new.md"` の SourceObserved が発火 (別 source として扱われる)
- これは **MVP 制限**。「同一 file の history が rename で分断される」ことを ADR レベルで pin
- xattr / ADS / Box Drive の per-file metadata で安定 Box item ID を取得して `external_id` を hash する経路は Phase 9.x 候補

採用理由:

- xattr / ADS は Box Drive がサポートしない (CldAPI placeholder には拡張属性を書き込む経路が無い)
- 安定 Box item ID は Box Platform API 経由でしか取れず、本 ADR の目的 (Web API 不使用) と矛盾
- rel_path は grep 可能・人間 debug 可能・`file://` URL に展開可能で UX 優位
- rename を identity 保持で扱うには inotify watch / FSEvents が必要 (rename event を捕捉して旧 id を新 id にリンク)、これは §決定 (i) で Phase 9.x 持ち越し
- Phase 9 MVP は scan-only / additive で identity 戦略を単純に保つ

### (d) Diff detection = `sources.fingerprint` 列 + scanner in-memory 比較

`sources` projection に `fingerprint TEXT NULL` 列を追加 (migration 0017、A2 PR で実装)。`SourceObserved` event に `fingerprint: str | None = None` field を backward-compat 追加 (schema_version 据え置き 1、ADR-0002 §4 「新 field 追加は OK」の範囲)。

`BoxDriveScanner.scan()` の手順:

1. 開始時に `SELECT external_id, fingerprint FROM sources WHERE connector_name = 'box_drive'` を一括実行し、in-memory `dict[str, str]` (`prior_fingerprints`) を構築する
2. `os.scandir()` 再帰 walk 中、各 file について `fingerprint = f"{size}:{mtime_ns}"` を計算
3. `prior_fingerprints.get(rel_path)` と比較:
   - 一致 → **skip** (`SourceObserved` 発火しない)
   - 不一致 or 不在 → `ScannedFile` を yield → mapper 経由で `SourceObserved` を append (atomic UoW、Phase 3 PR #26 契約継承)

`fingerprint` の構成要素:

- `size` (bytes): `entry.stat().st_size` を文字列化
- `mtime_ns` (nanoseconds): `entry.stat().st_mtime_ns` を文字列化、`f"{size}:{mtime_ns}"` の colon separator で連結
- SHA-256 / content hash は本文 read を要するため §決定 (b) 違反、採用しない
- false positive (手動 `touch` で mtime が動く / OS clock skew) は受容する。「mtime が動いた = 変更ありと観測」が agent 観点で正解の semantics

採用理由:

- `sources` projection 1 列追加 + `f"{size}:{mtime_ns}"` 計算で 100k+ files の差分検出が `stat()` のみで成立 (本文 read 不要、§決定 (b) と整合)
- in-memory dict 比較は O(1) で 100k file 全 walk でも overhead が無視可能
- `f"{size}:{mtime_ns}"` 形式は human-readable で debug 可能 (hex hash と違い `opshub source show <id>` 出力でも意味が読める)
- 既存 4 connector は `fingerprint=None` のまま動作する (None は projector で NULL 書き込み、backward-compat、A2 PR DoD)

### (e) 削除追跡なし (Phase 9 MVP では未追跡、stale row は Phase 9.x `--stale` flag 候補)

Phase 9 MVP では **削除を event 化しない**。Drive から file が消えた場合:

- 次回 scan で当該 `rel_path` が walk 結果に含まれない → mapper / SourceService が呼ばれない → 新規 event は append されない
- `sources` projection に prior row が **そのまま残る** (event-sourced append-only の自然な帰結。projection は events を replay して構築されるため、削除 event が無い限り row は維持される)
- operator から見ると「stale row」が累積する状態 (= 過去観測されたが現在の Drive に無い source)

Phase 9.x 候補:

- `opshub source list --stale --connector box_drive` で「最終観測から N 日経過し、かつ最後の scan で再観測されなかった source」を炙り出す CLI
- `SourceDeleted` event の追加 (Phase 9.x で削除追跡が必要になった段階で議論、ADR-0002 §4 backward-compat 範囲で追加可能)

採用理由:

- 削除検知は scan 開始時の `sources` projection 全 row と walk 結果の symmetric diff が必要で、scan の単純性 (walk → fingerprint 比較 → append) を破壊する
- event-sourced で「過去観測の事実」は immutable に残るのが自然 (削除して projection から消すと「過去そこに何があったか」を trace するには events 直接 query が必要)
- operator が「stale を見たい」use case は Phase 9.x で `--stale` flag を追加すれば cover 可能、MVP 段階で先回りしない
- 削除追跡を MVP に含めると `BoxDriveScanner.scan()` の責務が「walk + diff + delete event 発行」に肥大化し、test fixture も増える

### (f) `root_path` platform-aware default

`core/platform.py` (新設、B1 PR) で OS / 実行環境を判定し、`box_drive_default_root_path(platform) -> Path | None` で default `root_path` を返す。

| Platform | 判定方法 | default `root_path` | 備考 |
|---|---|---|---|
| WSL2 | `/proc/sys/kernel/osrelease` に `microsoft` 文字列を含む + `sys.platform == "linux"` | `Path("/mnt/b")` | operator が事前 `mountvol B: \\?\Volume{GUID}\` + `wsl --shutdown` 設定済の前提 |
| macOS | `sys.platform == "darwin"` | `Path.home() / "Box"` | Box Drive デフォルトインストール先 |
| Linux native | `sys.platform == "linux"` かつ WSL2 判定が false | `None` (= `ConfigError("Box Drive client is not available on Linux native; see docs/box-drive-setup.md")`) | Box Drive Linux client は存在しない |
| Windows native | (該当しない) | (該当しない) | opshub 全体が POSIX-only、`pyproject.toml` classifier `Operating System :: POSIX`、Windows native は対象外 |

`opshub.toml` で operator が `[connectors.box_drive] root_path = "<custom>"` を指定した場合は platform default を無視して custom を使う (escape hatch)。custom path が存在しない / アクセス不能の場合は `ConfigError` で fail-fast。

採用理由:

- WSL2 / macOS で operator が「いちいち root_path を書かなくてよい」UX が成立
- Linux native は Box Drive client がない事実を fail-fast で伝達 (operator setup の前提が破れていることを早期検出)
- Windows native は opshub 全体の POSIX-only 前提と整合 (Phase 9 だけ Windows 対応すると compat surface が肥大化)
- `core/platform.py` の cold-start budget は `__future__` / `pathlib` / `sys` のみ import で M6 guard 順守 (`time opshub --help` ≤ 300ms 維持)

### (g) Excludes は `opshub.toml` inline (`[connectors.box_drive] exclude_globs`)、共通 `excludes.yaml` 機構化は Phase 9.x

box_drive scanner は `exclude_globs: list[str]` を `opshub.toml` の `[connectors.box_drive]` セクションから inline で受け取る。例:

```toml
[connectors.box_drive]
enabled = true
root_path = "/mnt/b"
exclude_globs = [
  ".DS_Store",
  "Thumbs.db",
  "**/node_modules/**",
  "**/.git/**",
  "**/secrets/**",
]
```

ADR-0005 §決定で言及されている `~/.config/opshub/excludes.yaml` の共通 excludes 機構は Phase 9 では実装しない。Phase 9.x で全 connector 横断 (Phase 7 Box の event filtering / Phase 9 box_drive の path filtering / Phase 7 Slack / MS365 等) のリファクタとして統合する。

採用理由:

- Phase 9 MVP では 1 connector 分の excludes で十分。共通機構を先に作ると 4 既存 connector の filter logic も同期して移行する必要があり、scope が膨らむ
- `opshub.toml` inline は operator が「box_drive の excludes はどこ?」と探すコストが低い (config が 1 ファイルに集約)
- Phase 9.x で `~/.config/opshub/excludes.yaml` を導入する際は、`opshub.toml` inline を deprecated にせず両 sources を merge する経路で migration 可能 (ADR-0005 と整合)

### (h) Operator precondition (`mountvol B:` + `wsl --shutdown`) は opshub 範囲外、`docs/box-drive-setup.md` に外出し

WSL2 で `/mnt/b` を出現させる手順:

```text
# Windows PowerShell (管理者) で実行
mountvol B: \\?\Volume{GUID}\
wsl --shutdown
# WSL2 を起動し直すと /mnt/b が visible になる
```

(qiita 参考記事: <https://qiita.com/himacreation/items/e375e010d670d756e754>)

これは **opshub の自動化範囲外**。理由:

- `mountvol` は Windows 側コマンドで、WSL2 内の Python から呼び出すには PowerShell elevation が必要 (security boundary を越える)
- `\\?\Volume{GUID}\` の GUID は operator 環境ごとに異なり、opshub が判定する経路がない (Windows レジストリ参照が必要)
- WSL2 再起動を opshub が trigger するのは UX 上問題 (実行中の他 process / 編集中の file を巻き込む)

代わりに `docs/box-drive-setup.md` (C1 PR で新設) に以下を記載:

- WSL2 operator 向け: `mountvol` 手順 + qiita 記事 link + 確認方法 (`ls /mnt/b` で Box workspace が見えれば OK)
- macOS operator 向け: Box Drive を <https://www.box.com/resources/downloads> からインストール後、`ls ~/Box` で workspace が見える前提
- Linux native operator 向け: 「Box Drive Linux client は提供されていない。VM / WSL2 経由を推奨」と明示

採用理由:

- OS 設定の自動化は opshub の責務範囲を超える (Phase 1-8 で確立した「opshub は SQLite + events + projection + CLI」境界と整合)
- operator setup を docs に集約することで「opshub 起動時の前提条件」が 1 ファイルで把握できる UX 利点
- WSL2 / macOS の経路が分岐するため docs で明示する方が漏れにくい

### (i) Watch mode は Phase 9.x 持ち越し (Phase 9 MVP は scan-only)

Phase 9 MVP では `opshub connector sync box_drive` を実行した時のみ FS を walk する **scan-only** モードに限定する。以下は Phase 9 では実装しない:

- inotify (Linux) / FSEvents (macOS) / Microsoft CldAPI callback (Windows / WSL2 経由) による push 駆動更新
- `watchdog` ライブラリ経由の cross-platform abstraction
- daemon mode (`opshub watch start` で常駐 process)

Phase 9.x で **filewatch backend abstraction** として別 PR / 別 ADR で扱う。理由:

- Watch mode を先に実装すると identity が rename event を保持する形に設計せざるを得ず、§決定 (c) の rel_path 一本 + rename = 旧停止+新発火 MVP 制限が表現できなくなる (rename を inotify が捕捉した場合に「id を更新するか / 別 source として扱うか」の判断が必要)
- scan mode で 1 vendor (box_drive) 分の運用知見 (false positive 率 / scan duration / event noise) を蓄えてから filewatch を設計する方が安全
- Phase 9.x で 2 vendor 目 (OneDrive client / Dropbox / Google Drive for desktop / iCloud Drive) を実装する際に `local_drive/` 共通基底と filewatch を同時に抽象化する順序が自然

scan mode の trigger は operator manual + cron 経由を想定 (`crontab -e` で `0 */6 * * * opshub connector sync box_drive` 等)。常駐 daemon の OS-level 自動起動 (systemd / launchd) は opshub 範囲外。

## Consequences

### Positive

1. **Web API 不可な企業環境でも Box content を operational memory に取り込める** — Box admin が developer app / OAuth grant を承認しない operator でも、Box Drive デスクトップクライアントが動いていれば OpsHub の event chain に Box content が乗る。Phase 4 semantic recall / Phase 5 brief / Phase 6 propose / Phase 8 knowledge graph がすべて `source_type="box_drive_file"` の source を automatic に活用できる (cross-source recall は Phase 4 機構で自動)
2. **`Connector` Protocol は変えない** — Phase 7 4 connector で確立した Protocol を再利用 (auth layer だけ OS 依存に置換)。framework の freeze 状態を維持し、5 つ目の connector category を 1 connector 分の特殊化として閉じ込めた
3. **`stat()` のみ contract で IT policy / 機密性 / 容量 / cache 肥大化を構造的に防止** — `open()` 禁止 invariant を test で pin することで、ADR-0005 違反 / Box Drive hydration / network egress / OS notification 発火がすべて構造的に発生不能になる
4. **fingerprint で 100k+ files の event noise を抑制** — `sources.fingerprint` 列 + in-memory dict 比較で「変更ありの file のみ event 化」が成立。`projections rebuild` も既存 events を replay すれば fingerprint が再生成され冪等
5. **既存 4 connector への影響ゼロ** — `SourceObserved.fingerprint` は `Optional[str] = None` で backward-compat、`sources.fingerprint` 列は NULL 許容、既存 connector の挙動は 1 byte たりとも変わらない (A2 PR DoD で test pin)
6. **rel_path identity は grep / 人間 debug / `file://` URL 展開で UX 優位** — SHA hash した不透明な id ではなく path string そのものを id にすることで、operator が `opshub source open <id>` / `opshub source show <id>` で直感的に source を辿れる
7. **同一 Box content の二重取り込みを許容** — Phase 7 `box` connector (`source_type="box_event"`) と Phase 9 `box_drive` connector (`source_type="box_drive_file"`) が同じ Box file を異なる source_type で観測しても、Phase 8 `links` projection の manual `link add` で operator が束ねる経路が将来開いている (`source_type` 分離設計の利点)

### Negative / Trade-offs

1. **rename / move で identity が分断される** — `projects/old.md` → `projects/new.md` の rename で「過去 history が old.md に、現在 history が new.md に」分裂する。operator が手で `link add` するか、Phase 9.x で xattr / inotify rename event 捕捉が必要
   - 緩和: rel_path 形式で grep 可能なため、operator が `opshub source list | grep projects/` で旧新両方を見つけて手動で link 可能。常用される rename パターンは Phase 9.x で filewatch + xattr 経路を検討
2. **削除が追跡されない (stale row が累積)** — Drive から削除された file の `sources` row が残り続ける。長期運用で「過去観測されたが現在 Drive に無い」row が累積する
   - 緩和: Phase 9.x の `opshub source list --stale` で炙り出す予定。当面は append-only の自然な状態として受容
3. **`fingerprint = f"{size}:{mtime_ns}"` の false positive** — 手動 `touch` / Box Drive 同期で mtime が動いただけで content 変更がない場合も再 SourceObserved 発火。event noise の potential
   - 緩和: `mtime` が動いた = 「Box が同期 push した = 何らかの change を Box 側が観測した」と解釈すれば agent 観点で正解 semantics。本文 hash は §決定 (b) 違反のため採用不能
4. **WSL2 operator setup が opshub 範囲外で UX hop が増える** — `mountvol` + `wsl --shutdown` が事前必須で、operator が docs を読んで OS 設定する hop が発生する
   - 緩和: `docs/box-drive-setup.md` で WSL2 / macOS / Linux native 経路を一括ガイド、qiita 参考記事を link。`opshub connector sync box_drive` 実行時に `root_path` が存在しない場合は actionable error で setup docs を案内
5. **Linux native は対象外で operator scope が狭まる** — Box Drive Linux client が存在しないため Linux native 環境 (WSL2 でない pure Linux) では box_drive connector が使えない
   - 緩和: 該当 operator は VM / WSL2 経由を案内 (`docs/box-drive-setup.md`)。Phase 7 Web API 経路の `box` connector は Linux native でも動作する (OAuth grant が可能な環境ならば代替経路として有効)
6. **CldAPI hydration contract は OS 実装に依存** — Microsoft / Apple OS 仕様変更で `stat()` が hydrating になった場合、本 ADR の §決定 (b) 不変条件が破れる
   - 緩和: contract test (`tests/integration/test_box_drive_no_hydration.py`、`OPSHUB_BOX_DRIVE_TEST_ROOT` env opt-in) を CI に常駐させ、real env で持続検証する。万一破れた場合は ADR を superseded として書き直し、scanner を停止
7. **Watch mode 不在で「即時更新」UX が無い** — Phase 9 MVP は scan-only のため、Box 側で file が更新されても次回 manual sync / cron まで反映されない
   - 緩和: cron で 6h / 1h 等の頻度で sync する案内を `docs/box-drive-setup.md` に記載。Phase 9.x で filewatch backend を追加して event 駆動に進化

## 軽減策

1. **`open()` 禁止 invariant の test pin** — Phase 9 B1 PR で `unittest.mock.patch("builtins.open", side_effect=AssertionError("forbidden"))` を scan 全 path に適用する test を追加。scanner が誤って本文 read を導入した瞬間 CI が落ちる構造的 guard
2. **CldAPI non-hydration contract test** — `tests/integration/test_box_drive_no_hydration.py` を `OPSHUB_BOX_DRIVE_TEST_ROOT` env opt-in で配置 (CI default は skip、real env でのみ実行)。Phase 9 完了後も持続検証
3. **`docs/box-drive-setup.md` で operator setup を集約** — WSL2 (`mountvol`) / macOS (Box Drive install) / Linux native (代替案) を 1 ファイルにまとめ、`root_path` 不存在時の actionable error から link
4. **`exclude_globs` で機密 path を operator が opt-out** — Phase 7 で確立した excludes 慣行を `opshub.toml` inline で踏襲。`secrets/` 等を含む path を operator が事前除外可能
5. **`opshub.toml` `[connectors.box_drive] enabled = false` default** — Phase 7 全 connector と同パターン、operator が明示 opt-in しない限り sync 経路に乗らない

## Alternatives Considered

### 1. 新 connector ではなく既存 `box` connector を拡張 (`source_type="box_event"` のまま FS scan も追加)

却下理由:

- Phase 7 `box` connector は Box Platform API + OAuth refresh token を前提とした auth layer / fetcher / mapper を持つ。FS scan を同 connector に詰めると Connector responsibility が「Web API + FS walk の dual mode」になり、settings / cursor / failure semantics が分岐して複雑化
- `source_type="box_event"` (Phase 7) と `source_type="box_drive_file"` (Phase 9) は **意味が異なる** (前者は Box の event stream entry、後者は FS 上の file)。同じ `source_type` で扱うと Phase 4 recall / Phase 8 links projection で「event か file か」を識別する手段が失われる
- Web API が使える operator と使えない operator は **mutually exclusive ではない** (両経路を同時に運用したいケースがある)。connector が分かれていれば operator が独立に enable / disable 可能、`source_type` 分離で二重取り込みも許容可能
- ADR-0010 §決定 「1 connector → 1 service 呼び出し / 1 event 単位」 の atomic 性原則と整合

### 2. Filesystem-backed pattern を Phase 9 で `local_drive/` 共通基底として抽象化

Phase 9 MVP で OneDrive client / Dropbox / Google Drive for desktop / iCloud Drive の対応も視野に入れて、`connectors/local_drive/base.py` を共通基底として抽出する案。

却下理由:

- 1 vendor (box_drive) の実装経験から抽象を抽出する方が、premature abstraction を避けられる (XP の rule of three: 2 vendor 目で抽象化を判断、3 vendor 目で確定)
- vendor ごとに quirks がある: OneDrive は file id を xattr で持つ可能性、Dropbox は smart sync の placeholder semantics が異なる、Google Drive for desktop は My Drive と Shared Drives で root が分岐、iCloud Drive は Documents だけ visible 等。これらを 1 vendor 分しか実装していない段階で抽象化すると、後で抽象が破壊的に変わるリスク
- Phase 9 plan §1 #2 で「`local_drive/` 共通基底抽出は Phase 9.x で 2 vendor 目を実装する際に」と pin 済
- Phase 9 MVP scope を「box_drive 専用」に絞ることで A1-C1 全 5 PR の責務が単純化される

### 3. Spike 経由で identity 戦略 (rel_path vs xattr vs Box item ID via local sidecar) を先に pin

spike PR で 3 候補を実装比較してから本 ADR を書く案。

却下理由:

- xattr / ADS は Box Drive がサポートしない (placeholder への拡張属性書き込み経路なし) ことが OS contract から既に判定可能、spike 不要
- Box item ID via local sidecar (例: `.opshub-box-ids.json` を root_path に置く) は Box Drive が当該 file 自体を同期対象にしてしまう risk が高く (sidecar が clutter)、設計から外す
- 残る選択肢は実質 rel_path 一本で、spike しても結論は変わらない
- rename = 旧停止+新発火 制限は spike しても解消されない (xattr で stable id を実現できないため)
- Phase 9 plan §6 (spike 不採用の根拠) で「OS contract + 設計選択で全 open question を潰す」方針を pin 済

### 4. 削除追跡を MVP に含める (`SourceDeleted` event 発行 + 対応する projector update)

scan 開始時の `sources` projection 全 row と walk 結果を symmetric diff し、消えた file を `SourceDeleted` event として発行する案。

却下理由:

- `BoxDriveScanner.scan()` の責務が「walk + fingerprint 比較 + 削除検出 + event 発行」に肥大化、test fixture も 2-pass scan (削除前 / 削除後) で増える
- event-sourced append-only で「過去観測された source が現在 Drive に無い」状態は stale row として残るのが自然 (削除して projection から消すと「過去そこに何があったか」を trace するには events 直接 query が必要)
- operator が「stale を見たい」 use case は Phase 9.x で `opshub source list --stale` CLI で対応可能。MVP 段階で先回りしない (YAGNI)
- Phase 9 plan §1 #6 で「削除追跡なし」を pin 済、本 ADR §決定 (e) で再 pin

## Validation

本 ADR の決定 (a)-(i) は Phase 9 sub-issue A-C の実装で pin された (Phase 9 = 2026-05-23 完了)。実テストファイル名を以下に列挙する:

- **(a) `Connector` Protocol 再利用 + auth layer を OS 依存に置換** — B2 PR (#193) で `BoxDriveConnector` が `Connector` Protocol に適合することを `tests/unit/connectors/box_drive/test_connector.py` で pin (signature 一致 + `ConnectorContext` 受領 + `SourceService.observe` 経由 append)。`opshub connector auth set connector:box_drive` の actionable error reject は `tests/unit/cli/test_connector_auth.py` (C1 closeout PR で追記) と `src/opshub/cli/connector.py` の `auth_set` 内 ``name == "connector:box_drive"`` 分岐で pin
- **(b) `os.stat()` のみ、`open()` 禁止** — B1 PR (#191) で `tests/unit/connectors/box_drive/test_scanner.py` 内 `test_scanner_never_opens_files` (`unittest.mock.patch("builtins.open", side_effect=AssertionError("forbidden"))` を scan 全 path に適用、`pathlib.Path.open` / `read_text` / `read_bytes` も同時に patch) で pin。CldAPI non-hydration contract test は `tests/integration/test_box_drive_no_hydration.py` を `OPSHUB_BOX_DRIVE_TEST_ROOT` env opt-in で配置 (CI default は skip、real env で持続検証 — ADR §軽減策 #2)
- **(c) Identity = `rel_path`** — B2 PR (#193) で `tests/unit/connectors/box_drive/test_mapper.py` の `test_external_id_equals_rel_path` で pin (mapper が `ScannedFile.rel_path` をそのまま `SourceObserved.external_id` に流すこと)。C1 closeout PR の `tests/integration/test_phase9_lifecycle.py::test_box_drive_lifecycle_2_pass_sync` が 2-pass scan の lifecycle 全体で `external_id` が POSIX-form rel_path であることを e2e で再検証
- **(d) Diff detection via `sources.fingerprint`** — A2 PR (#192) で migration 0017 が `sources.fingerprint TEXT NULL` 列を追加することを `tests/integration/test_phase9_migrations.py` で pin (upgrade / downgrade / 既存 row の NULL 残置 / 他 connector の backward-compat)。`SourceObserved.fingerprint` field 追加は `tests/unit/domain/test_events_source.py` で pin (schema_version=1 据え置き + backward-compat)。`SourcesProjection` projector が `fingerprint=None` で NULL 書き込みすることを `tests/unit/projections/test_sources.py` で pin。`BoxDriveScanner.scan()` が prior_fingerprints と比較して変更ありのみ yield することは B1 PR (#191) の `tests/unit/connectors/box_drive/test_scanner.py::test_scanner_skips_unchanged_files` で pin。C1 closeout PR の `tests/integration/test_phase9_lifecycle.py::test_box_drive_lifecycle_2_pass_sync` が `sources.fingerprint` 列が `f"{size}:{mtime_ns}"` の形で永続化され Pass 2 の修正 file で値が更新されることを e2e で pin
- **(e) 削除追跡なし** — C1 closeout PR の `tests/integration/test_phase9_lifecycle.py::test_box_drive_lifecycle_2_pass_sync` で 2-pass scan の 2 pass 目で file 削除を再現し、削除された file について `SourceObserved` が再発火しないこと + `sources` projection に prior row が残ること + `SourceDeleted` 系 event が発行されないことを pin
- **(f) `root_path` platform-aware default** — B1 PR (#191) で `tests/unit/core/test_platform.py` で `detect_platform()` が WSL2 / macOS / linux / unsupported を正しく返すこと (`/proc/sys/kernel/osrelease` mock + `sys.platform` mock)、`box_drive_default_root_path()` が WSL2=`/mnt/b` / macOS=`~/Box` 展開 / Linux native=None を返すことを pin。B2 PR (#193) の `tests/unit/connectors/box_drive/test_connector.py::test_connector_fails_fast_when_no_platform_default` が Linux native で `ConfigError` を raise することを pin
- **(g) Excludes は `opshub.toml` inline** — B2 PR (#193) で `BoxDriveConnectorSettings.exclude_globs` field 定義を `tests/unit/core/test_config.py` で pin (env var override + default empty)。`BoxDriveScanner` が exclude_globs を適用することを B1 PR (#191) の `tests/unit/connectors/box_drive/test_scanner.py::test_scanner_applies_exclude_globs` で pin (top-level + nested + gitignore-style `**/` prefix)
- **(h) Operator precondition は opshub 範囲外** — C1 closeout PR で `docs/box-drive-setup.md` が新設され WSL2 (`mountvol` 手順 + qiita 記事 link) / macOS (Box Drive install) / Linux native (代替案 = WSL2 / VM 経由) の手順を集約。`root_path` 不存在時の `ConfigError` は B1 PR (#191) の `tests/unit/connectors/box_drive/test_scanner.py::test_scanner_raises_config_error_when_root_missing` で pin (ConfigError message が `docs/box-drive-setup.md` を指すこと)
- **(i) Watch mode は Phase 9.x 持ち越し** — Phase 9 MVP の test には watch mode / inotify / FSEvents / daemon 関連の test を **追加しない** ことが pin (test の不在自体が決定の reflection)。Phase 9.x で別 ADR / 別 PR で追加

**Atomicity contract** (per-file UoW、ADR-0019 と直接 §決定 にはないが Phase 3 PR #26 契約の継承) — C1 closeout PR の `tests/integration/test_phase9_connector_atomicity.py` で 3 test pin: (1) 2 yield → 2 sources + 2 inbox rows、(2) scanner mid-scan failure → prefix 2 files commit + `ConnectorSyncFailed` event、(3) projector failure on 2nd file → 1st file commit + 2nd rolled back。Phase 7 `test_phase7_connector_atomicity.py` と同じ shape を box_drive 用に parameterize。

## 関連

- [Principles 1 (Local-first)](../principles.md) — Box Drive デスクトップクライアント経由でローカル FS から取り込む経路は Local-first principle と整合
- [Principles 6 (External Content Minimization)](../principles.md) — `stat()` のみ + `open()` 禁止 不変条件は本 principle の延長
- [Principles 7 (Connector Contract)](../principles.md) — `Connector` Protocol を変えずに 5 つ目の connector category を追加
- [Principles 9 (Phased Delivery)](../principles.md) — Phase 9 = Local-filesystem-backed Connector Layer (本 ADR で確定)
- [Architecture §2.1 (Connector Layer)](../architecture.md) — FS-backed vendor 行を追加 (C1 PR で実施)
- [Architecture §2.11 (新規、Local-filesystem-backed Connector Pattern)](../architecture.md) — 本 ADR の pattern を architecture docs に節として追加 (C1 PR で実施)
- [ADR-0001: Python Stack](0001-python-stack.md) — POSIX-only 前提 (`pyproject.toml` classifier)、Windows native が対象外な根拠
- [ADR-0002: Event-Sourced Architecture](0002-event-sourced-architecture.md) §4 — `SourceObserved` への `fingerprint: str | None = None` field 追加が backward-compat 範囲である根拠 (schema_version 据え置き 1)
- [ADR-0005: External Content Minimization](0005-external-content-minimization.md) — `stat()` のみ + `open()` 禁止 不変条件 (本 ADR §決定 (b)) の根拠、summary ≤ 200 chars enforce の継承
- [ADR-0010: Connector Contract](0010-connector-contract.md) — `Connector` Protocol を変えずに auth layer を OS 依存に置換する根拠、責務 (fetch + normalize + event 化) と禁止事項 (Task / Decision / Link 直接生成しない、projection 直接更新しない) を本 ADR でも全継承
- [ADR-0014: SaaS Token Storage](0014-saas-token-storage.md) — box_drive は token を持たない (OS-level Box Drive 認証に依存) ため `core/secrets` / keyring 経路は使わない (Phase 7 4 connector との差分)
- [ADR-0018: Slack Connector Token Principal](0018-slack-token-principal.md) — 直前 ADR、本 ADR の番号採番 (0019) の根拠
- Phase 7 `box` connector — Web API 経路 (`source_type="box_event"`)、本 ADR の box_drive (`source_type="box_drive_file"`) と二重取り込み許容 (operator が独立に enable / disable 可能)
- [Phase 9 Plan §1 確定済み事項 + §2.1 sub-issue A + §6 spike 不採用の根拠](../phase-9-plan.md)
- 参考記事 (WSL2 → Box Drive mountvol 手順): <https://qiita.com/himacreation/items/e375e010d670d756e754>
