# OneDrive Drive Setup (Phase 11 connector)

`onedrive_drive` connector は OneDrive Desktop client が OS にマウントした
ローカル FS を直接 walk する。Microsoft Graph API は使わない
([ADR-0019](adr/0019-local-filesystem-backed-connector.md) §決定 (j) で
box_drive と同パターンに統一)。

Phase 7 の `ms365` connector (Microsoft Graph API + OAuth) と Phase 11 の
`onedrive_drive` connector (ローカル FS scan) は二重取り込みを許容し、
operator が独立に enable / disable できる。Web API 経路が IT policy で
塞がれている環境でも OneDrive content を OpsHub の operational memory に
取り込めるようにするのが本 connector の存在理由。Phase 11 で box_drive と
共通の Office 抽出 hook (`content_extraction = true` で `.docx`/`.xlsx`/`.pptx`
を markitdown 経路で本文取り込み、[ADR-0025](adr/0025-office-document-content-extraction.md))
も併用できる。

## 対応 platform

- **WSL2** (Linux on Windows): `/mnt/onedrive` (operator 設定必要、後述)
- **macOS**: `~/OneDrive` (OneDrive Desktop デフォルトインストール先)
- **Linux native / Windows native**: 非対応 (opshub 全体が POSIX-only、
  ADR-0019 §決定 (f))

## WSL2 setup

OneDrive Desktop は Windows 上では通常 `C:\Users\<user>\OneDrive` に
同期されているが、WSL2 から見るには path 越えが必要。`mountvol` で
別ドライブレターに割り当てるか、`/mnt/c/Users/<user>/OneDrive` を
`/mnt/onedrive` に bind mount する 2 案がある。

### 案 A: bind mount (推奨)

```bash
# WSL2 内
sudo mkdir -p /mnt/onedrive
sudo mount --bind /mnt/c/Users/<your-windows-user>/OneDrive /mnt/onedrive
```

`/etc/fstab` に追記して永続化:

```text
/mnt/c/Users/<your-windows-user>/OneDrive  /mnt/onedrive  none  bind  0  0
```

### 案 B: 直接 path 指定

bind mount を使わず `opshub.toml` で直接 path 指定する:

```toml
[connectors.onedrive_drive]
root_path = "/mnt/c/Users/<your-windows-user>/OneDrive"
```

`/mnt/c/` は Windows NTFS への bridge で OneDrive の file metadata がそのまま
見える。CldAPI / File Provider Extension の placeholder hydration を防ぐため
本 connector は `stat()` のみで `open()` しない (ADR-0019 §不変条件 (b))。

確認: `ls /mnt/onedrive/` で OneDrive workspace が見えれば OK。

## macOS setup

OneDrive Desktop を <https://www.microsoft.com/microsoft-365/onedrive/download>
からインストール後、Microsoft アカウントで sign in すると `~/OneDrive`
配下が同期される。確認: `ls ~/OneDrive` で workspace が見えれば OK。
OpsHub 側で追加の設定は不要 (`opshub.toml` で `root_path` を省略すれば
自動で `~/OneDrive` に解決される)。

## Linux native setup

Microsoft は OneDrive の Linux native client を提供していない。WSL2 / macOS /
VM 経由を推奨。Web API 経由の Phase 7 `ms365` connector は Linux native でも
動作するので、OAuth grant が可能な環境ならばそちらを利用する。

## opshub.toml 設定

> **TOML 読込**: `opshub.toml` は起動時に毎回読まれ、`OPSHUB_*` 環境変数は TOML を上書きする (優先順位 `init args > env > toml > defaults`、[ADR-0032](adr/0032-runtime-toml-config-loading.md))。`OPSHUB_CONFIG_DIR=<dir>` で config dir 全体を差し替え可。詳細は [`docs/troubleshooting.md` §3.10](troubleshooting.md)。

```toml
[connectors.onedrive_drive]
enabled = true                               # default: false
# root_path は省略時 platform default (WSL2=/mnt/onedrive, macOS=~/OneDrive)
# root_path = "/mnt/onedrive/Work/Project"   # サブツリーに絞ることも可
max_depth = 16                               # default: 16
max_files = 100_000                          # default: 100_000
follow_symlinks = false                      # default: false

# Phase 11: Office 抽出 hook (ADR-0019 §決定 (b') + ADR-0025)
content_extraction = false                   # default: false。true で .docx/.xlsx/.pptx を markitdown 抽出
```

env 経由の override も同じ pattern で動作する
(`OPSHUB_CONNECTORS__ONEDRIVE_DRIVE__ROOT_PATH=/mnt/onedrive/Work/Project`)。
CI / container では env 経由が便利。

### 除外 path の設定

path-based exclusion は共通 `~/.config/opshub/excludes.yaml` の
`paths:` selector に集約する ([ADR-0020](adr/0020-full-local-content-retention.md) §(b))。
連携前は Phase 11 F4-b で `[connectors.onedrive_drive] exclude_globs = [...]`
inline で設定していたが、epic #470 で `model_config = ConfigDict(extra="forbid")`
化したため `opshub.toml` に inline `exclude_globs` を残すと
`ValidationError` で sync が起動しない (移行手順は [`docs/upgrading.md`](upgrading.md) §Pre-userbase compat shim cleanup)。

```yaml
# ~/.config/opshub/excludes.yaml
paths:
  - "**/.DS_Store"
  - "**/~$*"
  - "**/secrets/**"          # 機密 path はここで遮断
```

`excludes.yaml` は box_drive / onedrive_drive 含む全 connector 横断の
SSOT。

`content_extraction = true` を有効化するには `[office]` extras を install
する必要がある (markitdown 経由、ADR-0025):

```bash
uv tool install "ozzylabs-opshub[office]"
```

その後 `opshub onedrive_drive sync` 実行時に `.docx`/`.xlsx`/`.pptx`
が検出されると、`core/document_extract.extract_document(path)` 経由で本文を
抽出し、`source_type = word_document` / `excel_spreadsheet` /
`powerpoint_slide_deck` で `sources.body` に persist する。

## sync 実行

```bash
opshub onedrive_drive sync
```

差分検出は `sources.fingerprint` 列 (Phase 9 step A2 / migration 0017)
との比較で自動的に行われる。2 回目以降の sync は
`fingerprint = f"{size}:{mtime_ns}"` が変化した file のみ取り込む。
fingerprint state は `connector_name = 'onedrive_drive'` で box_drive と
独立に管理される (同じ file が両 mount にあれば 2 回別 source として記録)。

定期実行は cron / launchd 等の OS-level scheduler で operator が設定する
(常駐 daemon は Phase 11 scope 外):

```cron
# crontab -e
0 */6 * * * opshub onedrive_drive sync
```

## 認証

`opshub onedrive_drive auth set` は **登録されていない** (Phase 17 ADR-0031 §決定 (6))。
コマンドを叩くと Typer が `No such command 'auth'` で exit 2 する。
`onedrive_drive` は OS-level OneDrive Desktop 認証 (Microsoft
アカウント sign-in 状態を OneDrive Desktop daemon が保持) に依存しているため、
opshub 側で token を持たない (ADR-0019 §決定 (a))。opshub から見える唯一の
設定は `opshub.toml` 内の `[connectors.onedrive_drive] root_path` /
`content_extraction` のみ。

## 制約事項

- **ファイル本文は default で read されない** (`os.stat()` の metadata のみ、
  ADR-0019 §不変条件 (b))。CldAPI / File Provider Extension の placeholder
  hydration を防ぐため。
- **Office 抽出は opt-in** (`content_extraction = true` 時のみ markitdown
  経路で `.docx`/`.xlsx`/`.pptx` を open、ADR-0019 §決定 (b'))。size cap
  (50 MB) / text cap (500K chars) / 失敗時 fail-safe (`body=None`) は
  ADR-0025 §決定 (b)/(c) で pin。
- **rename / move は「旧 path の停止 + 新 path での発火」として観測**
  される (ADR-0019 §決定 (c))。同一 file の history が rename で分断
  される MVP 制限。
- **削除追跡なし** (ADR-0019 §決定 (e))。OneDrive から消えた file の
  `sources` 行は stale row として残る。`opshub source list --stale` は
  Phase 11.x 候補。
- **watch mode なし** (ADR-0019 §決定 (i))。Phase 11 MVP は scan-only。
  `opshub onedrive_drive sync` を operator が cron 等で叩く前提。

## トラブルシューティング

### `OneDrive root_path is not configured ...` エラー

Linux native 環境で実行している (OneDrive Desktop がない)。WSL2 / macOS /
VM 経由に切り替えるか、Phase 7 `ms365` connector で代替する。

### `OneDrive root_path does not exist: /mnt/onedrive ...` エラー

WSL2 で bind mount / mountvol 設定が未実施。上記 「WSL2 setup」 を参照。
macOS で OneDrive Desktop がインストールされていない / sign in 未実施の
可能性もある。

### sync しても変更が反映されない

OneDrive Desktop 側の同期が完了しているか確認 (`ls -la <root>/<file>` で
mtime が最新か)。OneDrive Desktop は file が変更されたタイミングと OS 側に
mtime が反映されるタイミングに lag がある (特に大きい file)。
`opshub onedrive_drive sync` を数分後にもう一度実行する。

### Office 抽出が動かない

`opshub.toml` で `content_extraction = true` を設定しているか確認。さらに
`[office]` extras (`uv sync --extra office` or
`uv tool install "ozzylabs-opshub[office]"`) が install されている必要が
ある。markitdown が import できない場合は `ImportError` で fail-fast。

### 100k+ files で sync が時間がかかる

`max_files = 100_000` がデフォルト上限。これを超える workspace では
`opshub.toml` で値を上げる
(`[connectors.onedrive_drive] max_files = 500_000` 等)。1M を超えるなら
Phase 11.x の chunked-scan discussion が必要 — issue を起票してほしい。

## 関連 docs

- [ADR-0019: Local-filesystem-backed Connector](adr/0019-local-filesystem-backed-connector.md) (Phase 11 改訂 §決定 (b') + (j))
- [ADR-0025: Office Document Content Extraction](adr/0025-office-document-content-extraction.md)
- [ADR-0010: Connector Contract](adr/0010-connector-contract.md)
- [docs/box-drive-setup.md](box-drive-setup.md) (兄弟 connector、同 pattern)
- [Phase 11 Plan](phase-11-plan.md)
