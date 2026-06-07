# Troubleshooting

> Phase 14 epic #317 / [ADR-0027](adr/0027-observability-and-troubleshooting-logging.md)。

OpsHub の CLI / MCP server で問題が起きたときの調査手順をまとめる。グローバルなログ verbosity / debug オプションは Phase 14 epic #317 で導入された ([ADR-0027](adr/0027-observability-and-troubleshooting-logging.md))。トークンや鍵が誤って出力されないよう、ログ全体は redaction processor を通すように設計してある。

## 1. グローバルオプション

すべてのサブコマンド (`opshub task ...` / `opshub <connector> sync` / `opshub mcp serve` 等) に共通で効くフラグ。`opshub --help` でも一覧表示できる。

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

### 3.1 `opshub <connector> sync` が失敗する

Phase 17 (ADR-0031) で旧 `opshub connector sync <name>` は per-noun group (`opshub slack sync` / `opshub github sync` / etc.) に再編済 (`docs/upgrading.md` §Phase 17)。

デフォルトの失敗表示は `ConnectorSyncFailed` event に **例外型名だけ** を残す (R3 不変条件、ADR-0014 / ADR-0022 由来)。`sync failed: <Type>` の 1 行サマリは **stderr** に、event log の `error_message` は型名のみで、例外メッセージは出さない。成功時の `synced <name>: N item(s) observed` は従来通り **stdout** なので、結果を pipe で受け取るスクリプトは影響を受けない (`opshub <connector> sync > out.txt 2> err.txt` のように分けると挙動が明快)。

原因調査には `--debug` を付けて再実行する:

```bash
opshub --debug github sync
```

`--debug` 時のみ、サニタイズ済みの例外メッセージと traceback が **stderr** に追加表示される。デフォルトの `sync failed: <Type>` stderr サマリ・event log の `error_message` 自体は変わらない (R3 cont'd) — `--debug` が増やすのは追加の stderr 行 (例外メッセージ + サニタイズ済み traceback) だけ。トークン形状はすべて marker に置換されてから出力されるため、ログを社内チャットや issue に貼っても安全。

cron 経由など、フラグを渡せない場合は環境変数で:

```bash
OPSHUB_DEBUG=1 opshub github sync
```

それでも原因が分からないときは:

- `opshub connectors` でコネクタが登録されているか
- `opshub <connector> auth set` (例: `opshub slack auth set`) で credential を保存し直す
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
- API backend の場合は `opshub embedder auth set openai` / `embedder:voyage` / `llm:anthropic` 等で API key を keyring に保存したか
- `(model_id, model_version)` を切替えた直後は再 embed が必要 (`opshub embeddings rebuild`)

`--debug` で出る traceback はサニタイズ済みなので、API key 形状はすべて marker 化されている。

### 3.4 暗号化 DB が開けない

`[storage] encryption = true` を有効にしたあとに DB が開けないとき:

```bash
opshub --debug db migrate
```

`ConfigError` で「encryption key not found」のように出る場合:

- OS keychain にアクセスできているか (`opshub <connector> auth set` 系が動くか)
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
- 既に当たっているのに hit しない場合、本文 (`sources.body`) に該当文字列が入っているかを §3.5 と同じ要領で `sqlite3` で確認する。`sources.body IS NULL` 行は **post-epic [#470](https://github.com/ozzy-labs/opshub/issues/470) で存在しない** (migration `0030_enforce_sources_body_not_null` で Phase 3-9 historical NULL 行は破棄、metadata-only 行は `body = summary` substitute、[ADR-0019](adr/0019-local-filesystem-backed-connector.md) §不変条件 (b) + [ADR-0010](adr/0010-connector-contract.md) §不変条件 6)。FTS5 / LIKE で hit しないように見える場合は (1) connector が summary 自体を populate していない、(2) sync 未実行、のいずれか
- 3 文字以上で「本文に literal phrase が存在する」のに hit しない場合は **regression なので issue 起票**。`opshub --debug search "<query>"` で再現 + 直近の migration head と DB 暗号化状態を添えて報告する

**動作仕様の補足**:

- `box 権限` のように空白で区切ると 2 token に分割され、それぞれが本文中の独立 token と一致する場合のみ hit する (literal phrase `box 権限` を本文に含む source が必要)。これは FTS5 phrase 検索の仕様であり、Phase 15 でも変わらない
- `--raw` で `box* AND 権限*` のような FTS5 boolean / prefix 構文は引き続き有効。default モードで日本語が改善された以降も、power user 向けに残してある
- MCP `search` tool ([ADR-0022](adr/0022-mcp-server-surface.md) §決定 (f)) は `raw_query` を hard-coded `false` で叩くため、アシスタント 14 Skill (`find-document` / `research` / etc.) 経由でも本改善が透過的に効く

**関連**:

- [ADR-0028 FTS5 Japanese tokenizer (trigram)](adr/0028-fts5-japanese-tokenizer.md) — 本改善の設計根拠
- [`docs/phase-15-plan.md`](phase-15-plan.md) — Phase 15 全体の plan (sub-issues S1-S4 構成 / 検証手順 / Phase 16+ outlook)
- [epic #338](https://github.com/ozzy-labs/opshub/issues/338) — Phase 15 親 epic、§背景 に operator 観測の 0 hit 事例表
- PR [#346](https://github.com/ozzy-labs/opshub/pull/346) (S1 ADR + plan) / [#363](https://github.com/ozzy-labs/opshub/pull/363) (S2 migration 0028) / [#364](https://github.com/ozzy-labs/opshub/pull/364) (S3 SearchService LIKE fallback)
- [`src/opshub/services/search_service.py`](../src/opshub/services/search_service.py) — `_MIN_FTS_QUERY_CHARS = 3` 閾値と `_search_like_fallback` 実装
- [`src/opshub/db/migrations/versions/0028_rebuild_sources_fts_trigram.py`](../src/opshub/db/migrations/versions/0028_rebuild_sources_fts_trigram.py) — tokenizer 物理張り替え + back-fill + trigger 再作成

### 3.7 Slack の channel ID が分からない / `[connectors.slack] channels` に何を書けばよいか

Slack connector を有効化するには `opshub.toml` の `[connectors.slack] channels = ["C012345...", ...]` に **channel ID** を列挙する必要がある。Slack Web UI から「リンクをコピー → URL 末尾」を読む手作業はチャネル数が多いワークスペースで現実的でない。

**手順** (Phase 14.x [#341](https://github.com/ozzy-labs/opshub/issues/341) で初出、Phase 15+ [#366](https://github.com/ozzy-labs/opshub/issues/366) で `channels` → `conversations` 刷新 = `users.conversations` 切替 + DM/MPIM 統合 + 進捗表示、[#374](https://github.com/ozzy-labs/opshub/issues/374) で type 別固定ソート + `--since` activity フィルター追加、Phase 19-B [ADR-0034](adr/0034-slack-engagement-axis.md) で engagement 軸 = 自分が発言した channel を導入、Phase 19-D [ADR-0035](adr/0035-slack-sort-axis-consolidation.md) で `--activity` flag を `--sort=name|last_self_post|last_activity` に統合 + default `--format=toml` 切替):

```bash
opshub slack auth set                                       # 既存。User Token (`xoxp-`) 推奨
opshub slack conversations                                  # default: TOML 出力 / `--sort=name` (display_name 昇順) / probe なし
opshub slack conversations --format=table                   # pre-19-D 表形式 (eyeball / script 互換)
opshub slack conversations --filter eng                     # name / participant に "eng" を含む conversation のみ
opshub slack conversations --types public,private           # public / private channel のみ (DM/MPIM を除外)
opshub slack conversations --include-archived               # archived channel も対象
opshub slack conversations --all                            # workspace 全体 (`conversations.list` 経由、joined 外も含む)
opshub slack conversations --since 30d                      # name 順 + engagement 軸 implicit default (ADR-0035 §(d)): 直近 30 日に **自分が発言した** channel のみ、LAST_POST 列、`search:read` scope 必要
opshub slack conversations --sort=last_self_post            # 自分の最終投稿日時 降順 (engagement 軸明示。`--since` なしは暗黙 90d cutoff + stderr notice)
opshub slack conversations --sort=last_activity --since 30d # 旧挙動: 直近 30 日に **誰かが発言した** channel 降順 (per-conv 1 conversations.history 呼出、LAST_ACTIVITY 列、broadcast / announcement-only 含む、`*:history` scope 必要)
opshub slack conversations --since 2026-05-01 --format=toml  # 絶対日付指定 (ISO 8601。YYYY-MM-DD は UTC 0:00 解釈、`+09:00` 付き timezone も可)
```

default `--format=toml` の出力を `~/.config/opshub/config.toml` の `[connectors.slack]` セクションに貼り、不要行を消すだけで sync 対象が確定する (ADR-0035 §(a))。`--since` 指定時 / `--sort=last_*` 指定時は TOML コメントにも `# <name> (public, last post 2026-05-30)` (engagement 軸) / `# <name> (public, last 2026-05-30)` (any 軸) 形式で activity 日付が付くので、レビュアが「最近動いている channel か」を一目で判断できる。

**sync の取得範囲を絞る (Phase 20 / [ADR-0036](adr/0036-slack-sync-date-floor.md))**: ボリュームの大きい channel で初回 sync が重い場合、`[connectors.slack] sync_since` で日付 floor を設定すると、それより古いメッセージを `opshub slack sync` が取得しなくなる。相対 (`"90d"` / `"4w"`、sync 実行時点で評価) でも ISO 絶対日付 (`"2026-01-01"`) でも指定でき、未設定なら従来どおり全件バックフィルする。特定 channel だけ全件取りたい場合は table 形式で `[[connectors.slack.channels]] id=... / since="all"` と書く (貼り付けた `channels = ["C..."]` 文字列配列もそのまま有効)。

floor 関連の挙動でよくある質問:

- **floor を設定したのに既存の古いメッセージが消えない**: 正常。floor は「これ以降のみ取得する」下限であり、既に sync 済みのメッセージを削除しない。
- **floor を有効にしたのに再取得が走らない / 既存 channel に効かない**: 正常。per-channel cursor が authoritative (`oldest = max(cursor, floor)`) なので、cursor が floor より新しい既存 channel では floor は inert になる。floor が効くのは初回 sync・新規追加 channel・cursor が floor より古い場合のみ。
- **floor を下げた (例 `90d` → `365d`) のに古い履歴が戻ってこない**: 仕様。floor を下げても遡及 backfill はしない (cursor が authoritative)。古い履歴を取り直すには `opshub projections rebuild` で Slack cursor をリセットしてから sync する。
- **相対指定 `"90d"` の起点がいつなのか分からない**: sync 実行時点基準で毎回再評価される。絶対的な下限が必要なら ISO 日付 (`"2026-01-01"`) を使う。

**`--since` の値**:

- 相対 (`<N>d` = N 日前、`<N>w` = N 週前): 例 `7d`、`2w`、`90d`
- 絶対 ISO 8601: `2026-05-01` / `2026-05-01T12:00:00+09:00` / `2026-05-01T00:00:00Z`
- 月・年単位 (`1M` / `1y`) は曖昧性のため非対応 (`30d` / `365d` で代替)、時間・分単位もこのコマンドの粒度に合わないため非対応

**典型エラー**:

- `Error: Slack OAuth token is not configured` → `opshub slack auth set` を先に実行する
- `Error: Slack users.conversations failed: missing_scope (needed: 'groups:read')` → `--types public,private` で private channel を含めるには Slack App の OAuth スコープに `groups:read` を追加し (DM/MPIM listing は `im:read` / `mpim:read`)、再認可後にトークンを `auth set` で更新する ([ADR-0018](adr/0018-slack-token-principal.md) §Decision (7))
- `Error: Slack users.conversations failed: invalid_auth` → トークンが失効。`opshub slack auth set` で再登録する
- `no conversations matched (filter: '...')` (stderr 出力、exit code 0) → `--filter` 文字列を見直すか、`--types` で対象種別を広げる / `--include-archived` を付ける / `--all` で workspace-wide に切替える
- `warning: skipping mpim conversations: missing_scope (needed: 'mpim:history')` (stderr、exit code 0) → `--since` 使用時に `conversations.history` の scope が type 単位で欠けると、該当 type 全体を出力から外す + 1 度だけ warning を出す。他 type の結果はそのまま表示される。必要な type の `*:history` scope (`channels:history` / `groups:history` / `im:history` / `mpim:history`) を Slack App に追加するか、`--types` で該当 type を外す
- `warning: skipped 3 inaccessible channels (channel_not_found=2, not_in_channel=1). ...` (stderr、exit code 0) → `--since` 使用時に `conversations.history` が一部の行で `channel_not_found` / `not_in_channel` を返した場合、該当行のみを出力から落とし、call 終了時に 1 件だけ aggregate warning を出す ([PR #405](https://github.com/ozzy-labs/opshub/pull/405))。原因マップ:
  - `channel_not_found`: Slack Connect / 外部共有チャネル (一覧には載るが history は外部 workspace の領域)、deactivated user との `im` (DM)、list と probe の間で archive / leave / delete された race、Enterprise Grid の DLP / e-Discovery で history のみブロック。**多くは構造的で operator 側のリカバリ手段なし**
  - `not_in_channel`: principal が private channel から外された / 自身が leave した。Slack UI 上で再 join するか `/invite` で戻すと次回以降の listing で hit する
  - 該当 channel id を特定する場合は `--debug` (または `-vv`) を付けて再実行する。skip した行ごとに `slack.conversations.history.row_skipped` event が `channel_id` / `error_code` / `conversation_type` を伴って出力される ([PR #407](https://github.com/ozzy-labs/opshub/pull/407))
  - 注意: sync hot path (`opshub slack sync`) は `opshub.toml` で明示指定された channel id を fetch するため、同じ error code を **fail-fast** 扱いする (config drift を検知させる意図)。discovery と sync で error semantics が意図的に非対称なのは、discovery = 動的列挙 / sync = 明示指定の責務差に基づく
- `Invalid value for '--since': '<入力>' is not a recognised value` (exit code 2) → 相対は `<N>d` / `<N>w`、絶対は ISO 8601 (`2026-05-01` / `2026-05-01T00:00:00Z`) のみ受け付ける
- `unknown --sort value 'foo'; choose one of name, last_self_post, last_activity` (exit code 2) → `--sort` は `name` (default、display_name 昇順) / `last_self_post` (engagement 軸 ts 降順) / `last_activity` (any-author 軸 ts 降順) のみ。タイポ / Phase 19-D 以前の `--activity={mine|any}` 表記を参照しているのが原因
- `Error: --all is incompatible with engagement-axis sort (--sort=last_self_post or --sort=name + --since; ...) use --sort=last_activity for workspace-wide activity.` (exit code 1) → `--all` + engagement 軸経路 (`--sort=last_self_post` または `--sort=name + --since`) の組合せ非両立 ([ADR-0034](adr/0034-slack-engagement-axis.md) §決定、[ADR-0035](adr/0035-slack-sort-axis-consolidation.md) §(f))。`search.messages` は principal が member の channel しか index しないため、workspace-wide listing と engagement 軸の積集合は joined-only と同義になる。`--sort=last_activity` (any 軸) を明示するか `--all` を外す
- `notice: --sort=<sort> defaulted to --since 90d to cap probe cost; pass --since explicitly to override.` (stderr、exit code 0、1 度だけ) → `--sort=last_self_post|last_activity` を `--since` なしで指定したため、暗黙 `--since 90d` cutoff が当たった (ADR-0035 §(e))。全期間 search が必要なら `--since 365d` 等で明示的に override する

トークン値・API レスポンス本文はどの出力経路 (stdout / stderr) にも出ない。`--debug` を付けた場合の追加 traceback もサニタイズ済み (§3.1 と同じ redaction processor が効く)。

### 3.7a engagement 軸 (`--sort=last_self_post` / `--sort=name + --since`) で何も表示されない / `search:read` 不足エラー

Phase 19-B ([ADR-0034](adr/0034-slack-engagement-axis.md)) で engagement 軸 (= 自分が発言した channel) を導入し、Phase 19-D ([ADR-0035](adr/0035-slack-sort-axis-consolidation.md)) で CLI 表記を `--sort=last_self_post` (engagement 軸明示) と `--sort=name + --since` (engagement 軸 implicit default、ADR-0035 §(d)) に整理した。`search.messages` 経由で自分の最近の発言 channel を index 化し、それで listing をフィルタする。**`--since` を単独で指定するだけで `search:read` 要件が発生する** (Bot Token user / `search:read` なし User Token user が混乱しないよう、`--sort=last_activity` への切替を覚えておくこと)。下記の代表的な失敗ケースとリカバリ手順:

- `Error: Slack search.messages failed: missing_scope (needed: 'search:read'). User Token must hold 'search:read' for --sort=last_self_post (or --sort=name combined with --since, which falls back to the engagement axis per ADR-0035 §(d)). Rerun with --sort=last_activity if you cannot grant the scope. See ADR-0018 §Decision (7) ...` (exit code 1) → User Token の OAuth scope に `search:read` がない。Slack App の OAuth & Permissions で `search:read` (User Token Scopes) を追加し再認可、`opshub slack auth set` でトークンを更新する。scope を増やせない (workspace policy / 管理者承認待ち) 場合は暫定的に `--sort=last_activity` で旧 `*:history` 経路に戻す
- `Error: Slack Bot Token cannot satisfy 'search:read' (engagement axis); use a User Token ('xoxp-') or rerun with --sort=last_activity.` (exit code 1) → Bot Token (`xoxb-`) は `search:read` を保持できない (Slack の制約)。User Token (`xoxp-`) を `opshub slack auth set` で再登録するか、`--sort=last_activity` で旧挙動に切替える
- `Error: Slack search.messages failed: invalid_auth` / `not_authed` / `account_inactive` / `team_not_found` (exit code 1) → User Token 失効 / 再認証 / 権限剥奪。`opshub slack auth set` で再登録する
- `notice: search.messages may lag by minutes; use --sort=last_activity for live activity.` (stderr、exit code 0、1 度だけ) → Slack `search.messages` は full-text index 経由のため数分〜数十分の lag がある (ADR-0034 §(i))。直近の発言だが engagement 軸の出力に現れない channel がある場合は、`--sort=last_activity` で `conversations.history` 直接呼出に切替えると live 状態が見える。なお、この notice は **`-q` / `OPSHUB_LOG_LEVEL` の影響を受けず**、engagement 軸経路で常に 1 度だけ stderr に出る (ADR-0034 §(i))。完全に suppress したい場合は `--sort=last_activity` を明示すること
- 出力が空 (`no conversations matched`、exit code 0) で listing 自体は機能している場合 → 過去 `<期間>` で自分が発言した channel が実際に 0 件、または lag で index に未反映。`--sort=last_activity` で any-author 経路 (broadcast / announcement-only 含む) も併せ確認する
- 該当 channel が listing には載るが engagement 軸の output から落ちている場合 → 自分は member だが発言していない (read-only 状態)。これは仕様通りの drop。`opshub --debug` で `slack.conversations.engagement_index_orphan` event の counter を確認すると、index にあるが listing にない channel 数 (Slack Connect / archived / type filter で落ちた件数) が見える (warning 化はしない、UX を汚さないため debug log 限定)

### 3.8 `opshub search` の Slack 結果 title が `unknown in #channel-name` になる

issue [#367](https://github.com/ozzy-labs/opshub/issues/367) で Slack mapper の title 形式を改善した。新形式は次の通り:

| message 種別 | title 形式 |
|---|---|
| 通常メッセージ | `{user} in #{channel}: {本文 80 字抜粋}` |
| 空 body (attachment only / file_share / Slackbot ping 等) | `{user} in #{channel}: (no text)` |
| `bot_message` subtype | `{bot_profile.name} in #{channel}: {抜粋}` (なければ `bot:{bot_id}`) |
| `channel_join` / `channel_leave` | `{user} joined #{channel}` / `{user} left #{channel}` |
| `channel_purpose` / `channel_topic` | `{user} set #{channel} purpose: {抜粋}` / `topic: {抜粋}` |
| `me_message` (`/me ...`) | `* {user} {抜粋}` |
| 上記いずれも該当しない (`user` / `bot_id` / `bot_profile` 全欠落、Slack 仕様逸脱の payload) | `unknown in #{channel}: ...` (最終 fallback、本来到達しない) |

抜粋長は [`src/opshub/connectors/slack/mapper.py`](../src/opshub/connectors/slack/mapper.py) の `TITLE_BODY_EXCERPT_CHARS = 80` で固定。改行 / 連続空白は単一空白に正規化し、80 字超過時は単一 Unicode 省略記号 (`…`、U+2026) を付与する。

**症状**: search 結果 / MCP `find-document` / `research` Skill の title が `unknown in #channel-name` のまま見える、または title に body 抜粋が含まれない。

**原因**: 既存 `sources.title` が #367 以前の古い format (`{user} in #{channel}` 単体、または bot/system message で `unknown`) のまま persist されている。merge 後の最初の sync では新規分のみ新 format で landing するため、過去 row は再生成が必要。

**対処**:

```bash
# 既存 sources projection を再構築 (titles は SourceObserved event から再 derive される)。
opshub projections rebuild
```

`projections rebuild` は event store を保持したまま全 projection を流し直すため、event log は touch されない。embeddings は **body** から生成しており title 変更の影響を受けないので **再生成不要** (`opshub embeddings rebuild` を実行する必要はない)。

**それでも `unknown` が残る場合**: source の `raw` payload に `user` / `bot_id` / `bot_profile` のいずれも含まれていない (Slack 仕様逸脱、または非常に古い custom integration)。最終 fallback として `_UNKNOWN_USER_DISPLAY` (= `"unknown"`) が surface するのは仕様。`--debug` 付きで sync を再実行し、該当メッセージの `raw` 構造を確認する:

```bash
opshub --debug connector sync slack 2> slack-sync-debug.log
# slack-sync-debug.log を grep して該当 ts の raw payload を確認
```

**関連**:

- issue [#367](https://github.com/ozzy-labs/opshub/issues/367) — Slack search title 改善 (本セクション根拠)
- [`src/opshub/connectors/slack/mapper.py`](../src/opshub/connectors/slack/mapper.py) `_build_title` / `_truncate_body` — title 組立 SSOT
- [`src/opshub/connectors/slack/fetcher.py`](../src/opshub/connectors/slack/fetcher.py) `_resolve_author_display` — author 解決 chain (user → bot_profile.name → bot_id → unknown)
- [ADR-0022](adr/0022-mcp-server-surface.md) §決定 (f) — MCP `search` tool は `raw_query` hard-coded `false` のためアシスタント 14 Skill は透過的に新 title format の恩恵を受ける

### 3.9 アシスタント 14 Skill が host (Claude Code / Codex CLI / Copilot CLI) で発火しない

`personal-brief` / `next-actions` / `find-document` 等のアシスタント 14 Skill が、Claude Code の `Skill` ツール選択画面や Codex CLI / Copilot CLI の skill catalog に現れない、または自然文 (「今日のまとめ」「次に何やる?」等) を投げても発火しないケース。

**症状**: host を起動してもアシスタント 14 Skill の description が候補に出ない。host を再起動しても改善しない。`~/.claude/skills/` または `~/.agents/skills/` 配下に SKILL.md が見当たらない。

**原因**: `opshub skills install` (または `opshub init` 経路) が未実行で payload が host 側ローダー directory に展開されていない、または以前 install したあと operator が SKILL.md を hand edit して bundle と drift している ([ADR-0029](adr/0029-distribute-assistant-skills-via-opshub-package.md))。

**確認手順**:

```bash
# 1. install 状況を一覧 (host scope = ~/.claude/skills + ~/.agents/skills)
opshub skills list --host all --scope user

# 期待値: 14 skill すべてが ``installed`` 列を表示
# - missing  -> SKILL.md が存在しない (install 未実行 or rolled back)
# - modified -> SKILL.md は存在するが bundle と byte 不一致 (hand edit / 旧版残骸)
# - installed -> bundle と byte 一致 (正常)

# 2. ローダー directory の物理確認
ls ~/.claude/skills/    # Claude Code 用 (14 skill dir)
ls ~/.agents/skills/    # Codex CLI / Copilot CLI 用 (14 skill dir)
```

**修復手順**:

```bash
# 1. 再 install (default で上書き、ADR-0029 §決定 (g): SSOT sync wins)
opshub skills install --host all --scope user

# 2. hand edit を保護したい場合は --skip-existing を付ける
#    (この場合 modified な skill は残り、missing のみ書き込まれる)
opshub skills install --host all --scope user --skip-existing

# 3. project scope に install (worktree 内 dogfood、Phase 16-D)
#    ./.claude/skills/ + ./.agents/skills/ に書き込まれる
opshub skills install --host all --scope project

# 4. install 内容を事前確認 (dry-run、書き込み 0 byte)
opshub skills install --dry-run --print-paths
```

**install 後も発火しない場合**:

- **host を再起動する**。Claude Code / Codex CLI / Copilot CLI は起動時に skill catalog を読み込むため、install 中に host が走っていると新しい SKILL.md は反映されない。
- `opshub --debug skills install --host all --scope user 2> install-debug.log` で再 install し、`install-debug.log` の `category=skill_install` event に `written` / `skipped` / `overwritten` 数値が出ているか確認する。0 件なら bundle が破損している可能性があり、`uv tool install --reinstall ozzylabs-opshub` で wheel を再 install する。
- `opshub skills install` 自体が `Error: opshub package is missing the bundled skill payload (_skills/ directory)` で exit 1 する場合、wheel が Phase 16-B build 前のもの。`uv tool install --reinstall ozzylabs-opshub` で最新 wheel に更新する。

**関連**:

- [ADR-0029](adr/0029-distribute-assistant-skills-via-opshub-package.md) — アシスタント 14 skill 配信経路 (opshub package 同梱)
- [`src/opshub/cli/skills.py`](../src/opshub/cli/skills.py) — `opshub skills install` / `opshub skills list` 実装
- [`src/opshub/_skills_resources.py`](../src/opshub/_skills_resources.py) — `SkillResourceError` (wheel 破損時 hint)
- [docs/assistant-agent.md §8](assistant-agent.md) — setup 手順 (3 host 共通)

### 3.10 `config.toml` と環境変数の優先順位

`opshub init` 後に `~/.config/opshub/config.toml` を編集しても反映されない、または `OPSHUB_*` 環境変数と TOML 設定が衝突したときどちらが効くか分からないケース。

**前提**: OpsHub の設定は **init args > env > toml > defaults** の順に評価される ([ADR-0032](adr/0032-runtime-toml-config-loading.md))。

- `~/.config/opshub/config.toml` は `opshub init` で作成され、**起動時に毎回読み込まれる**。手で値を編集すると次回起動から反映される (再 install / 再 init は不要)。
- `OPSHUB_*` 環境変数 (例: `OPSHUB_EMBEDDING__BACKEND=local` / `OPSHUB_LLM__BACKEND=anthropic`) は TOML より優先される。CI / headless / 一時的な上書き用途。
- `OPSHUB_CONFIG_DIR=<dir>` で config ディレクトリの位置を上書きできる (デフォルト `~/.config/opshub`)。複数 OpsHub インスタンスを並行運用する場合 (例: dev / prod 分離) に有用。
- TOML が存在しなくても起動失敗しない (defaults にフォールバック)。`opshub init` を実行していない fresh shell でも `opshub --help` / `opshub task list` 等は通る。

**症状別の切り分け**:

| 症状 | 確認手順 |
|---|---|
| `config.toml` を編集したのに反映されない | `OPSHUB_*` 環境変数で同 key を export していないか確認 (env > toml)。`env \| grep ^OPSHUB_` で列挙 |
| 設定が読まれているか不明 | `opshub --debug task list` 等で起動時の config load を観測 (DEBUG レベルで設定 source 経路がログに出る) |
| 起動時に `ConfigError` で fail-fast する | TOML の値が enum 制約に違反している (例: `[embedding] backend = "grok"` のような未知 backend)。stderr に該当 key と allowed values が出る |
| `~/.config/opshub/config.toml` 以外の場所を読ませたい | `OPSHUB_CONFIG_DIR=/path/to/dir` を export してから `opshub init` を実行 (TOML はその dir 配下に作成される) |

**不正値による fail-fast**: TOML / env で未知 enum / 範囲外の数値を渡すと起動時に `ConfigError` で fail-fast する (ADR-0032)。例:

```bash
# 未知 backend (allowed: disabled / local / openai / voyage)
$ OPSHUB_EMBEDDING__BACKEND=grok opshub task list
Error: ConfigError: [embedding] backend "grok" is not a valid choice
       (allowed: disabled, local, openai, voyage)
```

正しい値に修正すれば次回起動から通る。runtime fallback は行わない (Local-first + fail-fast 原則、principles.md §1)。

**関連**:

- [ADR-0032](adr/0032-runtime-toml-config-loading.md) — `config.toml` runtime 読込 + 優先順位 (`init args > env > toml > defaults`)
- [`src/opshub/core/config.py`](../src/opshub/core/config.py) — `Settings` (pydantic-settings) の SSOT
- §1 / §2 の global verbosity フラグ / 環境変数も同じ優先順位 (`CLI フラグ > 環境変数 > デフォルト`) で揃えている

### 3.11 Slack の自分宛 mention / DM digest が反映されない

Phase 18-B ([ADR-0033](adr/0033-slack-mention-demand-digest.md)) で導入された `slack_demand_digest` projection が空のままになる、`opshub slack mentions list` が想定行を返さない、`<@self>` mention が hit していないように見えるケース。

**前提**: 本 projection は新 fetch を持たず、`opshub slack sync` が append した既存 `SourceObserved` event を消費する純粋な下流追加。Self user id (Slack `U...` id) は projection 初期化時に Slack `auth.test()` 経由で 1 度だけ解決し cache する ([ADR-0033 §決定 (f)](adr/0033-slack-mention-demand-digest.md))。

**症状別の切り分け**:

| 症状 | 確認手順 |
|---|---|
| `opshub slack mentions list` が空 | (1) `opshub slack sync` を実行済みか確認 / (2) sync 後に `opshub projections rebuild` を実行 (新 projection 登録後は **既存 event を流し直す必要がある**) |
| DM だけ出て mention が hit しない | `auth.test` が失敗していて self user id が解決できていない可能性。`opshub slack auth test` で確認。一時的な workaround として `OPSHUB_SLACK_SELF_USER_ID=U12345` を export してから `opshub projections rebuild` 実行 (CI / headless 用、本番でも有効) |
| 一部 channel が `private` でなく `public` に classify されている | `SourceObserved` event は raw payload を持たないため、projection は channel id prefix (`C` / `G` / `D`) で type を判定する ([ADR-0033 §決定 (b)](adr/0033-slack-mention-demand-digest.md))。`G...` channel は全て `private` に collapse (mpim は body の `<@self>` 経路で別途検知される) |
| mention の `FROM` 列が常に `-` | Phase 18-B の `SourceObserved` event は author の Slack ID を持たない (`title` に display name は載るが id は載らない)。FROM 列に user id を埋めるのは Phase 19+ の connector 拡張に依存 (ADR-0033 §Consequences §scope 外) |
| MPIM の demand が出ない | MPIM 自体は `dm` row として出ない (Slack DM = `D...` のみ)。MPIM 内の `<@self>` mention は `demand_kind=mention` で hit する (body 経路) |

**手動で再構築する**:

```bash
opshub projections rebuild
# 既存 event を全 projection に流し直す。slack_demand_digest を含む 14 projection が冪等に再構築される。
opshub slack mentions list                       # default (all types, all kinds, limit 50)
opshub slack mentions list --types im,mpim       # DM + MPIM のみ
opshub slack mentions list --demand-kind mention # mention のみ
opshub slack mentions list --format json | jq .  # JSON 出力 (full row schema)
```

**典型エラー**:

- 出力に `slack_demand_digest: cannot resolve operator self user id` warning が出る → `opshub slack auth set` で User Token (`xoxp-`) を保存し、`opshub slack auth test` で `user_id` を確認、`opshub projections rebuild` を再実行する。または `OPSHUB_SLACK_SELF_USER_ID=U...` を export してから rebuild する
- mention 行の channel が `private` でなく `public` で表示される → ADR-0033 §決定 (b) の channel id prefix-based classification によるもの。private と mpim の区別が必要なら `opshub slack conversations` 出力と突き合わせる

**関連**:

- [ADR-0033 Slack mention / DM demand digest](adr/0033-slack-mention-demand-digest.md) — 設計根拠 + 6 軸決定の SSOT
- [`src/opshub/projections/slack_demand_digest.py`](../src/opshub/projections/slack_demand_digest.py) — projection 実装 (self user id cascade / mention literal / DM prefix 判定)
- [`src/opshub/db/migrations/versions/0029_create_slack_demand_digest.py`](../src/opshub/db/migrations/versions/0029_create_slack_demand_digest.py) — table schema (natural key + 2 CHECK + FK + 2 INDEX)
- Phase 18-C で MCP `slack.demand.list` tool が同 projection 上に薄く乗る予定 ([#430](https://github.com/ozzy-labs/opshub/issues/430))

### 3.12 Slack thread reply の取り込みが想定通りでない

Phase 20 ([ADR-0030](adr/0030-slack-thread-reply-ingestion.md) revised + landed、epic [#465](https://github.com/ozzy-labs/opshub/issues/465)) で `opshub slack sync` を thread reply (late reply 含む) も含む message 単位の全量取得に拡張した。本節は thread reply が期待どおり ingest されない / `--thread-activity-window` の調整 / cold thread reactivation / `conversations.replies` rate limit / 旧形状 cursor からの recovery の 4 系統をまとめる。

**前提**: 親も子返信も `slack_message` source_type 1 種で表現され、`thread_ts` は `SourceObserved.raw["thread_ts"]` に Slack API verbatim で保持される (Gmail / Outlook の message 単位 ingest と symmetric、ADR-0030 §不変条件 #1 #2)。`sources` projection に新 column は追加しない。

**`--thread-activity-window` のチューニング**:

`[connectors.slack] thread_activity_window` (default `"30d"`、CLI `--thread-activity-window` / 環境変数 `OPSHUB_CONNECTORS__SLACK__THREAD_ACTIVITY_WINDOW`) は Phase 2 polling 対象を絞る pruning floor。`parse_since` 共通経路 ([ADR-0036](adr/0036-slack-sync-date-floor.md)) を流用し、相対 (`"30d"` / `"4w"`) でも ISO 絶対日付 (`"2026-05-01"`) でも、`"all"` で prune 無効化指定可。

```toml
[connectors.slack]
enabled = true
thread_activity_window = "60d"   # 直近 60 日に reply のあった thread のみ polling
# thread_activity_window = "all" # 全 thread 永久 polling (rate limit budget に注意)
```

- 窓を **狭める** → 古い thread を polling から除外し API call 数を抑える。窓経過後の thread に late reply が来ても追従しない (= cold thread reactivation の意図された limitation)
- 窓を **広げる** → 既知 thread を長く polling し続けるが、Phase 2 で `threads` 軸 entry 数に比例して `conversations.replies` call が増える。Tier 3 (`50+ /min`) を圧迫する場合は `--thread-activity-window` を絞り戻す

**cold thread reactivation の limitation**:

`thread_activity_window` を経過した thread の `threads` 軸 entry は Phase 2 happy path 完了時に prune される (mid-iteration crash は resume-safe のため prune しない)。窓経過後に投稿された late reply は本経路では追従しない (`channels` 軸は親 ts を超えないため Phase 1 でも再取得されない)。再取得が必要なケース:

```bash
opshub projections rebuild
# Slack の compound cursor がリセットされ、次回 sync で全 channel + thread が cold-start で取り直される。
# WARNING: 大規模 workspace では rate limit budget と sync 時間が顕著に増える。`sync_since` で範囲を絞ってから rebuild するのが現実的。
```

将来オプションとして `opshub slack sync --include-cold-threads` のような opt-in flag を残しているが、本 Phase では実装しない (ADR-0030 §(d) `## 採用しなかった代替` の追加項目候補)。

**`conversations.replies` の rate limit (429)**:

`conversations.replies` は Slack Tier 3 (`50+ /min`)。Phase 1 で `latest_reply` 持ち親について 1 件、Phase 2 で `threads` 軸 entry 1 件あたり 1 件の追加 call が発生する。active な workspace で 429 が頻発する場合は次の順で対処:

1. `--thread-activity-window` を狭めて Phase 2 polling 対象を減らす (例: `"30d"` → `"14d"`)
2. `[connectors.slack] sync_since` ([ADR-0036](adr/0036-slack-sync-date-floor.md)) で `conversations.history` の floor を上げ、Phase 1 で追加 fetch する thread を減らす
3. `[connectors.slack] excludes.channels` / `excludes.senders` で API call が走らないよう除外 (Phase 20-A で `excludes` 該当 parent は `conversations.replies` を skip する API budget guard を追加済)

retry policy は `opshub.connectors.slack._retry.retry_on_rate_limit` helper を 4 つの call site (`_call_history` + `conversations._call_history_oldest` + `conversations._call_list` + `_call_replies`) で share しているため、3 attempts + `Retry-After` honoured + fallback 1s / 2s / 4s exponential が一律に適用される ([ADR-0030 §不変条件 #6](adr/0030-slack-thread-reply-ingestion.md))。

**旧形状 cursor (`{channel_id: ts}` flat-dict) からの recovery**:

Phase 20-B で `connector_cursors.cursor_value` を 2 軸 compound envelope (`{"channels": {...}, "threads": {...}}`) に張り替えた (ADR-0030 §不変条件 #4)。Pre-Phase-20-B な flat-dict を持つ DB で `opshub slack sync` を回すと **`ConfigError` で reject** され、次のメッセージが exit 1 で出る:

```text
Error: Slack cursor envelope is pre-Phase-20-B (flat dict). Run `opshub projections rebuild`
to migrate to the {"channels": ..., "threads": ...} compound schema. opshub is pre-userbase
and ships no silent migration (per ADR-0030 §不変条件 #4).
```

復旧手順:

```bash
opshub projections rebuild
# 既存 event を全 projection に流し直す。connector_cursors も含めて再構築される。
opshub slack sync     # 通常通り起動。新 compound envelope で cursor が persist される。
```

`channels` 軸の最大 ts は migration で正しく再計算されるため、過去 ingest 分が再取得されることはない (event store 上の `SourceObserved.raw["ts"]` から projection で max を取り直す)。`threads` 軸は次回 sync の Phase 1 で seed され直す。

**典型エラー**:

- `Error: Slack cursor envelope is pre-Phase-20-B (flat dict). Run \`opshub projections rebuild\` ...` (exit 1) → 上記「旧形状 cursor からの recovery」手順
- `Error: Slack conversations.replies failed: thread_not_found (channel=C..., thread_ts=...)` (exit 1) → 親 thread が削除された / Slack Connect / channel から自身が外れた等。ADR-0030 §Decision (b) では `conversations.replies` のエラーは fail-fast (skip しない) のため、structural な失敗は operator のリカバリ手段なし
- `Error: Slack conversations.replies failed: missing_scope (needed: 'channels:history' / 'groups:history' / 'im:history' / 'mpim:history')` (exit 1) → `conversations.replies` は channel type と同じ `*:history` scope を要求する ([ADR-0018](adr/0018-slack-token-principal.md) §Decision (7))。Slack App OAuth scope を追加し再認可、`opshub slack auth set` でトークンを更新する

**関連**:

- [ADR-0030 Slack Thread Reply Ingestion Policy](adr/0030-slack-thread-reply-ingestion.md) — 設計根拠 + 6 軸決定の SSOT (Phase 20 で revised + landed)
- [ADR-0036 Slack Sync Date Floor](adr/0036-slack-sync-date-floor.md) — `sync_since` (Phase 1 history floor)、`thread_activity_window` (Phase 2 polling prune) とは独立した floor 経路
- [`src/opshub/connectors/slack/_retry.py`](../src/opshub/connectors/slack/_retry.py) — 4 call site で share される rate limit retry helper
- Phase 20-A PR [#474](https://github.com/ozzy-labs/opshub/pull/474) / 20-B PR [#473](https://github.com/ozzy-labs/opshub/pull/473) / 20-C PR [#476](https://github.com/ozzy-labs/opshub/pull/476) — 実装の対応 PR

## 4. セキュリティ注意書き

`-v` / `-vv` / `--debug` / `--log-file` を使うときに operator が知っておくこと:

- **トークン / 鍵 / 既知形状の secret は、どの verbosity でも redaction processor 経由で marker 化される** (R1 / R2 / R3 cont'd / R4)。`--debug` の full traceback、ログイベント値、`--log-file` の出力すべてに redaction が適用される。対象トークン形状: `sk-...` / `ghp_...` / `github_pat_...` / `xoxp-` / `xoxb-` / `xoxa-` / `xoxr-` / `xoxs-` / `AKIA...` / `AIza...` / `eyJ...eyJ...` (JWT) / `Bearer ...` ([`src/opshub/core/sanitise.py`](../src/opshub/core/sanitise.py) が SSOT)。
- **redaction は「既知形状」に対する防衛網であり、完全な PII scrubber ではない**。新規 SaaS が独自形式の token を導入した場合、`core/sanitise.py` の regex を拡張するまではカバーされない。PII (氏名 / メールアドレス / 社内識別子) はそもそも本文ストアに入ってくる前提なので、ログにそれが出ること自体は redaction の対象外である。
- **`--log-file` で出力したファイルは第三者と共有しない**。ファイル本体は mode 0600 で作成され (R5)、内容にも redaction が掛かるが、ログのスタックトレース / モジュールパス / 行番号 / 内部 ID は debug 情報として残る。共有が必要な場合は、対象ファイルを自分で目視確認してから渡す。
- **MCP server 応答にトークン情報を passthrough しない契約は `--debug` 相当でも維持される** (ADR-0022 §(b))。agent host のトランスクリプトに token が乗ることはない。
- **連携先の generic な API レスポンス本文** (HTML / プレーンテキストの error response 等) も `sanitise_error_message` を通る経路でログに乗る前にスキャンされる。ただし、SaaS 側が独自形式のキーをエラー文に埋め込んだ場合は前項の通り対象外になる。

参照: [SECURITY.md](../SECURITY.md) §Debug-safe logging、[ADR-0027](adr/0027-observability-and-troubleshooting-logging.md) §(b) / §(c) / §(d)。

## 5. 関連

- [ADR-0032 Runtime TOML config loading](adr/0032-runtime-toml-config-loading.md) — `config.toml` 起動時読込 + 優先順位 `init args > env > toml > defaults` (§3.10 の設計根拠)
- [ADR-0027 Observability & troubleshooting logging](adr/0027-observability-and-troubleshooting-logging.md) — 本ドキュメントの設計根拠 (5 オプション + 4 環境変数 + redaction processor + `--log-file` 0600 + `format_debug_traceback`)
- [ADR-0022 MCP Server Surface](adr/0022-mcp-server-surface.md) — MCP 境界の redaction (`mcp/_redact.py`)。本 ADR の structlog processor と独立した第二層
- [ADR-0014 SaaS Token Storage](adr/0014-saas-token-storage.md) — トークンは keyring。ログには出さない原則
- [ADR-0026 CLI Progress Reporting](adr/0026-cli-progress-reporting.md) — `--progress` / `OPSHUB_PROGRESS` の進捗表示。stdout / stderr 分離の同型先例
- [ADR-0020 Full Local Content Retention](adr/0020-full-local-content-retention.md) §決定 (a) / (f) — body retain-everything と summary whitespace 正規化の対称規約 (§3.5)
- [SECURITY.md](../SECURITY.md) — 脆弱性開示と threat model
- [docs/mcp-setup.md §8](mcp-setup.md#8-troubleshooting) — MCP serve 固有のトラブルシューティング表
