# 0031. CLI Command Surface Organization (noun-first / per-noun group)

- Status: Accepted
- Date: 2026-06-03
- Deciders: opshub maintainers

## Context

Phase 1-15 完了時点で `opshub` CLI は connector / embedder / llm / events / projection / mcp 等の混在する top-level group を持つ。Phase 10 ([ADR-0006](0006-cli-first-mvp.md) §決定) で「CLI が agent 接続の唯一経路」、Phase 12 ([ADR-0022](0022-mcp-server-surface.md)) で MCP server 面が CLI と並列に追加された結果、CLI 表面は connector 系の verb / noun が散在する状態になっている。

具体的に表面化している不整合は 3 点:

1. **`connector` 親 group の冗長性** — `opshub connector slack conversations` / `opshub connector sync slack` は `connector` という親 group を間に挟むことで「connector の slack の conversations」「connector の sync の slack」と 3 階層になる。Phase 14 H1 ([#374](https://github.com/ozzy-labs/opshub/issues/374)) で `--since` 対応を追加した際にこの 3 階層構造の冗長性が再認識された。`gh repo` / `aws s3` 等の業界標準 CLI は noun を top-level group に置く 2 階層 (`<noun> <verb>`)
2. **`connector auth set` 配下の同居問題** — Phase 12 で `embedder` / `llm` 認証 (OpenAI / Anthropic / Voyage 等の API key) を `opshub connector auth set` の sub-target として実装してしまい、`opshub connector auth set openai_embedder` / `opshub connector auth set anthropic_llm` のような形になった。`connector` group に connector 以外 (embedder / llm) が同居しており、group 名と中身が一致しない
3. **`opshub connector list` の文法** — single-word noun `connector` を verb `list` の subject として扱う形 (`connector list`) は英語として不自然 (動詞 `list` の補語が単数 noun 1 つ)。一覧コマンドは `opshub connectors` (複数形 noun 単独) のほうが自然 (`gh repo list` ではなく `gh repos` 相当)

### 既存設計判断との関係

- [ADR-0006 (CLI-first MVP)](0006-cli-first-mvp.md) §決定で「CLI を唯一の agent 接続経路」と確定し、CLI 設計原則 4 項 (`--format md|json|tsv` 切替 / read 副作用なし / write `--dry-run` / permission 摩擦削減) を pin。本 ADR は同原則を踏まえつつ、**top-level group の組織方針** (verb-first vs noun-first / 階層数) を ADR-0006 では未確定だった層に追加する
- [ADR-0010 (Connector Contract)](0010-connector-contract.md) は Connector Protocol + 責務 / 禁止事項を pin するが、CLI dispatch 経路の組織方針 (group 階層 / 命名) は ADR-0010 の scope 外
- [ADR-0014 (SaaS Token Storage)](0014-saas-token-storage.md) は keyring key 規約 (`connector:<name>:<purpose>`) + env var override (`OPSHUB_CONNECTOR_<NAME>_<PURPOSE>`) を pin するが、`auth set` / `auth test` CLI のコマンド階層は ADR-0014 の scope 外
- [ADR-0022 (MCP Server Surface)](0022-mcp-server-surface.md) は MCP tool surface (stdio transport / token passthrough 禁止 / read/write 分離) を pin するが、CLI 表面は ADR-0022 の scope 外 (CLI と MCP は並列の agent-facing surface)

### pre-userbase スタンス

opshub は Phase 1-15 完了時点で実ユーザー 0 (opshub memory `opshub-pre-userbase-compat-stance.md`)。compat / migration / dual-read / deprecation period は提案しない方針が確定しており、ideal end-state を直接出して clean に切り替えるのが本 ADR の前提。

## Decision

CLI 表面を **noun-first / per-noun group** に再編する。7 軸で意思決定する。

| # | 軸 | 決定 |
|---|---|---|
| 1 | top-level group の単位 | **noun** (`opshub <connector> <verb>`)。verb-first パターン (`opshub sync <connector>`) は不採用 |
| 2 | connector 一覧 | `opshub connectors` (複数形 noun 単独)。旧 `opshub connector list` を置換 |
| 3 | 認証コマンドの配置 | 各 noun group に分散 (`opshub slack auth set / test` 等)。top-level `auth` group は新設しない |
| 4 | embedder / llm secret | per-noun group (`opshub embedder auth set openai` / `opshub llm auth set anthropic`) |
| 5 | OAuth flow 型 vs token paste 型 | 同一 `auth set` サブコマンドで吸収 (`--token` は token paste 型でのみ意味があり、OAuth 型では warning) |
| 6 | FS-backed connector の auth | `opshub box_drive auth set` / `opshub onedrive_drive auth set` は実装しない (no-op reject ではなく command 不在で `Usage:` exit 2) |
| 7 | backward-compat alias | 載せない (pre-userbase、旧 `opshub connector ...` は完全廃止) |

### (1) top-level group の単位: noun

CLI top-level group は noun を採る。`opshub <connector> <verb>` の 2 階層構造で、`opshub slack sync` / `opshub github sync` / `opshub ms365 sync` 等が表面となる。

採用理由:

- **業界標準 CLI との整合** — `gh repo <verb>` / `aws s3 <verb>` / `kubectl get <noun>` (引数順を見ると k8s も実質 noun-first) など、agent / operator が日常的に触れる CLI は noun-first 寄り。operator のメンタルモデルを揃える
- **階層数の最小化** — 3 階層 (`opshub connector slack sync`) → 2 階層 (`opshub slack sync`) で 1 階層削減。tab completion / `--help` の reading cost が低い
- **noun と group 名が一致** — `opshub slack` group には slack 関連 verb (`sync` / `auth` / `conversations`) のみが入る。group 名と中身が乖離しない

採用しなかった代替: verb-first (`opshub sync <connector>` / `opshub auth set <connector>`)

- group 名 (`sync`) が複数 connector を跨ぐため、ある connector に固有の verb (Slack の `conversations` 等) を追加すると group 名と中身が乖離する
- tab completion で `opshub sync <TAB>` した時に列挙されるのは connector 名 (slack / github / ...) で、これは結局 noun を後置している = noun-first と等価情報を表示する。verb-first を選ぶ意義が薄い

### (2) connector 一覧: `opshub connectors` (複数形 noun 単独)

connector 一覧は `opshub connectors` (動詞なし、複数形 noun のみ)。旧 `opshub connector list` を完全に置換。

採用理由:

- 一覧は「複数 connector の状態を示す」操作で、複数形 noun 単独で自然に表現できる (`gh repos` 相当)
- `opshub connector list` は `connector` (singular) + `list` (verb) で「1 connector を list する」のように読める文法的曖昧さがある

### (3) 認証コマンドの配置: 各 noun group に分散

`auth set` / `auth test` は各 connector noun group の sub-command として配置する。

```text
opshub slack auth set
opshub slack auth test
opshub github auth set
opshub github auth test
opshub ms365 auth set
opshub ms365 auth test
opshub box auth set
opshub box auth test
opshub teams auth set
opshub teams auth test
opshub google_workspace auth set    # Drive + Gmail + Calendar 3 connector で refresh token 共有
opshub google_workspace auth test
```

top-level `opshub auth set <connector>` group は **新設しない**。理由は (1) と同じく noun group との対称性を保つため、および `auth set` が embedder / llm にも必要 (= top-level `auth` group を作ると `auth set <something>` の `<something>` に connector / embedder / llm が混在する) で同居問題が再発するため。

### (4) embedder / llm secret: per-noun group

embedder / llm の API key (OpenAI / Voyage / Anthropic 等) は **per-noun group** で持つ。

```text
opshub embedder auth set openai
opshub embedder auth set voyage
opshub llm auth set anthropic
opshub llm auth set openai
```

採用理由:

- embedder / llm は connector ではない (SaaS から data を取り込む経路ではなく、API key を必要とする推論 / 埋め込み backend)。`opshub connector` group の sub-target に入れると group 名と中身が一致しない (現状の不整合)
- 一方で embedder / llm は noun として `connector` と並列に置ける ([ADR-0012 (Embedding Strategy)](0012-embedding-strategy.md) で embedder は独立 layer、ADR-0015 で llm も同様)
- vendor 名 (`openai` / `voyage` / `anthropic`) は `auth set` の引数として扱う (vendor ごとに `opshub embedder openai auth set` のような 3 階層を作らない、tab completion 1 段で完結)

### (5) OAuth flow 型 vs token paste 型: 同一 `auth set` で吸収

connector の認証は 2 形式が混在する:

- **token paste 型**: GitHub PAT / Slack User Token / Teams Token — operator が SaaS 側で発行した token 文字列を直接 paste する
- **OAuth flow 型**: MS365 / Box / Google Workspace — OAuth 2.0 authorization code flow、ブラウザで consent → redirect された code を paste、refresh token を取得

両形式とも `opshub <connector> auth set` の同一サブコマンドで吸収する。flow type は connector ごとに `auth.py` 内で固定 (operator が flow type を flag で切り替える必要なし)。

- `--token <value>` flag は **token paste 型でのみ意味がある**。OAuth flow 型の connector で `--token` を指定した場合は warning を出し、OAuth flow に進む (`--token` 値は無視)

採用理由:

- operator メンタルモデルの一貫性 — どの connector でも `opshub <name> auth set` を叩けば認証が始まる、flow type を意識する必要なし
- flow type 固定で実装層も simplification (connector 側の `auth.py` で flow type を `if` 分岐しない)

### (6) FS-backed connector の auth: 実装しない (command 不在で exit 2)

`box_drive` / `onedrive_drive` は local FS にマウントされた cloud-sync 経路 ([ADR-0019 (Local-FS-backed Connector)](0019-local-filesystem-backed-connector.md))。OS 側 (Box Drive / OneDrive アプリ) が SaaS 認証を担うため、opshub 側に認証経路は不要。

```text
opshub box_drive sync          # OK
opshub box_drive auth set      # 不在、Typer / Click が "Usage:" を表示して exit 2
opshub onedrive_drive sync     # OK
opshub onedrive_drive auth set # 不在、exit 2
```

採用理由:

- **no-op reject (`auth set` を accept してメッセージ出力) より command 不在 (`exit 2`) のほうが clean** — Typer / Click が `Usage:` を表示し、operator に "そもそも該当コマンドが存在しない" ことが伝わる。no-op reject は「叩いたら何かが起きそう」と誤解を招く
- FS-backed connector の前提 (operator が OS アプリで認証済) は [ADR-0019](0019-local-filesystem-backed-connector.md) §決定 で確定しており、本 ADR §決定 (6) は同方針の CLI 表面への自然な反映

### (7) backward-compat alias: 載せない (pre-userbase)

旧 `opshub connector ...` group は完全廃止する。`opshub connector list` / `opshub connector sync slack` / `opshub connector auth set slack` 等を accept する deprecation alias は実装しない。

採用理由:

- **pre-userbase** (opshub memory `opshub-pre-userbase-compat-stance.md`) — 実ユーザー 0 の段階で alias を載せる利得はゼロ、long-term maintenance コストのみ残る
- **alias 経路は test 追加コスト + 説明コスト** — 旧 / 新両経路を test pin する必要があり、`--help` / docs にも両経路を併記する必要が出る。clean に切り替えるほうが PR 1 本で完了する
- **release-please で major bump として扱う** — Phase 17-B PR2 の commit に `feat!:` + `BREAKING CHANGE:` footer を付け、`release-please` が major version bump で release する (`CHANGELOG.md` 自動生成、`docs/upgrading.md` Phase 17 行で旧 → 新対応表を提供)

## 目指す表面

Phase 17-B 完了後の CLI 表面の全体像:

```text
opshub slack sync
opshub slack auth set / test
opshub slack conversations
opshub github sync
opshub github auth set / test
opshub ms365 sync
opshub ms365 auth set / test         # OAuth paste-code
opshub box sync
opshub box auth set / test           # OAuth paste-code
opshub teams sync
opshub teams auth set / test
opshub google_workspace sync
opshub google_workspace auth set / test  # OAuth、3 connector で refresh token 共有
opshub google_mail sync              # auth 共有 (google_workspace slot)
opshub google_calendar sync          # auth 共有 (google_workspace slot)
opshub box_drive sync                # auth 不在
opshub onedrive_drive sync           # 同上

opshub connectors                    # 一覧 (旧 connector list)

opshub embedder auth set openai|voyage
opshub llm auth set anthropic|openai
```

廃止: `opshub connector` group 全体 (`connector list` / `connector sync` / `connector auth set/test` / `connector slack conversations`)。

embedder / llm / events / projection / mcp / search / brief / recall / propose / inbox / task / decision / source / graph / skills / init 等の既存 top-level group は本 ADR の scope 外 (本 ADR は connector / embedder / llm の認証 + sync 経路のみ touch)。

## non-goals

本 ADR の外側に明示的に置く事項:

- **connector contract ([ADR-0010](0010-connector-contract.md)) の変更** — Connector Protocol / mapper / cursor / 責務 1-6 / 禁止事項 1-7 / 改訂 (a)-(m) は不変。本 ADR は CLI dispatch 経路の組織方針のみを扱い、connector 実装層には触れない
- **MCP server surface ([ADR-0022](0022-mcp-server-surface.md))** — `opshub mcp serve` 経由の tool surface (stdio / token passthrough 禁止 / read/write 分離 / OTel naming) は無関係。CLI と MCP は並列の agent-facing surface で、本 ADR は CLI 側のみを扱う
- **keyring key 名 ([ADR-0014](0014-saas-token-storage.md))** — `connector:slack:token` / `connector:google_workspace:refresh_token` 等の key 規約は据え置き。CLI command 経路 (`opshub <name> auth set`) のみ変更し、storage 層の key 文字列は変更しない (operator の既存 keychain 互換)
- **env var override 命名 ([ADR-0014](0014-saas-token-storage.md))** — `OPSHUB_CONNECTOR_<NAME>_<PURPOSE>` (例: `OPSHUB_CONNECTOR_SLACK_TOKEN`) は不変。`_env_var_for_key()` 変換規則も据え置き
- **`connectors/` package layout** — `auth.py` + `fetcher.py` + `mapper.py` + `connector.py` の 4 module 構成、`google_auth/auth.py` shared foundation 等は不変 (内部 import 経路は CLI dispatch 経路の変更と独立)

## rebuttal: Phase 10/13/14 で `opshub connector ...` 形を採用した理由 → 2026-06 の再評価結果

Phase 10 (epic #203) で connector noun を `opshub connector <name> <verb>` の 3 階層に置いた当時の判断は、Phase 7 (Connectors Wave 2、Slack / MS365 / Box の 3 connector 追加) で「connector 系コマンドを 1 つの group にまとめると tab completion で発見しやすい」という UX 想定があった。Phase 13 (Google Workspace) / Phase 14 (Gmail + Calendar) でも同階層を踏襲した。

2026-06 の再評価で 3 点が変わった:

1. **`gh` / `aws-cli` の noun-first パターンとの整合性** — opshub の primary user は agent ホスト (Claude Code / Codex CLI 等) を介した operator で、これらの user は日常的に `gh repo <verb>` / `aws s3 <verb>` を叩いている。noun-first が agent / operator のメンタルモデル基準。3 階層の `opshub connector slack sync` より 2 階層の `opshub slack sync` のほうが業界標準に揃う
2. **Slack 専用サブグループ追加で非対称化** — Phase 14 H1 ([#374](https://github.com/ozzy-labs/opshub/issues/374)) で `opshub connector slack conversations --since` を追加した際、Slack だけが `connector slack <verb>` 配下に `sync` 以外の verb (`conversations`) を持つ非対称構造になった。他 connector に同様の verb (Outlook の `folders` / Drive の `files` 等) が追加されると同じ位置に並べる必要があり、`opshub connector <name> <verb>` の `<verb>` slot が `sync` 以外を扱う設計に既に変化している。この時点で 3 階層を維持するメリットは事実上消えた
3. **pre-userbase で alias コスト不要** — Phase 10 当時は将来の userbase を意識して安全側に倒したが、Phase 15 完了時点でも実ユーザー 0 ([ADR-0029 §Context](0029-distribute-assistant-skills-via-opshub-package.md) と同じ前提)。alias を載せない clean な切り替えが最も低コスト。`feat!` / `BREAKING CHANGE:` で 1 PR (Phase 17-B PR2) で完了する

3 点が揃ったため、Phase 10 当時の判断を 2026-06 で更新する。Phase 17 (epic [#409](https://github.com/ozzy-labs/opshub/issues/409)) でこの再評価結果を実装に落とす。

## 既存 ADR との関係

本 ADR は 4 つの既存 ADR と隣接する。各 ADR の scope と本 ADR の責務分離を明記する。

- **[ADR-0006 (CLI-first MVP)](0006-cli-first-mvp.md)** — 「CLI を唯一の agent 接続経路」「CLI 設計原則 4 項」の上位方針を pin。本 ADR は同原則を維持しつつ、top-level group の組織方針 (noun-first / per-noun group / 2 階層) を ADR-0006 では未確定だった層に追加する。ADR-0006 §決定 の 4 原則 (`--format` 切替 / read 副作用なし / write `--dry-run` / permission 摩擦削減) は本 ADR でも全て維持
- **[ADR-0010 (Connector Contract)](0010-connector-contract.md)** — Connector Protocol + 責務 / 禁止事項 + 改訂 (a)-(m) を pin。connector 本体契約 (fetch + normalize + event 化、Task / Decision / Link 直接生成禁止、write-back 禁止 等) は **不変**。本 ADR は connector の CLI dispatch surface のみを変更し、connector 実装層には触れない
- **[ADR-0014 (SaaS Token Storage)](0014-saas-token-storage.md)** — keyring key 規約 (`connector:<name>:<purpose>`) + env var override (`OPSHUB_CONNECTOR_<NAME>_<PURPOSE>`) + `core/secrets` 薄ラッパー + `secrets` extras 隔離 + Phase 7 / 11 / 13 / 14 validation を pin。keyring key 名 / env var override 命名 / storage 層実装は **不変**。本 ADR は `auth set` / `auth test` の CLI コマンド階層のみを変更 (`opshub connector auth set slack` → `opshub slack auth set`)。keyring slot 物理アドレスは ADR-0014 で確定済み、本 ADR で touch しない
- **[ADR-0022 (MCP Server Surface)](0022-mcp-server-surface.md)** — MCP tool surface (stdio transport / token passthrough 禁止 / read/write 分離 / context 効率 / OTel naming) を pin。CLI 表面と MCP tool surface は **並列の agent-facing surface** で、本 ADR は CLI 側のみを扱う。MCP tool 名 (`recall.search` / `task.create` / `connector.sync` 等) は ADR-0022 で確定済みで、本 ADR の CLI 再編に伴って変更しない

## 採用しなかった代替

### 1. verb-first 階層 (`opshub sync <connector>` / `opshub auth set <connector>`)

却下理由:

- group 名 (`sync` / `auth`) が複数 connector / embedder / llm を跨ぐため、ある noun に固有の verb (Slack の `conversations` 等) を追加すると group 名と中身が乖離する。本 ADR §決定 (1) §採用しなかった代替で論じた通り
- tab completion で `opshub sync <TAB>` した時に列挙されるのは結局 noun 名 = noun を後置している = noun-first と等価情報。verb-first を選ぶ意義が薄い

### 2. 旧 `opshub connector` group を残し、`opshub <name>` を deprecation alias として追加 (逆向き alias)

却下理由:

- pre-userbase スタンス (opshub memory `opshub-pre-userbase-compat-stance.md`) と矛盾。alias を載せる利得がゼロ
- 「正規 = 旧、alias = 新」だと release-please に major bump させる根拠が薄れ、CLI 表面の方針を変えた事実が CHANGELOG に明示されない

### 3. top-level `auth` group を新設し、`opshub auth set <connector>` で connector / embedder / llm を統合

却下理由:

- 本 ADR §決定 (3) で論じた通り、`auth set <target>` の `<target>` に connector / embedder / llm が混在し、現状の `opshub connector auth set` の同居問題が形を変えて再発する
- per-noun group (本 ADR §決定 (3)(4)) なら group 名と中身が一致し、`opshub slack` 配下は Slack 関連、`opshub embedder` 配下は embedder 関連、で clean に分離する

### 4. connector / embedder / llm の認証経路を別 CLI binary に分離 (`opshub-auth set <target>`)

却下理由:

- single binary 配布 ([ADR-0007 (Single Python Package)](0007-single-python-package.md) と整合) を破る。distribution / install の複雑度が上がる
- agent / operator が `opshub` と `opshub-auth` の使い分けを意識する必要が出る (現状 1 binary で完結する UX が劣化)

## Consequences

### Positive

1. **業界標準 CLI との整合** — `gh repo <verb>` / `aws s3 <verb>` パターンに揃い、agent / operator のメンタルモデル習得コストが下がる
2. **階層数の削減** — 3 階層 → 2 階層で tab completion / `--help` の reading cost が下がる (`opshub slack <TAB>` で sync / auth / conversations が並ぶ)
3. **group 名と中身の一致** — `connector` group の embedder / llm 同居問題が解消、各 noun group は自分の noun 関連 verb のみを持つ
4. **`opshub connectors` 単独コマンド** — connector 一覧が複数形 noun 単独で自然な英語に揃う
5. **clean 切り替え** — alias を残さないため test / docs / `--help` の負債が累積しない (pre-userbase だからこそ可能)

### Negative / Trade-offs

1. **major bump 級の breaking change** — pre-userbase なので実害は限定的だが、CHANGELOG / `docs/upgrading.md` で旧 → 新対応表を提供する必要 (Phase 17-B PR2 で対応)
2. **operator の手元 alias / shell function が壊れる** — pre-userbase で実ユーザー 0 のため影響なし想定、ただし opshub maintainer 自身が手元で alias を貼っていた場合は手で更新が必要
3. **`opshub init` 等の既存 top-level group との整合** — connector / embedder / llm 以外の top-level group (events / projection / mcp / search / brief / recall / propose / inbox / task / decision / source / graph / skills / init) は本 ADR の scope 外で touch しない。将来これらにも noun-first 原則を適用する場合は別 ADR を起票する (Phase 17 では connector / embedder / llm のみ touch)

## 関連

- [ADR-0006 CLI-first MVP](0006-cli-first-mvp.md) — CLI 設計原則の上位方針、本 ADR は同原則の延長で top-level group 組織方針を追加
- [ADR-0010 Connector Contract](0010-connector-contract.md) — connector 本体契約は不変、本 ADR は CLI dispatch surface のみ touch
- [ADR-0014 SaaS Token Storage](0014-saas-token-storage.md) — keyring key 規約 + env var override は不変、本 ADR は `auth set` / `auth test` の CLI 階層のみ touch
- [ADR-0022 MCP Server Surface](0022-mcp-server-surface.md) — MCP tool surface とは並列の agent-facing surface、本 ADR は CLI 側のみ
- [ADR-0007 Single Python Package](0007-single-python-package.md) — single binary 配布前提 (本 ADR §採用しなかった代替 4 の根拠)
- [Phase 17 Tracking Issue (epic)](https://github.com/ozzy-labs/opshub/issues/409) — 本 ADR は Phase 17-A の成果物
- [Phase 17-A (#410)](https://github.com/ozzy-labs/opshub/issues/410) — ADR-0031 新規 + ADR-0006/0010/0014/0022 cross-reference (本 PR)
- [Phase 17-B (#411)](https://github.com/ozzy-labs/opshub/issues/411) — CLI 実装 + tests + docs 一括更新 + `feat!` / `BREAKING CHANGE:`
