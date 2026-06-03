# Box Drive Setup (Phase 9 connector)

`box_drive` connector は Box Drive デスクトップクライアントが OS に
マウントしたローカル FS を直接 walk する。Box Web API は使わない
([ADR-0019](adr/0019-local-filesystem-backed-connector.md))。

Phase 7 の `box` connector (Box Platform API + OAuth) と Phase 9 の
`box_drive` connector (ローカル FS scan) は二重取り込みを許容し、
operator が独立に enable / disable できる。Web API 経路が IT policy で
塞がれている環境でも Box content を OpsHub の operational memory に
取り込めるようにするのが本 connector の存在理由。

## 対応 platform

- **WSL2** (Linux on Windows): `/mnt/b` (operator 設定必要、後述)
- **macOS**: `~/Box` (Box Drive デフォルトインストール先)
- **Linux native / Windows native**: 非対応 (opshub 全体が POSIX-only、
  [ADR-0019](adr/0019-local-filesystem-backed-connector.md) §決定 (f))

## WSL2 setup

Box Drive は Windows 上でドライブレターを持たない volume として
マウントされ、WSL2 からは見えない。手動でドライブレターを割り当ててから
`wsl --shutdown` で WSL を再起動する。

```powershell
# 管理者 PowerShell で実行
mountvol                                      # ボリューム一覧で Box の GUID を特定
mountvol B: \\?\Volume{<GUID>}\               # 例: 未使用の B: ドライブに割当
wsl --shutdown                                # WSL を完全再起動
```

WSL を再起動すると `/mnt/b/` が自動マウントされる。
確認: `ls /mnt/b/` で Box workspace が見えれば OK。

詳細手順: <https://qiita.com/himacreation/items/e375e010d670d756e754>

## macOS setup

Box Drive を <https://www.box.com/resources/downloads> から
インストール後、Web Box に Sign in すると `~/Box` 配下が同期される。
確認: `ls ~/Box` で workspace が見えれば OK。OpsHub 側で追加の設定は不要
(`opshub.toml` で `root_path` を省略すれば自動で `~/Box` に解決される)。

## Linux native setup

Box は Linux native client を提供していない。WSL2 / macOS / VM 経由を
推奨。Web API 経由の Phase 7 `box` connector は Linux native でも
動作するので、developer app 登録 / OAuth grant が可能な環境ならば
そちらを利用する。

## opshub.toml 設定

```toml
[connectors.box_drive]
enabled = true                               # default: false
# root_path は省略時 platform default (WSL2=/mnt/b, macOS=~/Box)
# root_path = "/mnt/b/Work/Project"          # サブツリーに絞ることも可
exclude_globs = ["**/.DS_Store", "**/~$*"]   # 任意
max_depth = 16                               # default: 16
max_files = 100_000                          # default: 100_000
follow_symlinks = false                      # default: false
```

env 経由の override も同じ pattern で動作する
(`OPSHUB_CONNECTORS__BOX_DRIVE__ROOT_PATH=/mnt/b/Work/Project`)。
CI / container では env 経由が便利。

## sync 実行

```bash
opshub box_drive sync
```

差分検出は `sources.fingerprint` 列 (Phase 9 step A2 / migration 0017)
との比較で自動的に行われる。2 回目以降の sync は
`fingerprint = f"{size}:{mtime_ns}"` が変化した file のみ取り込む。

定期実行は cron / launchd 等の OS-level scheduler で operator が
設定する (常駐 daemon は Phase 9 scope 外):

```cron
# crontab -e
0 */6 * * * opshub box_drive sync
```

## 認証

`opshub box_drive auth set` は **登録されていない** (Phase 17 ADR-0031 §決定 (6))。
コマンドを叩くと Typer が `No such command 'auth'` で exit 2 する。
`box_drive` は OS-level Box Drive 認証 (Web Box ログイン状態を OS daemon が
保持) に依存しているため、opshub 側で token を持たない
([ADR-0019](adr/0019-local-filesystem-backed-connector.md) §決定 (a))。
opshub から見える唯一の設定は `opshub.toml` 内の
`[connectors.box_drive] root_path` のみ。

## 制約事項

- **ファイル本文は read されない** (`os.stat()` の metadata のみ、
  [ADR-0019](adr/0019-local-filesystem-backed-connector.md) §決定 (b))。
  CldAPI / File Provider Extension の placeholder hydration を防ぐため。
- **rename / move は「旧 path の停止 + 新 path での発火」として観測**
  される (ADR-0019 §決定 (c))。同一 file の history が rename で分断
  される MVP 制限。xattr 経由の安定 identity は Phase 9.x 候補。
- **削除追跡なし** (ADR-0019 §決定 (e))。Box Drive から消えた file の
  `sources` 行は stale row として残る。`opshub source list --stale` で
  炙り出す機能は Phase 9.x 候補。
- **watch mode なし** (ADR-0019 §決定 (i))。Phase 9 MVP は scan-only。
  `opshub box_drive sync` を operator が cron 等で叩く前提。
  inotify / FSEvents / CldAPI callback による push 駆動更新は Phase 9.x
  で filewatch backend として抽象化予定。

## トラブルシューティング

### `Box Drive root_path is not configured ...` エラー

Linux native 環境で実行している (Box Drive client がない)。WSL2 / macOS /
VM 経由に切り替えるか、Phase 7 `box` connector で代替する。

### `Box Drive root_path does not exist: /mnt/b ...` エラー

WSL2 で `mountvol` 設定が未実施、または `wsl --shutdown` 後の再起動が
未実施。上記 「WSL2 setup」 を参照。

### sync しても変更が反映されない

Box Drive client 側の同期が完了しているか確認 (`ls -la <root>/<file>` で
mtime が最新か)。Box Drive は file が変更されたタイミングと OS 側に
mtime が反映されるタイミングに lag がある (特に大きい file)。
`opshub box_drive sync` を数分後にもう一度実行する。

### 100k+ files で sync が時間がかかる

`max_files = 100_000` がデフォルト上限。これを超える workspace では
`opshub.toml` で値を上げる
(`[connectors.box_drive] max_files = 500_000` 等)。1M を超えるなら
Phase 9.x の chunked-scan discussion が必要 — issue を起票してほしい。

## 関連 docs

- [ADR-0019: Local-filesystem-backed Connector](adr/0019-local-filesystem-backed-connector.md)
- [ADR-0005: External Content Minimization](adr/0005-external-content-minimization.md)
- [ADR-0010: Connector Contract](adr/0010-connector-contract.md)
- [Phase 9 Plan](phase-9-plan.md)
