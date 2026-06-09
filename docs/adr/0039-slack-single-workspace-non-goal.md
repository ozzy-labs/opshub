# 0039. Slack Single-Workspace Non-Goal (+ team_id bind guard)

- Status: Accepted + Landed (Phase 23-H, epic [#530](https://github.com/ozzy-labs/opshub/issues/530))
- Date: 2026-06-09
- Deciders: opshub maintainers
- Related: [ADR-0018](0018-slack-token-principal.md) (User/Bot token principal — orthogonal axis; this ADR constrains *how many* workspaces, not *which principal*), [ADR-0030](0030-slack-thread-reply-ingestion.md) / [ADR-0038](0038-slack-sync-gap-backfill.md) (compound cursor schema — the `team_id` bind axis is added here), [ADR-0010](0010-connector-contract.md) (connector / cursor contract)

## Context

`src/opshub/connectors/slack/auth.py` stores the Slack OAuth token under a
single fixed keyring slot `connector:slack:token`, and the connector is a
single registered instance (`name = "slack"`). The whole Slack pipeline —
token slot, connector id, `external_id = "{channel_id}:{ts}"` (whose comment
asserts workspace-wide uniqueness), `[connectors.slack]` config, and the
compound resume cursor — is **baked-in to a single workspace**. An operator
who belongs to several Slack workspaces (side-projects, OSS, employer) is
unsupported, and the original 9-perspective UX review missed this entirely.

The acute risk is **silent**: swapping the stored token to a second
workspace makes the next `opshub slack sync` mix two workspaces' messages
into one cursor + `external_id` namespace, exit 0, and look like success —
the exact "silent failure" pathology epic #530 set out to kill. Channel ids
can even collide across workspaces, corrupting `sources`.

## Decision

opshub treats **single-workspace as an explicit non-goal**: **one opshub
install = one Slack workspace**. We do *not* extend the token slot / config /
cursor / `external_id` to a workspace axis. To make the invariant safe rather
than merely assumed, we add a **`team_id` bind guard**:

1. The compound cursor gains a scalar `team_id` axis
   (`SlackCursorState.team_id: str | None`; `None` = unbound). It is additive
   and forward-compatible — a pre-23-H cursor lacks it and parses to `None`.
2. The sync hot path resolves the live workspace via `auth.test` and, **before
   any fetch**, reconciles it with the bound `team_id`:
   - **unbound** (first sync / after `cursor reset --all`) → bind it.
   - **bound, same team** → proceed.
   - **bound, different team** → `ConfigError` (loud), steering the operator
     to restore the original token or, to switch intentionally,
     `opshub slack cursor reset --all`.
   Because the check runs **before any fetch**, a foreign workspace's data
   never enters the DB — so the `external_id` workspace-wide-uniqueness
   premise (`mapper.py`) is never broken. This is the load-bearing reason we
   can leave `external_id` un-prefixed (a `team_id` prefix would be YAGNI).
3. `external_id` / config / projection get **no** workspace axis (single is the
   end-state). If a future need for multi-workspace appears, adopt it under a
   new top-level Phase + ADR with a `team_id` re-key (pre-userbase → a hard
   flip, no compat shim).
4. Existing and future Slack features (e.g. future channel-discovery surfaces)
   **may depend on the single-workspace invariant explicitly** — it is pinned
   here, not assumed implicitly.
5. **Enterprise Grid**: a Grid User Token may span several workspaces in an
   org, but `auth.test` reports the token's stable *home* `team_id`, so the
   guard does not false-positive. Ingesting channels from other workspaces in
   the same Grid is out of scope for this ADR.

## Consequences

- Each non-empty sync makes **one extra `auth.test` call** to resolve the
  `team_id` (the same call `opshub slack auth test` and the demand-digest
  projection already make; an invalid token already fails sync, so no new
  failure surface).
- Intentionally switching workspaces requires `opshub slack cursor reset --all`
  to rebind; the previous workspace's already-ingested `sources` remain and
  must be purged manually (an unsupported path).
- `opshub slack status` shows the **bound** workspace (`team_id`), complementing
  `opshub slack auth test` which shows the **live** workspace the token resolves
  to now. A divergence between the two is exactly what the next sync's guard
  rejects, making the failure diagnosable.
- A per-channel `opshub slack cursor reset --channel` does **not** unbind
  (only `--all` cold-starts the workspace bind).

## Alternatives Considered

- **(b) `team_id` token slot** (`connector:slack:<team_id>:token` + per-workspace
  config / cursor / projection axis) — **rejected**: no demonstrated need
  (operator confirmed single-workspace suffices for now), and it adds
  accidental complexity across four baked-in sites, directly against epic
  #530's complexity-reduction goal.
- **Declaration only, no guard** — rejected: leaves the silent-corruption path
  open, which is the very pathology #530 targets.
- **Operator-declared `team_id` in config** — rejected: operator burden; the
  cursor bind achieves the same guard with zero configuration.

## Validation

Pinned by `tests/unit/connectors/slack/test_connector.py` (bind on first sync /
pass on match / `ConfigError` on mismatch / rebind after reset / legacy cursor
binds / empty `team_id` does not bind), `test_slack_cursor.py` (per-channel
reset preserves `team_id`; `reset --all` clears it), `test_slack_status.py`
(bound-workspace display), and `tests/integration/test_phase7_slack_sync.py`
(end-to-end `team_id` persistence + same-workspace re-sync).
