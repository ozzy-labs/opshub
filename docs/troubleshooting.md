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

### 3.6 `opshub search` で日本語自然文がヒットしない / 短クエリで 0 hit になる

`opshub search "boxの権限"` や `opshub search "進捗記入"` のような日本語自然文、あるいは `opshub search "依頼"` / `opshub search "PR"` のような短クエリで結果が 0 件になるケース。

**症状**: `opshub search <日本語自然文>` や 1-2 文字のクエリで `no hits for '<query>'` が返る。同じ本文を含む source が `recall` (semantic) では拾える、あるいは `opshub search "<query>*" --raw` のように prefix 演算子を付けると拾える。

**原因 (Phase 14 以前)**: Phase 10 で導入した `sources_fts` (migration 0019) の tokenizer は `unicode61 remove_diacritics 2` を採用しており、空白や記号で区切られない日本語自然文を 1 つの長いトークンとして固める。このため `boxの権限` のように本文中のトークン中間部分に当たる入力は、prefix `*` を付けない限り 0 hit になる ([epic #338](https://github.com/ozzy-labs/opshub/issues/338) §背景 参照)。

**Phase 15 以降の改善** ([ADR-0028](adr/0028-fts5-japanese-tokenizer.md)、epic [#338](https://github.com/ozzy-labs/opshub/issues/338) / 子 PR [#346](https://github.com/ozzy-labs/opshub/pull/346) [#363](https://github.com/ozzy-labs/opshub/pull/363) [#364](https://github.com/ozzy-labs/opshub/pull/364)、本 PR で closeout):

- `sources_fts` の tokenizer が **FTS5 built-in `trigram`** に張り替わった (migration `0028_rebuild_sources_fts_trigram`)。3 文字以上の substring は default モードで自然にヒットする。`boxの権限` / `進捗記入` / `CDKの` のような日本語自然文も `--raw` / prefix `*` 不要で当たる。
- SearchService 層に **短クエリ (1-2 文字) の LIKE fallback** が入った。`依頼` / `PR` / `Q4` のような短い入力は `LOWER(body) LIKE LOWER(?)` の full scan path にルーティングされ、index hit はしないが結果が返る。case 比較は ASCII レベルで insensitive、クエリは NFC 正規化される。
- **`--raw` モードでは fallback 無効** (`--raw` は operator が FTS5 文法を直接書く power-user 契約のため、silent rewrite しない)。`--raw` で 1-2 文字を渡すと従来どおり 0 hit になり得る。

**確認方法**: `opshub db migrate` で migration `0028_rebuild_sources_fts_trigram` まで適用したか確認する。Alembic head が `0028_rebuild_sources_fts_trigram` であること、`sqlite_master` で `sources_fts` の DDL に `tokenize='trigram'` が含まれていることを確認できる:

```bash
sqlite3 ~/.local/share/opshub/db/opshub.sqlite \
  "SELECT sql FROM sqlite_master WHERE name = 'sources_fts';"
# 期待値: 末尾に tokenize='trigram' を含む CREATE VIRTUAL TABLE 文
```

`[storage] encryption = true` を有効にしている場合は SQLCipher 経由でアクセスする必要がある (§3.4 参照)。

**対応**:

- migration を当てていなければ `opshub db migrate` を実行する (back-fill 自動)
- 既に当たっているのに hit しない場合、本文 (`sources.body`) に該当文字列が入っているかを §3.5 と同じ要領で `sqlite3` で確認する。`sources.body IS NULL` の source (Phase 3-9 historical 行と `box_drive` / `onedrive_drive` の metadata-only 行、[ADR-0019](adr/0019-local-filesystem-backed-connector.md) §不変条件 (b)) は FTS5 / LIKE どちらの経路でも対象外
- 3 文字以上で「本文に literal phrase が存在する」のに hit しない場合は **regression なので issue 起票**。`opshub --debug search "<query>"` で再現 + 直近の migration head と DB 暗号化状態を添えて報告する

**動作仕様の補足**:

- `box 権限` のように空白で区切ると 2 token に分割され、それぞれが本文中の独立 token と一致する場合のみ hit する (literal phrase `box 権限` を本文に含む source が必要)。これは FTS5 phrase 検索の仕様であり、Phase 15 でも変わらない
- `--raw` で `box* AND 権限*` のような FTS5 boolean / prefix 構文は引き続き有効。default モードで日本語が改善された以降も、power user 向けに残してある
- MCP `search` tool ([ADR-0022](adr/0022-mcp-server-surface.md) §決定 (f)) は `raw_query` を hard-coded `false` で叩くため、秘書 14 Skill (`find-document` / `research` / etc.) 経由でも本改善が透過的に効く

**関連**:

- [ADR-0028 FTS5 Japanese tokenizer (trigram)](adr/0028-fts5-japanese-tokenizer.md) — 本改善の設計根拠
- [`docs/phase-15-plan.md`](phase-15-plan.md) — Phase 15 全体の plan (sub-issues S1-S4 構成 / 検証手順 / Phase 16+ outlook)
- [epic #338](https://github.com/ozzy-labs/opshub/issues/338) — Phase 15 親 epic、§背景 に operator 観測の 0 hit 事例表
- PR [#346](https://github.com/ozzy-labs/opshub/pull/346) (S1 ADR + plan) / [#363](https://github.com/ozzy-labs/opshub/pull/363) (S2 migration 0028) / [#364](https://github.com/ozzy-labs/opshub/pull/364) (S3 SearchService LIKE fallback)
- [`src/opshub/services/search_service.py`](../src/opshub/services/search_service.py) — `_MIN_FTS_QUERY_CHARS = 3` 閾値と `_search_like_fallback` 実装
- [`src/opshub/db/migrations/versions/0028_rebuild_sources_fts_trigram.py`](../src/opshub/db/migrations/versions/0028_rebuild_sources_fts_trigram.py) — tokenizer 物理張り替え + back-fill + trigger 再作成

### 3.7 Slack の channel ID が分からない / `[connectors.slack] channels` に何を書けばよいか

Slack connector を有効化するには `opshub.toml` の `[connectors.slack] channels = ["C012345...", ...]` に **channel ID** を列挙する必要がある。Slack Web UI から「リンクをコピー → URL 末尾」を読む手作業はチャネル数が多いワークスペースで現実的でない。

**手順** (Phase 14.x、issue [#341](https://github.com/ozzy-labs/opshub/issues/341)):

```bash
opshub connector auth set connector:slack       # 既存。User Token (`xoxp-`) 推奨
opshub connector slack channels                 # 表形式で channel 一覧を表示
opshub connector slack channels --format toml   # `[connectors.slack] channels` 用 snippet
opshub connector slack channels --filter eng    # name に "eng" を含む channel のみ
opshub connector slack channels --include-private    # private channel も対象 (`groups:read` 要)
opshub connector slack channels --include-archived   # archived channel も対象
```

`--format toml` の出力を `~/.config/opshub/config.toml` の `[connectors.slack]` セクションに貼り、不要行を消すだけで sync 対象が確定する。

**典型エラー**:

- `Error: Slack OAuth token is not configured` → `opshub connector auth set connector:slack` を先に実行する
- `Error: Slack conversations.list failed: missing_scope (needed: 'groups:read')` → `--include-private` を使うには Slack App の OAuth スコープに `groups:read` を追加し、再認可後にトークンを `auth set` で更新する ([ADR-0018](adr/0018-slack-token-principal.md) §Decision (7))
- `Error: Slack conversations.list failed: invalid_auth` → トークンが失効。`opshub connector auth set connector:slack` で再登録する
- `no channels matched (filter: '...')` (stderr 出力、exit code 0) → `--filter` 文字列を見直すか、`--include-private` / `--include-archived` で対象を広げる

トークン値・API レスポンス本文はどの出力経路 (stdout / stderr) にも出ない。`--debug` を付けた場合の追加 traceback もサニタイズ済み (§3.1 と同じ redaction processor が効く)。

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
