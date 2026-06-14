# 0046. Suasor を別 TypeScript 後継プロダクトとして新設 (opshub はレガシー凍結)

- Status: Accepted (opshub レガシー凍結 / Suasor を別 repo・TS で新設 / opshub は Suasor parity 到達後に sunset 検討)
- Date: 2026-06-14
- Deciders: opshub maintainers
- Supersedes: [ADR-0044](0044-rename-opshub-to-suasor.md) (opshub→Suasor **改名** — 本 ADR で「改名」ではなく「別プロダクト新設」に組み替える。Suasor の名前・根拠・命名最終形は継承、機械的リネームは**実行しない**)
- Realizes: [ADR-0045](0045-language-reassessment-zero-base-architecture.md) (ゼロベース最適 = TypeScript の結論を、opshub の移行ではなく **Suasor の新築**として実現する)
- Related: [ADR-0001](0001-python-stack.md) (opshub は Python のまま据え置き・凍結), [ADR-0011](0011-ozzy-labs-ecosystem-adoption.md) (`@ozzylabs` scope — Suasor が npm で合流), [ADR-0004](0004-agent-runtime-boundary.md) / [ADR-0002](0002-event-sourced-architecture.md) / [ADR-0010](0010-connector-contract.md) (Suasor が継承する設計)

## Context

[ADR-0044](0044-rename-opshub-to-suasor.md) は opshub の名前 (`opshub`) が DevOps 誤カテゴリ + OpsHub Inc. との同名衝突を抱えると判断し、**Suasor への改名** (単一プロダクト・機械置換 ~188 ファイル) を Accepted で決め、実装を defer していた。

その後 [ADR-0045](0045-language-reassessment-zero-base-architecture.md) で、配布 → 言語 → アーキテクチャを再検討した結果、**「ML は委譲」を前提とすればゼロベース最適は TypeScript** と結論した (Python の技術的優位は opshub 自身の event-sourced / FTS-first / ML 委譲 設計で軒並み軟化し、残るは incumbency のみ)。

ここで2つの決定 (0044 改名・0045 TS 最適) を**1つの動き**に統合する: opshub を in-place で改名・移行するのではなく、**opshub は据え置き、クリーンな名前 Suasor で TS の後継プロダクトを新築**する。pre-userbase ゆえ互換・移行 path は不要 ([pre-userbase posture](0008-naming-opshub.md))。

## Decision

### (a) opshub (Python) はレガシー凍結

opshub は**改名しない・移行しない・据え置く**。位置づけは **legacy freeze**: 保守 (重大 bug / セキュリティ) のみ、**新機能開発は行わない**。Suasor が機能 parity に到達した時点で sunset を検討する。ADR-0044 が defer した機械的リネーム (~188 ファイル) は**実行しない** (opshub は `opshub` 名のまま終える)。

### (b) Suasor は別 TypeScript プロダクトとして新設

- **別 repo `ozzy-labs/suasor`** (`@ozzylabs` scope に合流、ADR-0044 §(c) で空き確認済)
- 言語・アーキは [ADR-0045](0045-language-reassessment-zero-base-architecture.md) の TS 参照アーキを realize: **Bun / bun:sqlite + sqlite-vec / Drizzle + raw SQL / Zod / MCP TS SDK / FTS-first / ML 全委譲 (torch なし) / 配布 = npm + Bun 単一バイナリ + Docker(+Ollama)**
- 名前・読み (スアソル)・命名最終形・根拠は ADR-0044 から**継承** (CLI `suasor` / `@ozzylabs/suasor` / MCP server `suasor` / env `SUASOR_*` / config `~/.config/suasor/` / tagline「読み、覚え、助言する秘書エージェント」)
- **移行ではなく新築** (clean reimplementation)。opshub の技術負債・Python 由来の制約を持ち込まない

### (c) 2プロダクトは完全 disjoint で共存

名前・配布・config dir・keyring が重ならないため、移行 shim や互換層なしで並走できる:

| | opshub (legacy) | Suasor (successor) |
|---|---|---|
| 言語 | Python | TypeScript (Bun) |
| repo | `ozzy-labs/opshub` | `ozzy-labs/suasor` |
| 配布 | PyPI `ozzylabs-opshub` | npm `@ozzylabs/suasor` + 単一バイナリ |
| CLI / MCP name | `opshub` | `suasor` |
| config / env | `~/.config/opshub` / `OPSHUB_*` | `~/.config/suasor` / `SUASOR_*` |

operator は両方を同一マシンに併存させられる (別 DB・別 config)。データ移行は提供しない (pre-userbase、各々独立 init)。

### (d) Suasor が opshub から継承する設計 (言語非依存の実証済み資産)

新築だが**設計思想は引き継ぐ**: event-sourced 核 ([ADR-0002](0002-event-sourced-architecture.md)) / local-first・全本文ローカル保持・外部送信最小化 / connector 契約 ([ADR-0010](0010-connector-contract.md)) / MCP = エージェント境界 ([ADR-0004](0004-agent-runtime-boundary.md) / [ADR-0022](0022-mcp-server-surface.md)) / HITL write 境界 / FTS-first retrieval / **ML 委譲 posture** ([ADR-0045](0045-language-reassessment-zero-base-architecture.md) §(e)(f)) / アシスタント skill 群 (SSOT を Suasor repo に移す)。

### (e) ADR の住み分け

- **opshub の ADR (0001-0046) は opshub repo に残す** (履歴・immutability)。リネームしない
- **Suasor は新 repo で自前の ADR 列を開始** (Suasor ADR-0001 = TS stack、本 0045 の参照アーキを Accepted で起票)。opshub の関連 ADR は Suasor 側から参照リンクする
- 本 ADR-0046 は **opshub 側に「fork した事実」を記録**する終端的な ADR (opshub の今後の開発は原則ここで止まる)

## Consequences

### Positive

1. **~188 ファイルの機械的リネームが不要** — opshub は名前を保持し終える。ADR-0044 の最大コストが消える
2. **技術負債を新築で断つ** — 移行 (in-place 書き換え) のリスク・中間状態を回避し、ADR-0045 の理想アーキを最初から実装
3. **稼働資産を壊さない** — opshub は動き続ける (自分用に使える)。Suasor の parity を待って段階移行できる
4. **名前問題の解決** — go-forward の公開プロダクト Suasor はクリーン名、opshub の「悪い名前」は legacy 側で許容 (sunset で消える)
5. **`@ozzylabs` 合流** — Suasor が npm scope に入り、ecosystem 整合 ([ADR-0011](0011-ozzy-labs-ecosystem-adoption.md)) を回復

### Negative / Trade-offs

1. **一時的に 2 コードベースが存在** — opshub (凍結) と Suasor (開発中)。凍結方針 ((a)) で二重保守を最小化するが、parity 到達まで併存
2. **再実装の初期コスト** — connector ロジック・skill・projection を TS で書き直す。設計は流用できるが実装は新規
3. **opshub の bug fix は Suasor に自動反映されない** — 別言語・別 repo。重大 fix は両側に手当てが要る (凍結で新機能を足さない方針が衝突面を減らす)
4. **parity 未達の間は機能差** — Suasor が opshub の 11 connector / 19 MCP tool / 15 skill に追いつくまで、opshub が機能上の本体であり続ける

## Alternatives Considered

- **opshub を in-place で TS に書き換え (migration)** — 却下。24 phase 分の in-place 書き換えはリスク大・中間状態が不安定。新築の方が clean で、opshub を壊さず並走できる。
- **ADR-0044 原案どおり opshub を Suasor に改名 (Python のまま)** — 却下。名前は直るが [ADR-0045](0045-language-reassessment-zero-base-architecture.md) の言語結論 (TS) に応えない。Python のまま名前だけ変えるのは中途半端。
- **opshub と Suasor を並行 sibling として両方アクティブ開発** — 却下。solo+agent で実装が二重になり維持コストが倍。legacy freeze + 後継一本化の方が現実的。
- **opshub を個人用サンドボックスとして残す (Suasor が公開)** — 近いが不採用。「凍結・後継一本化」の方が意図 (機能は Suasor に集約) を明確にする。

## Validation / Follow-up

- **Suasor repo bootstrap** を別 Phase / 別 repo で行う: `ozzy-labs/suasor` 作成 + Suasor ADR-0001 (TS stack) 起票 + 最小 event store + MCP serve + 1 connector で walking skeleton。
- opshub 側は本 ADR 以降 **新機能 Phase を切らない** (保守のみ)。README に legacy 化 + 後継 Suasor への誘導を追記 (別 PR)。
- ドメイン (`suasor.dev` 等) 登録・商標ざっと確認は Suasor bootstrap Phase で (ADR-0044 §Validation 継承)。
