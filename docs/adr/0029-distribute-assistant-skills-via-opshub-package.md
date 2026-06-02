# 0029. Distribute Assistant Skills via opshub Package Bundling

- Status: Accepted
- Date: 2026-06-02
- Deciders: opshub maintainers

## Context

opshub をインストールしたユーザー (`uv tool install ozzylabs-opshub[mcp]`) がアシスタント 14 Skill (`personal-brief` / `next-actions` / `pr-review` / `find-document` / `meeting-prep` / `research` / `external-brief` / `decision-rationale` / `handoff-draft` / `announcement-draft` / `reply-draft` / `inbox-triage` / `source-extract` / `meeting-followup`) を即座に使えない致命的問題が Phase 12 H1 (2026-05-31) 以降ずっと続いている。

具体的には:

- [ADR-0004 §決定 (c)](0004-agent-runtime-boundary.md) (2026-05-31 Phase 12 H1 改訂) で SKILL.md SSOT は opshub `docs/skills/<name>/SKILL.md` に確定済
- 同 ADR は配信機構 (`ozzy-labs/skills` 側 CI + `@ozzylabs/skills` Renovate preset 経路、handbook ADR-0016) を「Phase 13+ に defer」とした。実際には Phase 13 (Google Workspace コネクタ) / Phase 14 (Gmail + Google Calendar コネクタ) / Phase 15 (Search 品質改善) で別優先事項が割り込み、Phase 15 完了時点でも依然 defer
- 結果として CLI 単独ユーザー (Renovate preset 未設定 = `ozzy-labs/skills` repo を clone していない / Renovate ロボットを動かしていない) は `cp -r opshub/docs/skills/* ~/.claude/skills/` を手で叩く必要があり、現実には**配信機構として機能していない**

加えて、SKILL.md SSOT が opshub `docs/skills/` に置かれている以上、`ozzy-labs/skills` 側で配布するには「opshub の `docs/skills/` を参照する CI を `ozzy-labs/skills` 側に組む」必要がある。これは「skill SSOT は opshub、配布は別 repo」という非対称構造で、handbook ADR-0016 のエコシステム共通 skill 配信機構 (drive / lint / commit 等の SSOT は `ozzy-labs/skills` 自体) と整合しない。

### Phase 10 で「opshub 同梱」が却下された経緯 (2026-05-30 時点)

[ADR-0004 Alternatives §6](0004-agent-runtime-boundary.md#6-agent-skills-を-opshub-本体に同梱--phase-10-で却下) で当時の opshub maintainer (2026-05-30) は同梱方式を 3 つの理由で却下した。本 ADR は 2026-06-02 時点で各却下理由を再評価する。

| # | Phase 10 却下理由 (2026-05-30) | Phase 16 時点の rebuttal (2026-06-02) |
|---|---|---|
| 1 | **skill lifecycle と ①コア lifecycle が乖離** — skill 改訂は頻繁、opshub 本体は semver で安定化。同梱すると skill 改訂のたびに opshub release を切る or skill を main branch で前進させる選択を強いられる | Phase 12 H1 で SKILL.md SSOT を opshub `docs/skills/` に移管した時点で、すでに「skill 改訂は opshub PR を経由」する運用が確定している ([ADR-0004 §決定 (c)](0004-agent-runtime-boundary.md))。lifecycle 乖離は移管時点で解消済。release-please による自動 release で skill 改訂は次の patch / minor release で配信される (release lifecycle の問題は releasing automation 側で吸収済) |
| 2 | **`ozzy-labs/skills` エコシステム配布機構と二重化** — ecosystem 共通 skill (drive / lint / commit 等) は `ozzy-labs/skills` preset 経由で配布されている。アシスタント skill だけ別経路にする合理性がない | アシスタント 14 skill と ecosystem 共通 skill は名前空間が disjoint (現状アシスタント 14 名は ecosystem 共通 skill 名と衝突なし) で、scope と所有者が異なる: アシスタント skill は opshub MCP surface に依存する opshub 専用 skill、ecosystem 共通 skill は repo 横断で再利用可能な開発作業 skill。二重化ではなく **scope 別所有者** の分担として整理できる (本 ADR §決定 (g) で scope 境界を明文化) |
| 3 | **複数ホスト間の skill 同期コスト** — operator が複数マシン・複数ホストで opshub を使うとき、同梱方式だと各環境で `opshub skills install` を再実行する必要が出る。preset 方式なら Renovate が自動 PR を出し続ける | `uv tool install ozzylabs-opshub` 自体が各マシン・各環境で実行される (opshub バイナリ配布の前提)。skill bundling = opshub install と同じ頻度で sync するため、同期コストは opshub install と同一 (追加コストゼロ)。Renovate 経由の自動 PR は `ozzy-labs/skills` を別途 clone / 運用しているユーザー向けの最適化で、opshub 単独ユーザーには benefit が届かない |

3 つの却下理由はすべて 2026-06-02 時点では弱い。逆に「Phase 12 H1 で SSOT 移管済、配信機構整備は 3 Phase 連続で defer された」事実から、配信を `ozzy-labs/skills` 経路に固定し続けることのコストが利益を上回っている (実ユーザー 0、`uv tool install` ユーザーはアシスタント skill を体感できないまま)。

### 設計上の制約

- **opshub は pre-userbase** (Phase 1-15 完了時点で実ユーザー 0)、compat / migration / dual-read を考慮する必要はない (`AGENTS.md` §設計判断のスタンス + opshub memory `opshub-pre-userbase-compat-stance`)
- **`uv tool install` ユーザーは非対話 path** が actual user path。CI / scripting は明示的に opt-out できる必要があるが、default は install 寄りであるべき
- **形A 不変** ([ADR-0004 §決定 (a)](0004-agent-runtime-boundary.md)) — opshub は runtime を持たず、MCP + Agent Skills のみ提供する。本 ADR は配信経路の選択であり、形A は触らない
- **外部書き戻し禁止 不変** ([ADR-0010 §禁止事項 7](0010-connector-contract.md)) — install は local FS の `~/.claude/skills/` への copy のみで、外部 SaaS には触らない
- **handbook ADR-0016 (`ozzy-labs/skills` 配布機構) と scope を carve-out する必要** — アシスタント skill だけ別経路にする合理性を明文化し、ecosystem 共通 skill との二重化ではないことを示す

## Decision

opshub Python package にアシスタント 14 Skill (SKILL.md + reference) を同梱し、`opshub skills install` で host の skill loader 配下に展開する。配信の SSOT は引き続き opshub `docs/skills/<name>/SKILL.md` (Phase 12 H1 で確定済、本 ADR で位置維持)、配信経路を `ozzy-labs/skills` Renovate preset から opshub package 同梱に切り替える。

7 軸で意思決定する。

### (a) 配布方針: opshub Python package に同梱

`pyproject.toml` の `[tool.hatch.build.force-include]` で `docs/skills` → `src/opshub/_skills` を build 時に copy する。`src/opshub/_skills` は declarative payload (SKILL.md + reference/ 配下の `.md` のみ、`.py` 不在) で、本 ADR §不変条件 で `.py` 不在を test pin する (`tests/unit/skills/test_core_boundary.py::test_skills_payload_contains_no_python`)。

- 配信 SSOT 位置は変更なし: 引き続き opshub `docs/skills/<name>/SKILL.md` ([ADR-0004 §決定 (c)](0004-agent-runtime-boundary.md))
- 配信機構: `uv tool install ozzylabs-opshub` 時点で `~/.local/share/uv/tools/ozzylabs-opshub/.../_skills/<name>/SKILL.md` が install される (uv tool の標準 layout)
- `opshub skills install` が `_skills/` を読んで host の skill loader 配下に copy する
- skill 改訂は opshub PR を経由 → release-please が patch / minor release を切る → `uv tool upgrade ozzylabs-opshub` で新版が配信される (Phase 10 却下理由 1 の lifecycle 乖離は解消)

採用しなかった代替:

- `[tool.hatch.build.targets.wheel.shared-data]` — hatch の `shared-data` は `~/.local/share/` 配下に展開する仕様だが、uv tool isolated environment では参照しづらい。package 内に `_skills/` を持ち、Python module import 経路で `importlib.resources` 経由でアクセスする方が isolated env でも safe
- 配信用 PyPI sub-package (`ozzylabs-opshub-skills` 等) — install 経路が `uv tool install ozzylabs-opshub[skills]` のような extras になり、default で install されない。本 ADR の動機 (default = install) と矛盾する

### (b) 配置パターン: フラット (host loader 互換)

install 先は `~/.claude/skills/<name>/SKILL.md` + `~/.agents/skills/<name>/SKILL.md` の **フラット配置** とする。既存 `ozzy-labs/skills` build 出力 (`ozzy-labs/skills` から `.claude/skills/` / `.agents/skills/` に展開される構造) と互換、host loader (Claude Code / Codex CLI / Copilot CLI) の expected layout と一致する。

`opshub skills install --scope user` の場合:

```text
~/.claude/skills/
├── personal-brief/
│   ├── SKILL.md
│   └── reference/...
├── next-actions/
│   ├── SKILL.md
│   └── reference/...
└── ... (14 skill 分)
```

opshub 専用 sub-directory (`~/.claude/skills/opshub/<name>/SKILL.md`) は採用しない。host loader が sub-directory を skill として認識しないケースがあり、ecosystem 共通 skill (drive / lint / commit 等) との並列性も崩れる。

### (c) install トリガー: 3 経路 + `opshub init` default = install

3 経路で install を起動できる。

1. **`opshub init`** — TTY 確認付き default = install ([§決定 (e)](#e-非対話-default--install) 参照)
2. **`opshub init --install-skills` / `opshub init --no-install-skills`** — 明示 flag で TTY 確認をスキップ
3. **`opshub skills install`** — 単独コマンド (`opshub init` 後に追加で叩く / 別 host にも install する / `--scope` 切替 / 個別 install)

`opshub init` 連携は Phase 16-C ([#384](https://github.com/ozzy-labs/opshub/issues/384)) で実装、本 ADR では契約のみ確定する。

### (d) ホスト範囲: Claude Code + Codex CLI / Copilot CLI

`opshub skills install --host {claude-code,codex,copilot,all}` で install 先を選べる。default = `all` (3 host 全部に同 14 skill を copy)。

- `claude-code` → `~/.claude/skills/` (user) / `./.claude/skills/` (project)
- `codex` → `~/.agents/skills/` (user) / `./.agents/skills/` (project) [^codex-shared-with-copilot]
- `copilot` → `~/.agents/skills/` (user) / `./.agents/skills/` (project) [^codex-shared-with-copilot]
- `all` → 上記すべて

[^codex-shared-with-copilot]: Codex CLI と Copilot CLI は同じ `~/.agents/skills/` loader を共有する ([handbook ADR-0016](https://github.com/ozzy-labs/handbook/blob/main/adr/0016-create-skills-repo.md))。`opshub skills install --host all` は `~/.claude/skills/` + `~/.agents/skills/` の 2 directory に copy する (重複なし)。

Gemini CLI は SKILL.md を host loader として読み込まない (AGENTS.md 経路) ため、本 ADR の install 対象外。Gemini ユーザーは AGENTS.md 経由で MCP を叩く ([`docs/assistant-agent.md`](../assistant-agent.md))。

### (e) 非対話 default = install

`opshub init` を `--install-skills` / `--no-install-skills` flag 未指定で非対話 (`sys.stdin.isatty() == False`) 実行した場合の default は **install** とする。一般的な non-interactive 安全側 default (skip) の逆向きを採る。

理由:

- 本 ADR の動機が `uv tool install` 単独ユーザー (Renovate preset 未設定) の救済
- non-interactive path = actual user path (CI / scripting が非対話 path を踏むのは想定外、`opshub init` を CI で叩くケースは稀)
- 明示的に skip したい CI / scripting ユーザーは `--no-install-skills` で opt-out できる
- `--install-skills` flag を CI で wrap する手間 (1 行追加) と、実ユーザーがアシスタント skill を体感できないまま放置される機会損失を比較し、後者の方が大きい

`opshub skills install` 単独コマンド側は TTY / 非 TTY 区別なく install を実行 (`opshub init` の対話部分のみ TTY 判定する)。

### (f) scope default = user

`opshub skills install --scope {user,project}` で install 先 scope を選べる。default = `user` (`~/.claude/skills/` + `~/.agents/skills/`)。

理由:

- アシスタント 14 skill は opshub MCP surface に紐づき、project に閉じない (アシスタントは repo 横断の operational memory を扱う)
- project scope は repo 単位で skill を pin したいケース用に予約 (例: project-specific な reply 文体 / fork した SKILL.md を試したい)
- project scope は `./.claude/skills/` / `./.agents/skills/` (CWD 配下) に copy する

### (g) 衝突戦略: default 上書き、`--skip-existing` で手編集保護

install 先に既存 SKILL.md がある場合の default 動作は **上書き** とする。SSOT 同期 (opshub `docs/skills/` ← `~/.claude/skills/<name>/`) を維持するため。

- `opshub skills install --skip-existing` で既存 SKILL.md を保護 (手編集を保持したい場合)
- 上書きは fingerprint (mtime + size) 比較ではなく無条件で copy (default の単純さを優先)
- `~/.claude/skills/<name>/SKILL.md` を operator が手編集した状態で `opshub skills install` を default 実行すると編集が失われる。これは default 上書きの trade-off で、`--skip-existing` で明示的に opt-out 可能

### (h) ADR-0011 (ecosystem adoption) からの scope carve-out

[ADR-0011 (Ozzy-Labs Ecosystem Adoption)](0011-ozzy-labs-ecosystem-adoption.md) は opshub が ozzy-labs エコシステム共通 skill 配信機構 (`@ozzylabs/skills` Renovate preset) に full participate する方針を確定している (§決定 2)。本 ADR はアシスタント 14 skill のみ scope を carve out し、それ以外の ecosystem 共通 skill (drive / lint / commit / ship / pr / review / health / implement / phase-issue / topics / commit-conventions / lint-rules / test の 13 件) は引き続き `@ozzylabs/skills` preset 経由で配信する。

carve-out の根拠:

- **scope の所有者が異なる**: アシスタント 14 skill は opshub MCP surface ([ADR-0022](0022-mcp-server-surface.md)) に依存する opshub 専用 skill で、opshub の SKILL.md SSOT ([ADR-0004 §決定 (c)](0004-agent-runtime-boundary.md)) と一体運用される。ecosystem 共通 skill は repo 横断で再利用可能な開発作業 skill で、opshub の MCP surface に依存しない
- **名前空間 disjoint** (不変条件): アシスタント 14 skill 名 (`personal-brief` / `next-actions` / `pr-review` / `find-document` / `meeting-prep` / `research` / `external-brief` / `decision-rationale` / `handoff-draft` / `announcement-draft` / `reply-draft` / `inbox-triage` / `source-extract` / `meeting-followup`) と ecosystem 共通 skill 名 (`drive` / `lint` / `commit` / `ship` / `pr` / `review` / `health` / `implement` / `phase-issue` / `topics` / `commit-conventions` / `lint-rules` / `test`) は disjoint で、両者が同じ `~/.claude/skills/` directory に共存しても衝突しない
- **install 経路の独立性**: opshub package 同梱経路は `opshub skills install` で起動、`@ozzylabs/skills` Renovate preset 経路は Renovate ロボットによる PR 経由で起動。両者は独立に動作し、互いに上書きしない (前者は 14 skill 名のみ touch、後者は 13 skill 名のみ touch)

将来、アシスタント skill 名と ecosystem 共通 skill 名が衝突した場合は本 ADR を改訂する (現状 disjoint の不変条件は名前空間設計時に維持する責務 = opshub maintainer)。

## 不変条件

本 ADR で確立する不変条件と pin test:

1. **`src/opshub/_skills/` 配下は declarative payload (`.py` 不在)** — skill bundle は SKILL.md + reference/ 配下の `.md` (および binary なら image など) のみで構成し、Python module を含めない。`tests/unit/skills/test_core_boundary.py::test_skills_payload_contains_no_python` で pin する (`_skills/` 不在時は skip = Phase 16-A では skip 動作 / Phase 16-B 着地後に active 化)
2. **アシスタント 14 skill 名と ecosystem 共通 skill 名は disjoint** — 名前空間衝突を構造的に避ける ([§決定 (h)](#h-adr-0011-ecosystem-adoption-からの-scope-carve-out) 不変条件)。新規 skill 追加時は両 list を確認する
3. **SKILL.md SSOT 位置は opshub `docs/skills/<name>/SKILL.md`** ([ADR-0004 §決定 (c)](0004-agent-runtime-boundary.md))。本 ADR は配信経路の選択のみで、SSOT 位置は触らない
4. **package bundle = SSOT の copy** — build 時に `docs/skills/` から `src/opshub/_skills/` へ copy する経路は 1 方向 (SSOT → bundle)、逆方向の bundle → SSOT は禁止

## 採用しなかった代替

### 1. `ozzy-labs/skills` Renovate preset 配布完成を待つ (Phase 10 で採用された方針の維持)

却下理由:

- Phase 13 / 14 / 15 の 3 Phase 連続で defer された (各 Phase で別優先事項が割り込んだ)、Phase 16 でも同じ defer が続くリスクが高い
- `ozzy-labs/skills` 側の CI を整備するコストは opshub package 同梱より大きい (opshub `docs/skills/` を参照する CI を別 repo に組む = 二重 sync 機構)
- 配信機構整備中も `uv tool install` ユーザーはアシスタント skill を体感できない (機会損失)
- Phase 10 却下理由 1-3 はすべて 2026-06-02 時点で弱い (上述 rebuttal 表)

### 2. opshub に「opshub skills push」のような Web 経路を追加

却下理由:

- opshub は形A ([ADR-0004 §決定 (a)](0004-agent-runtime-boundary.md)) で外部書き戻し / 外部 push 経路を持たない
- local FS への copy は外部書き戻しではない (ADR-0010 §禁止事項 7 違反ではない) が、Web 経路を追加すると形A の不変条件が崩れる
- Web 経路は本 ADR の動機 (local `uv tool install` ユーザーの救済) と直交する

### 3. 配信用 PyPI sub-package (`ozzylabs-opshub-skills` 等)

却下理由:

- install 経路が `uv tool install ozzylabs-opshub[skills]` のような extras になり、default で install されない
- 本 ADR の動機 (default = install) と矛盾
- sub-package の maintenance コスト (release / version 同期 / extras 管理) が opshub package 同梱より大きい

### 4. handbook ADR-0016 を改訂し ecosystem 共通 skill 経路の上にアシスタント 14 skill を載せる

却下理由:

- handbook ADR-0016 は org 横断方針で、opshub 専用 skill のために改訂する合理性が薄い (org 内他 repo に対する影響を波及させる)
- アシスタント 14 skill は opshub MCP surface ([ADR-0022](0022-mcp-server-surface.md)) に依存する opshub 専用 skill で、ecosystem 共通 skill とは scope の所有者が異なる ([§決定 (h)](#h-adr-0011-ecosystem-adoption-からの-scope-carve-out))
- scope carve-out (本 ADR で確定) の方が org 横断方針 (handbook ADR-0016) を維持しつつ opshub 専用の問題を解決できる

## Consequences

### Positive

1. **`uv tool install ozzylabs-opshub` ユーザーがアシスタント 14 skill を即座に使える** — Phase 16-B 着地後、`opshub init` 経路で skill が host に配信される
2. **skill lifecycle と opshub release lifecycle が一致** — skill 改訂は opshub PR を経由 → release-please で自動 release。Phase 10 却下理由 1 が完全に解消
3. **scope 境界の明文化** — アシスタント skill (opshub) と ecosystem 共通 skill (`ozzy-labs/skills`) の所有者・経路・名前空間が disjoint に整理される ([§決定 (h)](#h-adr-0011-ecosystem-adoption-からの-scope-carve-out))
4. **multi-machine sync の解消** — `uv tool install` 自体が各 machine で実行されるため、同期コストは opshub install と同一 (Phase 10 却下理由 3 が完全に解消)
5. **dogfood 経路が成立** — in-repo `.claude/skills/<assistant>/` populate (本 ADR §dogfood) で opshub maintainer 自身がアシスタント skill を repo 内で発火できる

### Negative / Trade-offs

1. **opshub install image の肥大化** — `docs/skills/` (14 SKILL.md + reference/) を `_skills/` に同梱するため、wheel size が増える (実測は Phase 16-B で確認)。M6 cold-start guard (`opshub --help` ≤ 300 ms、ADR-0006) には影響なし (`_skills/` は import time に読み込まれない declarative payload)
2. **`@ozzylabs/skills` Renovate preset 経路がアシスタント skill には届かない** — Renovate 自動 PR でアシスタント skill を upgrade する経路は失われる。trade-off として `uv tool upgrade ozzylabs-opshub` で代替する (Renovate を `ozzylabs-opshub` package そのものに向ければ同じ自動 PR 経路で skill も upgrade される)
3. **`opshub skills install` 実行を operator が能動的に叩く必要** — `opshub init` 連携 ([§決定 (c)](#c-install-トリガー-3-経路--opshub-init-default--install)) で TTY 時に自動起動するが、非対話 path は `--install-skills` の default install ([§決定 (e)](#e-非対話-default--install)) または明示的に `opshub skills install` を CI に組む
4. **手編集された host SKILL.md が default 上書きで失われる** ([§決定 (g)](#g-衝突戦略-default-上書き--skip-existing-で手編集保護)) — `--skip-existing` で opt-out 可能だが default 動作は破壊的。SSOT 同期を優先する trade-off

### dogfood

opshub repo 内 `.claude/skills/<assistant>/` (project scope) にアシスタント 14 skill を populate するかどうか:

**採用 (adopt)**。

理由:

- **opshub maintainer 自身がアシスタント skill を発火する状況が現実的** — Claude Code から opshub MCP server を叩いて状況確認 (`personal-brief`) / 関連資料探索 (`find-document`) / PR レビュー支援 (`pr-review`) する場面が想定される。dogfood が品質保証の自然な経路 (opshub memory `pursue-ideal-over-legacy-decisions`)
- **pre-userbase での実証経路** — 実ユーザー 0 の段階で opshub maintainer が dogfood しない限り、アシスタント skill の routing / MCP tool dispatch の品質を実証する経路がない (opshub memory `opshub-pre-userbase-compat-stance` の前提下で dogfood が唯一の実用検証)
- **scope = project の使用例にもなる** — `--scope project` flag ([§決定 (f)](#f-scope-default--user)) の dogfood case を opshub 自身が示せる (推奨案として外部ユーザーに示せる)

dogfood は Phase 16-D ([#385](https://github.com/ozzy-labs/opshub/issues/385)) で実行する。Phase 16-B (`opshub skills install` 着地、[#383](https://github.com/ozzy-labs/opshub/issues/383)) が前提依存。

将来 dogfood 経路を縮退させたくなった場合 (例: in-repo SKILL.md が opshub `docs/skills/` SSOT と drift する管理コストが顕在化した場合) は、本 ADR の dogfood 節を改訂し Phase 16-D を closeout する。

## 関連

- [ADR-0004 Agent Runtime Boundary](0004-agent-runtime-boundary.md) — §決定 (c) で SKILL.md SSOT 位置を確定 (Phase 12 H1)、本 ADR §決定 (a) で配信経路を確定 (Phase 16-A)。§決定 (c) は本 ADR にリンク委譲
- [ADR-0011 Ozzy-Labs Ecosystem Adoption](0011-ozzy-labs-ecosystem-adoption.md) — §決定 2 (skills_adapters opt-in) からの scope carve-out (本 ADR §決定 (h))
- [ADR-0022 MCP Server Surface](0022-mcp-server-surface.md) — アシスタント skill が叩く MCP tool surface (アシスタント skill は MCP surface に依存する opshub 専用 skill である根拠)
- [ADR-0016 Action Loop and Structured Output](0016-action-loop-and-structured-output.md) §決定 (l) — draft 系統一方針 (handoff-draft / announcement-draft の text-only 境界)
- [Phase 16 Tracking Issue (epic)](https://github.com/ozzy-labs/opshub/issues/381) — 本 ADR は Phase 16-A の起点
- [Phase 16-B (#383)](https://github.com/ozzy-labs/opshub/issues/383) — `[tool.hatch.build.force-include]` + `opshub skills install` / `opshub skills list` CLI 実装
- [Phase 16-C (#384)](https://github.com/ozzy-labs/opshub/issues/384) — `opshub init` 連携 (TTY prompt + `--install-skills` / `--no-install-skills` flag)
- [Phase 16-D (#385)](https://github.com/ozzy-labs/opshub/issues/385) — in-repo dogfood (本 ADR §dogfood で **採用**)
- handbook ADR-0016 (`ozzy-labs/skills` 配布機構、本 ADR §決定 (h) の scope carve-out 対象)
