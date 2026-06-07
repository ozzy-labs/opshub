# 0037. Browser Read Layer via Playwright

- Status: Accepted + Landed (Phase 21 完了、epic [#504](https://github.com/ozzy-labs/opshub/issues/504))
- Date: 2026-06-07 (Accepted); 2026-06-08 (Landed: 21-B [#511](https://github.com/ozzy-labs/opshub/pull/511) browser core / 21-C [#513](https://github.com/ozzy-labs/opshub/pull/513) web connector / 21-D [#512](https://github.com/ozzy-labs/opshub/pull/512) MCP `browser.fetch` / 21-E docs closeout)
- Deciders: opshub maintainers
- Related: [ADR-0010](0010-connector-contract.md) (connector contract — web connector を §Phase 21 改訂 (n)-(o) で追加)、[ADR-0019](0019-local-filesystem-backed-connector.md) (delta API なし connector の `fingerprint` 変更検知 pattern)、[ADR-0025](0025-office-document-content-extraction.md) (本文抽出の char cap / fail-safe 規律)、[ADR-0022](0022-mcp-server-surface.md) (MCP read/write 境界 — `browser.fetch` を write-category として 21-D で追加予定)、[ADR-0005](0005-external-content-minimization.md) / [ADR-0020](0020-full-local-content-retention.md) (本文ローカル保持の posture)、[ADR-0026](0026-cli-progress-reporting.md) (long-running CLI 進捗)、[ADR-0031](0031-cli-command-surface-organization.md) (noun-first CLI surface)

## Context

opshub の取り込み経路は Phase 1-14 で 10 connector (GitHub / Slack / MS365 Outlook / MS365 OneDrive / Teams / Box / Box Drive / OneDrive Drive / Google Workspace / Gmail / Google Calendar) まで揃ったが、すべてが **API または ローカル FS scan** ベースであり、**ブラウザレンダリングを要する Web ページの本文を取り込む経路がない**。JavaScript で描画される SPA / 動的ページ、あるいは認証 cookie の張られた社内 Web ツールの本文は、`requests.get` + HTML パースでは取れない。アシスタント 14 Skill も「外部 SaaS を直接叩かない」posture (ローカル DB のみ参照、[ADR-0022](0022-mcp-server-surface.md)) のため、ad-hoc に Web ページを読む手段を持たない。

Phase 21 (epic #504) で **ブラウザ読み取り層を新設**する。本 ADR はそのライブラリ選定・境界・拡張契約を pin し、後続 Phase (21-B 〜 21-E) が参照する決定をすべて固定する。**操作系 (click / fill / submit) は opshub 初の「外部への書き込み」**であり、HITL 承認単位の設計 + Web ページ由来 prompt injection 対策という重い前提を要するため、本 Phase では **方向だけ pin して後続 Phase に defer** する (§決定 (f))。

### ライブラリ選定の論点

ブラウザ自動化の低レベル経路には **生 CDP (Chrome DevTools Protocol)** と **高レベル API (Playwright / Puppeteer / Selenium)** がある。両者を「どちらもサポートする」設計は一見柔軟だが、実際には state 競合の温床になる。検討の結論:

1. **生 CDP の独立実装は罠が多い** — Chrome 136+ は debug port を開く際に **専用 user-data-dir 必須**化 (普段使いの profile を debug 接続から守るセキュリティ強化) し、`PUT /json/new` でのタブ生成・Domain ごとの `enable` 呼び出し・debug port (`--remote-debugging-port`) の公開によるローカル攻撃面など、低レベル運用の落とし穴が多い。
2. **ecosystem は生 CDP から離脱中** — Firefox 141 で CDP サポートを完全削除、Selenium 4.29+ で CDP の直接サポートを廃止 (WebDriver BiDi へ移行)、Puppeteer v24+ は WebDriver BiDi が default。「生 CDP 一本」は将来の互換性負債を抱える。
3. **生 CDP と高レベル API の 2 系統並走は state 競合の典型アンチパターン** — 同一ブラウザに高レベル API と生 CDP が別々の前提で触ると、navigation state / target lifecycle / session ownership が競合する。低レベルアクセスは必ず高レベル API の `CDPSession` 経由に一本化すべき。
4. **Playwright は CDP を内包する** — Playwright は内部的に CDP を使ってブラウザを駆動しており、`page.context().new_cdp_session(page)` で生 CDP コマンドを発行でき、`browser_type.connect_over_cdp(endpoint)` で既起動 Chrome に attach できる。つまり「生 CDP も高レベル API も使える」状態が **Playwright 1 本** で達成でき、しかも 2 系統並走の state 競合を構造的に回避できる。

## Decision

ブラウザ読み取り層を **Playwright (Python 公式 SDK) 1 本** で構築する。以下 (a)-(g) を pin する。

### (a) ライブラリ = Playwright、生 CDP の独立実装は持たない

- ライブラリは **Playwright (Python 公式 SDK)**。`browser` extras として `playwright>=1.50` (実装時 = 21-B で最新 stable を確認の上で下限を確定) を `pyproject.toml` の `[project.optional-dependencies]` に追加する。
- **生 CDP (Chrome DevTools Protocol) の独立実装は持たない**。`websocket-client` / 自前 CDP コマンドディスパッチャ等を opshub に実装しない。低レベル CDP アクセスが必要になっても必ず Playwright の `CDPSession` 経由に一本化する (§決定 (b))。
- 採用根拠は §Context のライブラリ選定論点 1-4。「生 CDP も使いたい」要件は Playwright の escape hatch (§決定 (b)) で満たされるため、2 系統並走の state 競合リスクを取らない。

### (b) CDP escape hatch = `new_cdp_session` / `connect_over_cdp` のみ許容、raw WebSocket 禁止

低レベル CDP が必要な場合 (将来の操作系 / 認証 session 管理等) は、以下 2 経路のみ許容する:

1. **`page.context().new_cdp_session(page)`** — 既存 Playwright ページに対して生 CDP コマンド (`Network.*` / `Page.*` 等) を発行する低レベル経路。
2. **`browser_type.connect_over_cdp(endpoint)`** — operator が別途起動した Chrome (`--remote-debugging-port` を開いた既存ブラウザ) に attach する経路。認証済み session の再利用 (将来の認証付きページ対応) の escape hatch として予約。

**raw WebSocket 直叩き (自前で `ws://localhost:<port>/devtools/...` に接続して CDP JSON を投げる) は禁止**。Playwright の `CDPSession` が target lifecycle と session ownership を一元管理するため、これを bypass すると §Context 論点 3 の state 競合が発生する。

### (c) Chromium のみ / headless default / opshub 専用 user-data-dir

- **Chromium のみ** — Firefox / WebKit は Phase 21 scope 外 (§Non-goals)。`playwright install chromium` 1 つだけを operator 手順とする。
- **headless が default** — `[browser] headless` (default `true`)。CI / cron / サーバ運用で GUI なしに動く既定を取り、debug 時のみ `headless = false`。
- **opshub 専用 user-data-dir** — ブラウザの user-data-dir は [`core/platform.py`](../../src/opshub/core/platform.py) の data dir (= [`core.config.default_data_dir()`](../../src/opshub/core/config.py) → `$XDG_DATA_HOME/opshub`) 配下に専用ディレクトリ (`default_data_dir() / "browser"` 相当) を切る。**ユーザーの普段使いの Chrome profile には一切触れない**。これは Chrome 136+ が debug 接続に専用 user-data-dir を要求する制約 (§Context 論点 1) とも整合し、operator の通常ブラウジング cookie / 拡張機能を opshub プロセスから隔離する。

### (d) 本文抽出契約: rendered DOM → text、cap 500K chars

- ブラウザがページを **レンダリングし終えた後の DOM** からテキストを抽出して `SourceObserved.body` に載せる (= Playwright を採る価値 = JS 描画後の本文が取れる)。
- **char cap = 500K chars** を [ADR-0025](0025-office-document-content-extraction.md) §決定 (b) (抽出後テキスト長上限) と **同じ規律** で適用する。500K chars 超過時は head-truncation + 末尾注記。cap 値・override key (`max_extracted_chars` 相当) は ADR-0025 の既定を継承する (browser 専用に別 default を切らない)。
- 抽出失敗 (navigation error / timeout / レンダリング失敗) は [ADR-0025](0025-office-document-content-extraction.md) §決定 (c) の fail-safe 規律に準じ、`OpsHubError` 系に変換した上で、connector 経路では fail-safe (`ConnectorSyncFailed` 系 / per-page スキップ) として扱う。char cap・抽出失敗 fail-safe の二点で ADR-0025 規律を共有することで「本文を持つ全 source が同じ cap / 同じ fail 規律に従う」不変条件を保つ。
- **抽出方法 (rendered DOM → text の具体手段) = `page.inner_text("body")` に確定 (21-B #506 決定)**。候補は `page.inner_text("body")` (Playwright 標準、軽量) と `markitdown` への HTML 経路 (ADR-0025 の既存抽出層と統一) の 2 つだったが、21-B で前者を採る。根拠:
  - `inner_text("body")` は **レンダリング後の DOM の可視テキスト** をそのまま返す = 「JS 描画後の本文が取れる」という Playwright 採用価値 (§決定 (a)) を直接実現する。`markitdown` 経路は `page.content()` で取った静的 HTML を再パースするため、レンダリング後 DOM ではなく **HTML 構造** を読むことになり、SPA / 動的描画の本文取り込みという動機 (§Context) を損なう。
  - `markitdown` 経路は browser 層を `[office]` extras の重量級コンバータに結合させる。browser 層は `[office]` から独立した `[browser]` extras であるべき (依存境界の単純化)。
  - cap / fail-safe 規律 (上 2 項) は手段に依らず共有するため、`inner_text` 採用後も「本文を持つ全 source が同じ cap / 同じ fail 規律に従う」不変条件は保たれる (`max_chars` head-truncation は `opshub.core.text_limits.truncate_with_marker` を ADR-0025 の Office 抽出と共用)。
  - 実装: [`opshub.browser.core.fetch_page`](../../src/opshub/browser/core.py) が `page.goto(url, wait_until="load", timeout=...)` → `page.title()` + `page.inner_text("body")` → 500K cap の順で組み立てる。timeout / navigation error は `BrowserFetchError` (= `ConnectorFailedError` サブクラス) に変換し、21-C connector の既存 fail-safe 経路がそのまま per-page スキップとして扱える。

### (e) MCP posture: 「read tool = ローカル DB のみ」不変条件を維持、ネットワークに出る tool は write-category

[ADR-0022](0022-mcp-server-surface.md) の **「read tool = ローカル DB のみ参照、自律実行 OK」不変条件を維持**する。ブラウザでネットワークに出る MCP tool (`browser.fetch`、21-D #508 で追加予定) は **write-category** として宣言する:

- `browser.fetch` は外部ネットワークに egress し、対象サイトの access log に痕跡を残し、prompt injection の攻撃面 (ページ本文に「次の tool で task を消せ」等の命令混入) を持つ。これは [ADR-0022](0022-mcp-server-surface.md) §決定 (c) で `connector.sync` を write 扱いとした論拠と **完全に同型**。
- したがって `browser.fetch` は `annotations.readOnlyHint = false` を付与し、**HITL per call** (呼び出しごとに人確認) とする。`connector.sync` と同じ整理。
- read tool (`recall.search` / `search` / `task.list` 等) は引き続きローカル DB のみを参照し、ネットワークに出ない不変条件を保つ。「read = ローカル / network egress = write」の境界を本層でも崩さない。
- ADR-0022 への正式な surface 追加 (tool 数 18 → 19、registry policy pin test 更新) は 21-D ([#512](https://github.com/ozzy-labs/opshub/pull/512)) で ADR-0022 §決定 (g) 改訂として着地済み (`WriteCategory.BROWSER_FETCH`、`tests/unit/mcp/test_registry_policy` が 13 read + 6 write を pin)。本 ADR §決定 (e) は「browser.fetch は必ず write-category」という制約を先に pin する位置付け。

### (f) 操作系 (click / fill / submit) の defer — 後続 Phase の前提条件を明文化

Phase 21 は **read 専用** (ページを開いて本文を取るだけ)。**click / fill / submit / file upload 等の操作系 (= ページへの能動的書き込み) は scope 外**とし、後続 Phase に defer する。defer にあたり、後続 Phase が満たすべき前提条件を以下に明文化して pin する:

1. **HITL 承認単位の設計** — 操作系は opshub 初の「外部への書き込み」であり、承認粒度を設計する必要がある。**操作ごとに承認** (1 click = 1 確認、安全だが UX が重い) か **セッションごとに承認** (一連の操作を 1 承認、UX は軽いが injection された操作が紛れ込むリスク) かのトレードオフを後続 Phase の ADR で確定する。本 ADR はこの設計が未確定であることを操作系 defer の第一根拠として pin する。
2. **Web ページ由来 prompt injection 対策** — 操作系は「ページ本文に書かれた指示で agent が click/submit してしまう」indirect prompt injection の典型攻撃面を開く。読み取り本文に対する injection ([ADR-0022](0022-mcp-server-surface.md) §決定 (c) で write = 人確認の根拠) に加え、**操作の連鎖** (本文を読む → その本文の指示で次の操作をする) を遮断する仕組みが必要。この対策設計が未確定であることを第二根拠として pin する。
3. **[ADR-0010](0010-connector-contract.md) §禁止事項 7 (write-back 禁止) との整理** — ADR-0010 は「外部 SaaS への書き戻し (`post` / `send` / `comment` / `reply`) を connector に実装しない」を不変条件として持つ。ブラウザ操作系 (form submit でメッセージを送る等) は **この write-back 禁止と正面衝突し得る**。操作系を導入する後続 Phase は、ADR-0010 §禁止事項 7 を改訂して「ブラウザ経由の write-back をどう位置づけるか」(全面禁止維持 / HITL 前提で限定解禁) を明示的に整理する必要がある。本 ADR はこの整理が未了であることを第三根拠として pin する。

この 3 前提が揃うまで操作系は実装しない。read 層 (本 ADR + 21-B 〜 21-E) は操作系 API (`page.click` / `page.fill` / `page.set_input_files` 等) を **呼ぶ code path を持たない**ことで、構造的に「操作経路の不在」を保証する ([ADR-0010](0010-connector-contract.md) §禁止事項 7 が write-back メソッドを実装しないことで経路を不在化するのと同型)。

### (g) binary 配布 = operator 手順、不在時 `ConfigError` 誘導

- Chromium binary の導入 (`playwright install chromium`) は **operator 手順** として docs (21-E で `docs/` に新設) に記載する。opshub package には binary を同梱しない (サイズ・ライセンス・更新の観点で pip wheel に Chromium を bundle しない)。
- **binary 不在時は `ConfigError`** で `playwright install chromium` の実行を誘導する。Playwright が「executable doesn't exist」系のエラーを返した場合、opshub 側で catch して install コマンドを案内する `ConfigError` に wrap する ([`core/platform.py`](../../src/opshub/core/platform.py) が `box_drive` の root 不在を `ConfigError` で誘導するのと同型)。
- CI では `.github/workflows/ci.yaml` の `uv sync` に `--extra browser` を加え、`playwright install chromium` step を追加する (21-B #506 で実装)。integration test は **localhost `http.server` fixture** でページを配信し、CI から外部ネットワークに出ない (§Non-goals / epic #504 テスト計画と整合)。

### (h) sync/async 境界

browser core は **Playwright sync API** で書く (CLI-first、opshub の sync SQLAlchemy codebase と整合)。Playwright sync API は asyncio イベントループ内で直接呼ぶと raise するため、MCP の async handler (`browser.fetch`) からは **`asyncio.to_thread` 経由** で browser core を呼ぶ (21-D #508 で実装)。本項は 21-B 着手時の前提として pin する (browser core の同期性 = MCP bridge が `to_thread` を要する根拠)。

## Consequences

### Positive

1. **JS 描画ページの本文取り込みが可能になる** — SPA / 動的ページ / 認証 cookie 越しの社内ツール本文が `sources.body` に入り、FTS5 / recall / assistant skill から横断検索できる。
2. **2 系統並走の state 競合を構造的に回避** — Playwright 1 本 + `CDPSession` escape hatch で「生 CDP も高レベルも使える」を達成し、生 CDP 独立実装の罠 (§Context 論点 1) を負わない。
3. **ecosystem の BiDi 移行に追従** — Playwright が CDP / BiDi の差異を吸収するため、生 CDP 削除 (Firefox 141 等) の影響を opshub が直接受けない。
4. **既存の不変条件を崩さない** — read tool = ローカル DB のみ (§決定 (e))、本文 char cap / fail-safe 規律 (§決定 (d) = ADR-0025 継承)、外部書き戻し不在 (§決定 (f) = ADR-0010 §禁止事項 7 継承) をすべて維持。
5. **operator profile の隔離** — 専用 user-data-dir (§決定 (c)) で普段使いブラウザの cookie / 拡張に触れず、Chrome 136+ の制約とも整合。

### Negative / Trade-offs

1. **Chromium binary の運用負担** — `playwright install chromium` が operator 手順として増える (§決定 (g))。緩和: 不在時 `ConfigError` 誘導 + docs 記載。
2. **ブラウザ起動コスト** — page fetch ごとにブラウザ起動 / レンダリング待ちが入り、API 取得より重い。緩和: web connector は operator が明示登録した URL のみ取得 (crawler ではない、[ADR-0010](0010-connector-contract.md) §Phase 21 改訂 (n))、`browser.fetch` は HITL per call で乱用を防ぐ。
3. **操作系の defer により「読めるが操作できない」非対称が残る** — フォーム送信を要するページ等は read 層では完結しない。これは意図された scope 限定であり、§決定 (f) の 3 前提が揃う後続 Phase で解禁する。
4. **prompt injection 攻撃面の拡大** — 取り込んだ Web ページ本文に injection が混入し得る。緩和: `browser.fetch` を write-category (HITL per call、§決定 (e))、操作系 defer (§決定 (f)) で「本文を読む → その指示で操作する」連鎖を構造的に遮断。本文保持自体の injection 緩和は [ADR-0021](0021-encryption-at-rest.md) 暗号化 + provenance タグ ([ADR-0020](0020-full-local-content-retention.md)) の既存 3 層を継承。
5. **`playwright>=1.50` 依存追加** — `browser` extras を入れない operator には影響ゼロ (optional-dependency)。

## Alternatives Considered

### 1. 生 CDP を独立実装する (`websocket-client` で `--remote-debugging-port` に直接接続)

却下。§Context 論点 1-3 の通り、Chrome 136+ の専用 user-data-dir 制約・Domain ごとの enable・debug port 公開リスク・2 系統並走 state 競合を opshub が自前で抱えることになる。Playwright の `CDPSession` escape hatch (§決定 (b)) で生 CDP 要件は満たせるため、独立実装は冗長かつ高リスク。

### 2. Playwright と生 CDP を両方サポートする (低レベルは生 CDP、高レベルは Playwright)

却下。§Context 論点 3 の通り、同一ブラウザを高レベル API と生 CDP が別前提で触ると navigation / target lifecycle / session ownership が競合する典型アンチパターン。低レベルは必ず Playwright の `CDPSession` 経由に一本化する (§決定 (a)(b))。

### 3. Selenium / Puppeteer-python を採用する

却下。Selenium 4.29+ は CDP 直接サポートを廃止し WebDriver BiDi に移行中、Puppeteer は Node.js 公式 (python port は非公式)。Python 公式 SDK + CDP/BiDi 吸収 + `connect_over_cdp` escape hatch を併せ持つのは Playwright。opshub は Python 単一 package (ADR-0007) のため Python 公式 SDK を優先する。

### 4. `requests` + HTML パーサ (BeautifulSoup / lxml) で取る

却下。JS 描画ページ (SPA) の本文が取れない = ブラウザ読み取り層を新設する動機 (§Context) そのものを満たさない。静的 HTML だけなら既存経路で足りるが、本 Phase の目的は「ブラウザレンダリングを要するページ」の取り込みである。

### 5. 操作系 (click / fill / submit) を Phase 21 に含める

却下 (defer、§決定 (f))。HITL 承認単位の設計・Web ページ由来 prompt injection 対策・ADR-0010 §禁止事項 7 (write-back 禁止) との整理という 3 つの重い前提が未確定であり、read 層と同 Phase に詰めると設計が破綻する。read 層を先に着地させ運用知見を得てから、3 前提を揃えた後続 Phase で解禁する (Phase 9 が scan mode で運用知見を得てから watch mode を後続に回した [ADR-0019](0019-local-filesystem-backed-connector.md) §決定 と同じ段階分割)。

### 6. 認証付きページの session 管理を Phase 21 に含める

却下 (defer)。`connect_over_cdp` escape hatch (§決定 (b)) で「将来 operator が認証済み Chrome に attach する」経路の余地のみ残し、cookie / session の persist / 管理は本 Phase scope 外 (§Non-goals)。専用 user-data-dir (§決定 (c)) に session を貯める設計は将来 ADR で確定する。

## Non-goals (Phase 21)

- **操作系 (click / fill / submit)** — 後続 Phase。本 ADR §決定 (f) で前提条件を pin するのみ。
- **crawler 機能 (リンク追跡 / sitemap 巡回)** — web connector は operator が明示登録した URL のみ取得 ([ADR-0010](0010-connector-contract.md) §Phase 21 改訂 (n))。
- **認証付きページの session 管理** — `connect_over_cdp` escape hatch (§決定 (b)) で将来対応の余地のみ残す。
- **Firefox / WebKit** — Chromium のみ (§決定 (c))。
- **アシスタント skill の追加・変更** — 14 skill 体制 / SKILL.md は不変。`research` skill への `browser.fetch` 組込みは操作系 Phase とまとめて再訪。

## 関連

- [ADR-0010: Connector Contract](0010-connector-contract.md) — §Phase 21 改訂 (n)-(o) で web connector を契約対象に追加 + fingerprint 変更検知契約を web に適用。
- [ADR-0019: Local-filesystem-backed Connector](0019-local-filesystem-backed-connector.md) §決定 (d) — delta API なし connector の `fingerprint` 変更検知 pattern。web connector の fingerprint 契約はこの pattern を URL レンダリング結果に適用する (ADR-0010 §Phase 21 改訂 (o))。
- [ADR-0025: Office Document Content Extraction](0025-office-document-content-extraction.md) §決定 (b)(c) — 抽出後テキスト 500K char cap + 抽出失敗 fail-safe 規律。本 ADR §決定 (d) はこれを rendered DOM → text 経路に継承する。
- [ADR-0022: MCP Server Surface](0022-mcp-server-surface.md) §決定 (c) — read/write 境界。`browser.fetch` は write-category (本 ADR §決定 (e))、正式 surface 追加は 21-D で ADR-0022 改訂。
- [ADR-0005: External Content Minimization](0005-external-content-minimization.md) / [ADR-0020: Full Local Content Retention](0020-full-local-content-retention.md) — 本文ローカル保持の posture。Web ページ本文も §6 の枠内で `sources.body` に persist する。
- [ADR-0021: Encryption at Rest](0021-encryption-at-rest.md) — 取り込んだ Web 本文の保存時暗号化 (prompt injection 緩和 3 層の 1 つ)。
- [ADR-0026: CLI Progress Reporting](0026-cli-progress-reporting.md) — `opshub web sync` の進捗表示 (21-C #507)。
- [ADR-0031: CLI Command Surface Organization](0031-cli-command-surface-organization.md) — `opshub web sync` の noun-first CLI surface (21-C #507)。
- [Epic #504: Browser read layer (Playwright)](https://github.com/ozzy-labs/opshub/issues/504) — 本 ADR は sub-issue 21-A (#505) で起票。21-B (#506) browser core / 21-C (#507) web connector / 21-D (#508) MCP `browser.fetch` / 21-E (#509) docs closeout が本 ADR の決定を実装する。
