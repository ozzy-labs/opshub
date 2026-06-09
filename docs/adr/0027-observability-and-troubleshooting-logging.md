# 0027. Observability & Troubleshooting Logging

- Status: Accepted
- Date: 2026-06-01
- Deciders: opshub maintainers

## Context

`core/logging.py` には既に `configure_logging(level=, json=)`（structlog、TTY 検出で JSON / console 自動切替）が存在するが、CLI フラグから一切叩かれていなかった。`get_logger()` がデフォルト引数で暗黙初期化するだけで、operator が verbosity を制御する術がない。root callback (`src/opshub/cli/app.py:_root`) には `--version` のみ。グローバルな verbosity / debug オプションがない。

しかし opshub は連携先（SaaS connector / LLM / 暗号化鍵 / `.env`）にトークンを大量に持つ。verbose / debug 出力（full traceback / 例外メッセージ / config dump）は、まさに今 redaction でガードしている経路を**バイパスし得る**。よって本 ADR は「CLI 観測性追加」であると同時に「ログ redaction の構造化」セキュリティ強化でもある。

設計上の制約:

- **ADR-0001 cold-start**: `opshub --help` は ~300ms 以内。`cli/*.py` は重い import をモジュール先頭に置けない（`tests/integration/test_cli_imports.py` が静的に強制、`test_cold_start` が wall-clock を強制）。
- **ADR-0014 / ADR-0021**: SaaS トークン / 暗号化鍵は keyring。`.env` はステージ・ログ禁止。
- **ADR-0022**: MCP boundary では `mcp/_redact.py:redact_secrets` がトークン形状を marker 化。
- **既存資産**: `core/sanitise.py:sanitise_error_message` が bearer / API key 形状を marker 化する SSOT。`mcp/_redact.py` はそれを呼び出す薄いラッパ。
- **出力チャネル分離**: stdout は各コマンドの結果サマリ専用（パイプ / スクリプト互換）。ログは `structlog` で stderr。ADR-0026 で確立済み。

## Decision

### (a) Verbosity / debug の意味論

CLI root callback に以下のグローバルフラグを追加する（T2 PR で配線）。全サブコマンド共通、デフォルト全 off:

| Option | Effect |
|---|---|
| `-v` / `--verbose` (repeatable) | `-v` → INFO / `-vv` → DEBUG。`-vv` は `--debug` を含意し `OPSHUB_DEBUG=1` env export + サニタイズ済み full traceback を有効化する |
| `-q` / `--quiet` (repeatable) | `-q` → WARNING / `-qq` → ERROR |
| `--debug` | DEBUG + 例外を**サニタイズ済み** full traceback で表示（`main()` の `Error: <msg>` 一行を上書き） |
| `--log-format [auto\|json\|console]` | TTY 自動検出を明示上書き。未知値は silent に `auto` フォールバック |
| `--log-file PATH` | ログをファイルにも tee。0600 で作成 |

環境変数同義: `OPSHUB_LOG_LEVEL` / `OPSHUB_LOG_FORMAT` / `OPSHUB_DEBUG` / `OPSHUB_LOG_FILE`（`mcp serve` の subprocess 起動・cron 経由 sync 等、フラグを渡せない文脈用）。**優先順位は CLI フラグ > 環境変数 > デフォルト**。`OPSHUB_DEBUG` の truthy 値は `1` / `true` / `yes` / `on` / `debug`（大文字小文字無視、前後空白 trim、`core/logging.py` `_TRUTHY` が SSOT）。

`-v` と `-q` の衝突は CLI 層（T2）でエラーにする想定だが、`resolve_log_settings` は両方渡された場合 quiet を最終的に適用する（保守的）。

**`-vv` ⇒ `--debug` の含意契約**（epic #317 post-merge audit H1、2026-06-02 で明文化）: CLI 層で DEBUG レベルに到達するすべての経路 (`--debug` / `-vv` / `-vvv`...) は `LogSettings.debug = True` を flip する。これにより T2 root callback の `OPSHUB_DEBUG=1` env export と T2 `main()` error wrapper のサニタイズ済み full traceback がフラグ綴り差で取りこぼされない。env 経路 (`OPSHUB_LOG_LEVEL=DEBUG`) は本契約の対象外で、`debug` flag を flip するには `OPSHUB_DEBUG=1` を明示する（既存 operator スクリプトの後方互換のため）。

### (b) structlog redaction processor 契約

`core/logging.py` の processor チェーンに、**全イベント値**（`event` 文字列 + 各 key-value + bound context + `format_exc_info` で展開される traceback 文字列）を `sanitise_error_message` に通す **redaction processor** を追加する。挿入位置:

```text
contextvars.merge → add_log_level → TimeStamper → StackInfoRenderer
  → format_exc_info → _redaction_processor → JSONRenderer / ConsoleRenderer
```

`format_exc_info` の **後** に redaction が走るため、`logger.exception(...)` で展開される traceback も scrub される。`Renderer` の **前** に走るため、scrub 済み dict が serialise される。

scrub は **値の型ごとに再帰的**に行う:

- `str` → `sanitise_error_message` で marker 化
- `list` / `tuple` → 各要素を再帰
- `dict` → 各 value を再帰（key は scrub しない: key 名にトークンが入る設計は許容しない）
- それ以外 → そのまま（数値・bool は token を構文的に持てない）

これにより `--debug` で full traceback を出しても、`logger.bind(token=...).info(...)` してしまっても、トークン形状は marker 化される。**ただし**:

- この処理は防衛網であり完全な PII scrubber ではない。コネクタ / LLM client 実装側でトークンを例外メッセージに含めない努力は引き続き必要。
- 「未知の形状の secret」（例: 新規 SaaS の独自 token 形式）はカバーしない。`core/sanitise.py` の regex を拡張すれば全 caller が恩恵を受ける。

### (c) connector / MCP の debug-safe 出力境界

- `cli/connector.py` の sync 失敗時表示は **デフォルトで `type(exc).__name__` のみ**（既存挙動・ADR-0014 / ADR-0022 由来）。`--debug` 時のみ**サニタイズ済み**メッセージ + traceback を opt-in で追加表示する（T3 範囲）。
- `mcp/_redact.py:redact_secrets` は MCP 出力境界専用として維持する。本 ADR の structlog processor とは独立した第二層の防衛網（MCP は agent host のトランスクリプトに直接出るため、log とは別経路で redaction が要る）。同じ regex set を `core/sanitise.py` 経由で共有しているため、carve-out 漏れは構造的に発生しない。

### (d) `--log-file` パーミッション契約

`--log-file PATH` 指定時、`configure_logging` は `path.parent` を `mkdir(parents=True, exist_ok=True)` した上で `os.open(path, O_CREAT | O_WRONLY | O_APPEND, 0o600)` でファイルを作成する。これにより:

- ファイル新規作成時、**パーミッションは byte 0 から 0600**（owner 読み書きのみ）。
- 既存ファイルは mode を変更しない（operator が意図的に変えている可能性を尊重）。
- `O_APPEND` で複数プロセスからの append-safe を担保（POSIX byte-level）。
- 内容は structlog processor を通っているため redaction 適用済み。

### (e) `format_debug_traceback(exc)` ヘルパ

T2 の `main()` error wrapper から呼ぶ専用ヘルパとして `format_debug_traceback(exc) -> str` を `core/logging.py` に追加する。`traceback.format_exception` の結果を join して `sanitise_error_message` を通すだけの薄いラッパだが、CLI 側に「`str(exc)` を直接 print してはいけない」という規約を強制する seam として機能する。

## Consequences

### Positive

- operator が `-v` / `--debug` で原因調査できる。トラブルシューティング UX が大幅改善。
- structlog redaction processor で**すべての**ログイベント値が scrub されるため、新規 call site が増えても自動的に保護される（個別 redaction 漏れの構造的解決）。
- `--debug` の full traceback もサニタイズ済みなので、operator が安全に共有できる（slack に貼る / issue に paste する場合の漏洩リスク低減）。
- 新規依存ゼロ（structlog は既に lockfile に存在）。cold-start 影響なし（structlog の top-level import は維持、CLI 側からは T2 で遅延 import）。
- `core/sanitise.py` 経由で `mcp/_redact.py` と regex set を共有しているため divergence しない。
- `--log-file` 0600 でディスクに残るログにも redaction が適用される。

### Negative / Trade-offs

- redaction processor は全イベント値を walk するため僅かな処理コストが乗る。INFO 連投経路では measurable になりうるが、現状の opshub は WARNING 中心の疎なログ運用（ADR-0026 §決定 (9) 参照）で実害なし。
- `core/sanitise.py` の regex 集合は「既知トークン形状」に限定されており、未知形式の secret はカバーしない（防衛網の限界）。
- `--debug` 時の traceback はサニタイズされているとはいえ、内部 stack 構造（モジュール path / 行番号）は露出する。これは debug の本質的価値とトレードオフ。

### Neutral

- T2 で root callback に追加される `-v` / `-q` / `--debug` / `--log-format` / `--log-file` フラグは、すべてのサブコマンドに自動で効く。個別サブコマンドは追加の wiring 不要。

## Alternatives Considered

- **個別 call site で redaction**: 既に `core/sanitise.py` / `mcp/_redact.py` で部分的に行っているが、新規 call site の追加忘れに弱い。processor 化で構造的に解決する。
- **`--debug` 時もメッセージを生で出す**: 既存の connector sync ガード（型名のみ）と整合せず、operator が「ログを slack に貼ったら secret が漏れる」リスクを抱える。サニタイズ前提とする。
- **`--log-file` のパーミッションを `umask` 任せにする**: shell や OS の `umask` 設定に依存し、`0644`（world-readable）でログが落ちる可能性がある。`0600` を atomic に強制する。
- **redaction を `format_exc_info` の前に挿入する**: traceback 文字列が後から構築されるため、processor の見ない state を scrub することになり穴ができる。後に挿入する。

## 関連

- [ADR-0001 Python Stack](0001-python-stack.md) — cold-start 予算の出所。CLI 側の structlog import は遅延が必須（T2）。
- [ADR-0014 SaaS Token Storage](0014-saas-token-storage.md) — トークンは keyring。ログには出さない原則。
- [ADR-0021 Encryption at Rest](0021-encryption-at-rest.md) — 暗号化鍵も keyring。ログには出さない原則。
- [ADR-0022 MCP Server Surface](0022-mcp-server-surface.md) — MCP 境界の redaction (`mcp/_redact.py`)。本 ADR と独立した第二層。
- [ADR-0026 CLI Progress Reporting](0026-cli-progress-reporting.md) — stdout / stderr 分離の先例。本 ADR も stderr 出力。
- [ADR-0034 Slack Engagement Axis](0034-slack-engagement-axis.md) — Phase 19 で追加される `search.messages?query=from:@me` 呼び出し経路の Slack OAuth token (`xoxp-`) も、本 ADR §(b) の structlog redaction processor 経路と `core/sanitise.py` SSOT regex (bearer / API key 形状) を inherit する。新 redaction 経路 / scrubber 拡張は ADR-0034 では追加せず、既 stance を継承する契約。
- [ADR-0035 Slack Sort Axis Consolidation](0035-slack-sort-axis-consolidation.md) — ADR-0034 の CLI surface (`--activity={mine|any}`) を `--sort=name|last_self_post|last_activity` に部分 supersede。`search.messages` 呼び出し経路 (engagement 軸 = `--sort=last_self_post`; Phase 23-G [#537](https://github.com/ozzy-labs/opshub/issues/537) で `--sort=name + --since` 暗黙 engagement は撤去) の token redaction は本 ADR §(b) processor を継承して不変。ADR-0035 §(e) で追加される暗黙 cutoff notice (`notice: --sort=<sort> defaulted to --since 90d ...`) は ADR-0034 §(i) indexing-lag notice と同様に `-q` / `OPSHUB_LOG_LEVEL` で suppress しない one-shot teaching message で、本 ADR の verbosity 制御 (structlog) とは独立した stderr 一行通知経路。
- Epic #317（CLI トラブルシューティング用オプション）, sub-issues #318 (T1) / 続く T2-T4.
