# 0007. Single Python Package, defer Monorepo

- Status: Accepted
- Date: 2026-05-16
- Deciders: ozzy

## Context

OpsHub は最終的に複数の Connector (GitHub / Slack / Microsoft 365 / Box / 将来追加分) を持つ可能性が高い。長期的には次のような monorepo 構成が想定される。

```text
packages/
├── opshub-core/
├── opshub-cli/
├── opshub-connector-github/
├── opshub-connector-slack/
├── opshub-connector-msgraph/
├── opshub-connector-box/
└── opshub-vectors/
```

しかし MVP 段階では Phase 1 (foundation only) のため connector を実装しない。Phase 3 まで monorepo の利益はほぼない。

monorepo を最初から採用する場合のコスト:

1. uv workspace 設定 / lockfile 管理 / 内部依存解決の学習コスト
2. CI が複数 package を並列ビルド / publish する設計コスト
3. テスト / lint の package 跨ぎ実行 (`uv run --package` 等) のセットアップ
4. import path の安定化 (`opshub.core.X` vs `from opshub_core import X` の選択)
5. リファクタコストが高い (まだドメインモデルが固まっていない段階で package 境界を決めるのは早すぎる)

## Decision

**Single Python package** で開始する。

```text
opshub/
└── src/
    └── opshub/
        ├── cli/
        ├── core/
        ├── db/
        ├── domain/
        ├── services/
        ├── projections/
        ├── connectors/      # ここに全 connector を同居
        │   ├── github/
        │   ├── slack/
        │   ├── msgraph/
        │   └── box/
        ├── markdown/
        ├── graph/
        ├── vectors/
        ├── runtime/
        └── agents/
```

すべてのモジュールが 1 つの `opshub` パッケージに属する。PyPI 配布も `opshub` 1 パッケージのみ。

### Monorepo 化の発火条件

以下のいずれかが揃ったときに `uv workspace` 移行を検討する。

1. **Connector 数が 5 を超える** — 互いに独立したリリースサイクルが必要になる
2. **Connector 単体の依存が膨らむ** — `connector-slack` 専用の重い依存 (例: `slack-sdk[all]`) が core に紛れ込む
3. **第三者が connector を提供する需要** — `pip install opshub-connector-<name>` で plugin 公開が必要
4. **テスト時間が許容範囲を超える** — connector 単位の部分テスト実行が必須になる

発火したら `ADR-NNNN: Migrate to Monorepo` を起票し、移行計画を立てる。

## Consequences

### Positive

1. **MVP 速度** — 1 package で最短経路。Phase 1 の 15 commit に集中できる
2. **リファクタ自由度** — モジュール境界を後から動かせる
3. **import path の単純化** — `from opshub.services.task_service import ...` で完結
4. **CI 単純化** — 1 ジョブで lint / type / test / build を回せる
5. **PyPI 配布が 1 パッケージ** — ユーザーは `pip install opshub` だけで全機能利用可能

### Negative / Trade-offs

1. **Connector ごとの独立 release が不可** — Slack 連携のバグ修正で OpsHub 全体を release する必要
2. **依存が膨らみがち** — connector 専用ライブラリが core にも intall される
3. **将来の monorepo 移行コスト** — Phase 3 終盤か Phase 4 で発生する見込み
4. **第三者 plugin 提供が困難** — pip extras + entry points で代替可能だが workspace ほど clean ではない

## 軽減策

1. **モジュール責務の鉄則を厳格に守る** ([repository-structure.md 3](../repository-structure.md))
2. **connector 専用依存は `optional-dependencies` で分離**

   ```toml
   [project.optional-dependencies]
   github = ["PyGithub>=2"]
   slack = ["slack-sdk>=3"]
   connectors-ms365 = ["msgraph-sdk>=1"]
   box = ["boxsdk>=3"]
   ```

   ユーザーは `pip install "opshub[github,slack]"` で必要分のみ install。
   Phase 10 監査 follow-up で `msgraph` extras と `all` bundle は廃止し、現状の
   `pyproject.toml` の extras 名 (`connectors-ms365`) に揃えた。`all` bundle
   は本リポでは提供せず、operator は必要な connector extras を明示的に列挙する。
3. **import の循環チェック** — `import-linter` または ruff の `TID` rule で connector → core の単方向依存を強制
4. **テスト境界** — connector テストはディレクトリ単位で実行可能にする (`pytest tests/connectors/github/`)

## Alternatives Considered

### 1. uv workspace で最初から monorepo

却下理由:

- MVP のスコープに対して overhead が大きい
- 境界が固まっていない段階で package 分割すると、後で頻繁な再構成が発生
- Phase 3 まで connector がないため workspace の利益が出ない

### 2. Connector のみ別 package、core / cli は single package

却下理由:

- 中途半端な分離で、結局 monorepo 化と同等の overhead が発生
- core / connector の境界設計を 2 回 (今 + 将来 monorepo 化時) やることになる
- `optional-dependencies` 方式で同等の選択的 install を実現できる

### 3. Connector を entry points + plugin 形式で第三者公開可能化

却下理由:

- MVP では第三者 plugin の需要なし
- 将来 monorepo 化と組み合わせて再検討の余地あり
- 今は採用しない

## 関連

- [Principles 9 (Phased Delivery)](../principles.md)
- [Repository Structure 4 (パッケージング方針)](../repository-structure.md)
- [ADR-0001: Python Stack](0001-python-stack.md)
