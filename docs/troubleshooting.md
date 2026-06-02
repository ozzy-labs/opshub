# Troubleshooting

> Phase 14 epic #317 / [ADR-0027](adr/0027-observability-and-troubleshooting-logging.md)。

OpsHub の CLI / MCP server で問題が起きたときの調査手順をまとめる。グローバルなログ verbosity / debug オプションは Phase 14 epic #317 で導入された ([ADR-0027](adr/0027-observability-and-troubleshooting-logging.md))。トークンや鍵が誤って出力されないよう、ログ全体は redaction processor を通すように設計してある。

## 1. グローバルオプション

すべてのサブコマンド (`opshub task ...` / `opshub connector sync ...` / `opshub mcp serve` 等) に共通で効くフラグ。`opshub --help` でも一覧表示できる。

| Option | 効果 | デフォルト |
|---|---|---|
| `-v` / `--verbose` (繰り返し可) | `-v` で INFO、`-vv` で DEBUG にログレベルを上げる。`--debug` の暗黙挙動と等価 (`-vv` のとき `OPSHUB_DEBUG=1` が自動 export される)。`--quiet` とは併用可能だが、最終的に `--quiet` の方が優先される | (適用なし) |
| `-q` / `--quiet` (繰り返し可) | `-q` で WARNING、`-qq` で ERROR にログレベルを下げる | (適用なし) |
| `--debug` | DEBUG レベルに固定し、`OpsHubError` 系の例外発生時にサニタイズ済みの full traceback を stderr に追加表示する (デフォルトの 1 行 `Error: <msg>` も残る)。`-vv` を含意し、`OPSHUB_DEBUG=1` を環境に export する (`mcp serve` 等の subprocess にも伝搬する) | off |
| `--log-format <auto\|json\|console>` | ログレンダラを明示する。`auto` (デフォルト) は stderr が TTY なら `console`、それ以外は `json` を選ぶ | `auto` |
| `--log-file PATH` | ログを stderr に加えて指定ファイルにも書く。ファイルは **mode 0600** で新規作成される (既存ファイルの mode は変更しない、`O_APPEND` で append-safe)。ファイル内容にも redaction processor が適用される | (なし) |

優先順位は **CLI フラグ > 環境変数 > デフォルト**。フラグは root callback で解決されるため、`opshub <subcommand>` の `<subcommand>` 直前に置く (例: `opshub -vv connector sync github`)。

`-v` と `-q` を同時に指定した場合は **`-q` (quiet) が優先される** (例: `-vv -q` は WARNING 相当)。これは「ノイズの多いコマンドに `-q` を後付けしたら明示的に静かになってほしい」という保守的なデフォルトであり、`src/opshub/core/logging.py:resolve_log_settings` が SSOT。

`--log-format` に未知の値 (例: `--log-format yaml`) を渡したときは silent に `auto` フォールバックする。明示的なエラーは出さず stderr に warning も出さないので、CI などで「想定通りのレンダラに固定したい」場合は値を厳密に管理すること (`auto` / `json` / `console` の 3 値のみ受理)。

## 2. 環境変数

CLI フラグを渡せない文脈 (`mcp serve` を subprocess として起動する agent host、cron 経由の sync 等) では同等の制御を環境変数で行う。

| 環境変数 | 受理する値 | 効果 |
|---|---|---|
| `OPSHUB_LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` (case-insensitive、前後空白 trim) | ログレベルを直接指定する。未知の値は無視され、デフォルト `INFO` にフォールバックする |
| `OPSHUB_LOG_FORMAT` | `auto` / `json` / `console` (case-insensitive、前後空白 trim) | ログレンダラを指定する。未知の値は無視され `auto` フォールバック |
| `OPSHUB_DEBUG` | truthy = `1` / `true` / `yes` / `on` / `debug`、falsy = `0` / `false` / `no` / `off` / 空文字 (case-insensitive、前後空白 trim) | `--debug` 同等。DEBUG レベル + sanitised full traceback を有効化する |
| `OPSHUB_LOG_FILE` | 絶対 or 相対パス | `--log-file` 同等。mode 0600 で作成 (既存ファイルは mode 不変)、`O_APPEND` |

`--debug` または `-vv` を CLI で指定したとき、root callback は `OPSHUB_DEBUG=1` を `os.environ` に export する。これにより、`opshub mcp serve` のような subprocess を fork する経路や、`connector sync` の失敗時 stderr 表示判定が、親プロセスの Typer Context を持たない場面でも `--debug` を観測できる。逆に、自分で `OPSHUB_DEBUG=1` を export していれば、`--debug` フラグなしでも全コマンドが debug 出力を行う。

参照: [`src/opshub/core/logging.py:resolve_log_settings`](../src/opshub/core/logging.py)、[`src/opshub/cli/app.py`](../src/opshub/cli/app.py)。

## 3. 典型シナリオ別レシピ

### 3.1 `opshub connector sync <name>` が失敗する

デフォルトの失敗表示は `ConnectorSyncFailed` event に **例外型名だけ** を残す (R3 不変条件、ADR-0014 / ADR-0022 由来)。`sync failed: <Type>` の 1 行サマリは **stderr** に、event log の `error_message` は型名のみで、例外メッセージは出さない。成功時の `synced <name>: N item(s) observed` は従来通り **stdout** なので、結果を pipe で受け取るスクリプトは影響を受けない (`opshub connector sync ... > out.txt 2> err.txt` のように分けると挙動が明快)。

原因調査には `--debug` を付けて再実行する:

```bash
opshub --debug connector sync github
```

`--debug` 時のみ、サニタイズ済みの例外メッセージと traceback が **stderr** に追加表示される。デフォルトの `sync failed: <Type>` stderr サマリ・event log の `error_message` 自体は変わらない (R3 cont'd) — `--debug` が増やすのは追加の stderr 行 (例外メッセージ + サニタイズ済み traceback) だけ。トークン形状はすべて marker に置換されてから出力されるため、ログを社内チャットや issue に貼っても安全。

cron 経由など、フラグを渡せない場合は環境変数で:

```bash
OPSHUB_DEBUG=1 opshub connector sync github
```

それでも原因が分からないときは:

- `opshub connector list` でコネクタが登録されているか
- `opshub connector auth set <name>` で credential を保存し直す
- `~/.config/opshub/excludes.yaml` の `channels` / `senders` / `repos` / `paths` が意図せず全件除外していないか

### 3.2 `opshub mcp serve` が agent host とつながらない

MCP server は agent host が subprocess として spawn する経路を前提にしている。Typer の root callback が走らないので、`-v` / `--debug` フラグでは制御できない。**環境変数で制御する**:

```bash
# Claude Code 等から渡す場合 (mcp_servers.json の env セクション)
{
  "mcpServers": {
    "opshub": {
      "command": "opshub",
      "args": ["mcp", "serve"],
      "env": {
        "OPSHUB_LOG_LEVEL": "DEBUG",
        "OPSHUB_LOG_FORMAT": "json"
      }
    }
  }
}
```

手元のシェルから直接起動して動作確認することもできる:

```bash
OPSHUB_LOG_LEVEL=DEBUG opshub mcp serve
# stdio 経由なので入力待ちになる。Ctrl-D で終了
```

`opshub mcp serve` の起動経路は `resolve_log_settings()` を CLI 引数なしで呼び、`OPSHUB_LOG_LEVEL` / `OPSHUB_LOG_FORMAT` / `OPSHUB_DEBUG` / `OPSHUB_LOG_FILE` だけを参照する ([`src/opshub/mcp/server.py:serve_stdio`](../src/opshub/mcp/server.py))。MCP 応答にトークン情報を渡さない契約 (ADR-0022 §(b)) は `--debug` 相当でも維持される。

agent host 側で「ツールが見えない」場合は次も確認する:

- `opshub init` を済ませてあるか (DB が無いと engine wiring が落ちる)
- `uv tool install "ozzylabs-opshub[mcp]"` で `mcp` extras が入っているか
- `opshub mcp tools` で tool 一覧が見えるか (server を起動せずに registry を表示する)

### 3.3 embedding / LLM backend のエラー

```bash
opshub --debug recall "..."
opshub --debug brief "<topic>"
opshub --debug propose generate "<topic>"
```

`ConfigError: <provider> backend not configured` 系のメッセージが出る場合:

- `~/.config/opshub/config.toml` の `[embedding]` / `[llm]` セクションで backend を指定しているか (`disabled` がデフォルト)
- API backend の場合は `opshub connector auth set embedder:openai` / `embedder:voyage` / `llm:anthropic` 等で API key を keyring に保存したか
- `(model_id, model_version)` を切替えた直後は再 embed が必要 (`opshub embeddings rebuild`)

`--debug` で出る traceback はサニタイズ済みなので、API key 形状はすべて marker 化されている。

### 3.4 暗号化 DB が開けない

`[storage] encryption = true` を有効にしたあとに DB が開けないとき:

```bash
opshub --debug db migrate
```

`ConfigError` で「encryption key not found」のように出る場合:

- OS keychain にアクセスできているか (`opshub connector auth set` 系が動くか)
- `OPSHUB_DB_ENCRYPTION_KEY` env var を一時的にエクスポートして起動できるか ([ADR-0014](adr/0014-saas-token-storage.md) §Phase 7、[ADR-0021](adr/0021-encryption-at-rest.md) §(b))
- `[storage] encryption = true` を後付けで有効にしただけでは既存 DB は暗号化されない。新規 init からやり直すか、SQLCipher 公式ドキュメントに従って手動で `ATTACH DATABASE` 経由で移行する

`--debug` の traceback には keyring slot 名 (`db:encryption_key` 等) は出るが、鍵そのものは redaction processor で marker 化される。

### 3.5 source の summary が空白に見えるが、body には content がある

SaaS connector (Slack / Teams / Outlook / Gmail / Drive 等) の source preview が `opshub recall` / `opshub brief` で空白 / 改行のみで表示されるのに、本文 (`sources.body` 列) には content が入っているように見えるケース。

**症状**: `opshub recall <query>` / `opshub search <query>` の出力で source 行の snippet (`summary` 由来) が空欄だが、同じ source が `opshub search` で本文一致ヒットする (= `body` 列には content がある)。

**原因**: PR [#355](https://github.com/ozzy-labs/opshub/pull/355) 以降、`SourceObserved.summary` 列は whitespace-only 入力を `None` (DB 上 `NULL`) に正規化する設計 ([ADR-0020 §決定 (f)](adr/0020-full-local-content-retention.md))。一方 `body` 列は同 ADR §(a) の retain-everything に従って whitespace を含めて verbatim 保持する。「snippet が空 + body に content」は仕様どおりの状態であり、bug ではない (上流 SaaS の `bodyPreview` が HTML-only / image-only メールで whitespace だけになる、Slackbot 通知の `text` が空文字、Teams の `body.content` が `<div></div>` のみ等のケースで発生)。

**確認方法**: DB (`$XDG_DATA_HOME/opshub/db/opshub.sqlite`、デフォルト `~/.local/share/opshub/db/opshub.sqlite`) を直接 SQLite で開き、当該 source の `summary` と `body` を比較する (`sources.id` 列が ULID。`connector_name` + `external_id` でも一意):

```bash
sqlite3 ~/.local/share/opshub/db/opshub.sqlite \
  "SELECT id, summary IS NULL AS summary_null, length(body) AS body_len
   FROM sources WHERE connector_name = '<name>' AND external_id = '<external_id>';"
```

`summary_null = 1` かつ `body_len > 0` なら仕様どおり。`summary` が `"   "` 等の whitespace 文字列を返した場合は normalisation が効いていない = リグレッションなので、`opshub --debug connector sync <name>` で再取得しつつ issue 起票する。`[storage] encryption = true` を有効にしている場合は SQLCipher 経由でアクセスする必要がある (§3.4 参照)。

**対応**: 不要。briefing / propose / next-actions skill 側では `ItemEnqueued.summary` の fallback (`f"{source_type}: {title}"` = `slack_message: <user> in #<channel>` 等) で識別可能な preview に倒れる。projection 側 (`sources.summary`) の preview を充実させたい場合は connector mapper 側で composed summary を作る (例: Gmail mapper の `f"from: {sender}, subject: {subject}"` のような fallback shape)。

**関連**:

- [issue #332](https://github.com/ozzy-labs/opshub/issues/332) / [#337](https://github.com/ozzy-labs/opshub/issues/337) / [#343](https://github.com/ozzy-labs/opshub/issues/343)
- PR [#336](https://github.com/ozzy-labs/opshub/pull/336) / [#335](https://github.com/ozzy-labs/opshub/pull/335) / [#340](https://github.com/ozzy-labs/opshub/pull/340) / [#342](https://github.com/ozzy-labs/opshub/pull/342) / [#355](https://github.com/ozzy-labs/opshub/pull/355)
- [ADR-0020 §決定 (f)](adr/0020-full-local-content-retention.md) — summary 側 whitespace 正規化の規約
- [`src/opshub/core/text_limits.py`](../src/opshub/core/text_limits.py) — `normalise_optional_text` SSOT helper

## 4. セキュリティ注意書き

`-v` / `-vv` / `--debug` / `--log-file` を使うときに operator が知っておくこと:

- **トークン / 鍵 / 既知形状の secret は、どの verbosity でも redaction processor 経由で marker 化される** (R1 / R2 / R3 cont'd / R4)。`--debug` の full traceback、ログイベント値、`--log-file` の出力すべてに redaction が適用される。対象トークン形状: `sk-...` / `ghp_...` / `github_pat_...` / `xoxp-` / `xoxb-` / `xoxa-` / `xoxr-` / `xoxs-` / `AKIA...` / `AIza...` / `eyJ...eyJ...` (JWT) / `Bearer ...` ([`src/opshub/core/sanitise.py`](../src/opshub/core/sanitise.py) が SSOT)。
- **redaction は「既知形状」に対する防衛網であり、完全な PII scrubber ではない**。新規 SaaS が独自形式の token を導入した場合、`core/sanitise.py` の regex を拡張するまではカバーされない。PII (氏名 / メールアドレス / 社内識別子) はそもそも本文ストアに入ってくる前提なので、ログにそれが出ること自体は redaction の対象外である。
- **`--log-file` で出力したファイルは第三者と共有しない**。ファイル本体は mode 0600 で作成され (R5)、内容にも redaction が掛かるが、ログのスタックトレース / モジュールパス / 行番号 / 内部 ID は debug 情報として残る。共有が必要な場合は、対象ファイルを自分で目視確認してから渡す。
- **MCP server 応答にトークン情報を passthrough しない契約は `--debug` 相当でも維持される** (ADR-0022 §(b))。agent host のトランスクリプトに token が乗ることはない。
- **連携先の generic な API レスポンス本文** (HTML / プレーンテキストの error response 等) も `sanitise_error_message` を通る経路でログに乗る前にスキャンされる。ただし、SaaS 側が独自形式のキーをエラー文に埋め込んだ場合は前項の通り対象外になる。

参照: [SECURITY.md](../SECURITY.md) §Debug-safe logging、[ADR-0027](adr/0027-observability-and-troubleshooting-logging.md) §(b) / §(c) / §(d)。

## 5. 関連

- [ADR-0027 Observability & troubleshooting logging](adr/0027-observability-and-troubleshooting-logging.md) — 本ドキュメントの設計根拠 (5 オプション + 4 環境変数 + redaction processor + `--log-file` 0600 + `format_debug_traceback`)
- [ADR-0022 MCP Server Surface](adr/0022-mcp-server-surface.md) — MCP 境界の redaction (`mcp/_redact.py`)。本 ADR の structlog processor と独立した第二層
- [ADR-0014 SaaS Token Storage](adr/0014-saas-token-storage.md) — トークンは keyring。ログには出さない原則
- [ADR-0026 CLI Progress Reporting](adr/0026-cli-progress-reporting.md) — `--progress` / `OPSHUB_PROGRESS` の進捗表示。stdout / stderr 分離の同型先例
- [ADR-0020 Full Local Content Retention](adr/0020-full-local-content-retention.md) §決定 (a) / (f) — body retain-everything と summary whitespace 正規化の対称規約 (§3.5)
- [SECURITY.md](../SECURITY.md) — 脆弱性開示と threat model
- [docs/mcp-setup.md §8](mcp-setup.md#8-troubleshooting) — MCP serve 固有のトラブルシューティング表
