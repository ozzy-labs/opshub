# Slack Setup (Phase 7 connector)

The `slack` connector pulls message bodies (channels / DMs / group DMs,
including thread replies) from the Slack Web API into OpsHub's `sources`
projection. Per [ADR-0018](adr/0018-slack-token-principal.md) the **User
Token** (`xoxp-...`) is the first-class principal — it matches the
"connector acts on behalf of the installing human" model used by GitHub
PAT / MS365 OAuth / Box OAuth. **Bot Token** (`xoxb-...`) is accepted as
an alternative for workspace / audit policy constraints (see the last
section); with a Bot Token each ingested channel must have the bot
`/invite`'d.

Slack has the highest first-run friction of any connector (app creation
→ scope grant → token issue), so this page is the single end-to-end
checklist. The end goal: a configured
`[connectors.slack.workspaces.<alias>]` table in `opshub.toml` that
`opshub slack sync` walks, after which the assistant skills
(`personal-brief` / `find-document` / etc.) can surface Slack context.
**A skill can only see Slack content that an `opshub slack sync` has
already ingested** — sync is the prerequisite for every read path.

> **Multi-workspace (Phase 24, [ADR-0041](adr/0041-slack-multi-workspace.md))**:
> one opshub install can ingest **N Slack workspaces**. Each workspace is
> named by an operator-chosen **alias** (the operation-layer key) and bound
> to its Slack `team_id` (the immutable data-layer key). Every command and
> config table is keyed on the alias; the steps below configure a single
> alias `acme` — repeat them per workspace (see §9). ADR-0041 supersedes the
> earlier single-workspace non-goal (ADR-0039).

## Checklist at a glance

```text
1. Create a Slack app                              → https://api.slack.com/apps
2. Add the MVP 3 User Token scopes                 → channels:history, channels:read, users:read
3. Install to workspace + copy the xoxp- token
4. opshub slack auth set --workspace acme          # store the token (keyring, per-alias slot)
5. opshub slack auth test --workspace acme         # verify token + granted scopes
6. opshub slack conversations --workspace acme --format=toml  # discover channel ids
7. paste the printed block into opshub.toml        # [connectors.slack.workspaces.acme] table
8. opshub slack sync                               # ingest — skills can now see Slack
```

> `--workspace` is optional when exactly one workspace is configured;
> with zero or multiple it is required (channel ids collide across
> workspaces, so opshub never guesses — see §9).

## 1. Create a Slack app

1. Go to <https://api.slack.com/apps> → **Create New App** → **From
   scratch**. Name it (e.g. `OpsHub`) and pick your workspace.
2. In the left menu open **OAuth & Permissions**.

## 2. Add the MVP scopes (User Token Scopes)

Under **OAuth & Permissions → Scopes → User Token Scopes**, add the
three MVP scopes that cover public-channel history ingestion + the
`opshub slack conversations` listing:

| Scope | Why |
| --- | --- |
| `channels:history` | read message bodies in public channels (ingestion) |
| `channels:read` | list public channels (`opshub slack conversations`) |
| `users:read` | resolve author / DM peer display names |

Optional scopes — add the ones matching the conversation types you want
to ingest (each `*:history` needs the matching `*:read` to also appear
in `opshub slack conversations`):

| Scope | Unlocks |
| --- | --- |
| `groups:history` + `groups:read` | private channels |
| `im:history` + `im:read` | direct messages |
| `mpim:history` + `mpim:read` | group DMs |
| `search:read` | engagement-axis sort (`--sort=last_self_post`), User Token only |
| `files:read` | file metadata |

> **Bot Token path**: if workspace policy denies User Token scopes, add
> the same scopes under **Bot Token Scopes** instead and read the
> *Bot Token (alternative principal)* section below.

## 3. Install + copy the token

1. **OAuth & Permissions → Install to Workspace** → approve the consent
   screen. Re-installing is required any time you change scopes.
2. Copy the **User OAuth Token** (starts with `xoxp-`). The Bot token
   (`xoxb-`) appears here too if you added Bot Token Scopes.

> **token shape**: OpsHub validates the prefix up front — a token that
> is not `xoxp-` / `xoxb-` (e.g. an app-level `xapp-` or a client
> secret) fails fast with an actionable `ConfigError` rather than a
> confusing `invalid_auth` at sync time. See
> <https://api.slack.com/authentication/token-types>.

## 4. Store the token in opshub

### (a) keyring (recommended, long-term)

```bash
opshub slack auth set --workspace acme
# paste the xoxp- token at the hidden prompt
```

The token is stored in the OS keychain under the **per-alias** slot
`connector:slack:acme:token` ([ADR-0014](adr/0014-saas-token-storage.md) /
ADR-0018 / [ADR-0041](adr/0041-slack-multi-workspace.md) §(a)). User Token
and the Bot Token alternative share this one slot. Each workspace alias
gets its own slot, so `acme` and `oss` never share a token.

> `--workspace` is optional with exactly one configured workspace; with
> zero or multiple it is required. `auth set` legitimately runs *before*
> the `[connectors.slack.workspaces.<alias>]` table is written, so it only
> validates the alias **grammar** (`^[a-z0-9][a-z0-9_]*$` — `-` is rejected,
> ADR-0041 §(a)), not membership in the config.

### (b) env var (CI / containers / WSL2)

```bash
export OPSHUB_CONNECTOR_SLACK_ACME_TOKEN="xoxp-..."
```

The env var wins over the keyring, so CI / Docker / WSL2 (where the OS
keychain may be unreachable) can inject the token without keyring setup.
The alias is upper-cased into the env name
(`OPSHUB_CONNECTOR_SLACK_<ALIAS>_TOKEN`), which is exactly why `-` is
disallowed in aliases — `my-ws` and `my_ws` would collapse to the same
env name (ADR-0041 §(a)).

> **env-var naming note (#535)**: the *token* env var is
> `OPSHUB_CONNECTOR_SLACK_<ALIAS>_TOKEN` (singular `CONNECTOR`, single
> underscore) because it is resolved by the keyring secret layer, which
> uses a flat naming scheme shared by every connector
> (`OPSHUB_CONNECTOR_<NAME>_TOKEN`, ADR-0014; the Slack alias slots in as
> the per-workspace discriminator). Every *non-secret* Slack setting uses
> the nested config form `OPSHUB_CONNECTORS__SLACK__*` (plural
> `CONNECTORS`, double underscore) because those flow through the
> pydantic-settings nested-config loader (ADR-0032). The asymmetry is
> intentional and bounded to the secret slot; #535 left the token env on
> the secret-layer scheme rather than introducing a Slack-only third form
> (see issue #535 §"token env 統一は要 cross-connector 判断").

## 5. Verify the token

```bash
opshub slack auth test --workspace acme
```

Prints `status: ok` with the team / user / principal and the **granted
scopes** (Phase 23-C, #533) so you can confirm the scopes you intended
were actually approved. A missing scope here is the usual cause of an
empty `opshub slack conversations` result. The `features:` readiness
block is evaluated against **this workspace's** token (ADR-0041 §(h)) —
each alias can hold a different principal / scope set.

### Feature readiness (`features:` block)

Below the field list, `auth test` prints a **`features:`** block (Phase
23-I, #539, [ADR-0040](adr/0040-slack-feature-scope-ssot-and-readiness.md))
that maps each ingestion feature to a scope-readiness verdict, so you see
what the token *can do* — and what scope to add — in one call:

```text
features:
  public channel sync: READY
  private channel sync: MISSING groups:history
  DM sync: MISSING im:history
  group-DM (mpim) sync: MISSING mpim:history
  engagement axis (--sort=last_self_post): READY
  note: READY = scope granted; it does not guarantee channel membership …
```

The feature → scope correspondence (the single source of truth lives in
`src/opshub/connectors/slack/scopes.py`):

| feature | required scope | recommended | note |
|---|---|---|---|
| public channel sync | `channels:history` | `users:read` (author names) | any token |
| private channel sync | `groups:history` | `users:read` | any token |
| DM sync | `im:history` | `users:read` | any token |
| group-DM (mpim) sync | `mpim:history` | `users:read` | any token |
| engagement axis (`--sort=last_self_post`) | `search:read` | — | **User Token only** |

Reading the verdicts:

- **READY** — the required scope is granted. `READY (degraded: +users:read …)`
  means it works but author display names won't resolve until you add the
  recommended scope.
- **MISSING `<scope>`** — add `<scope>` (re-install the app after changing
  scopes) to enable the feature.
- **N/A (User Token only)** — a Bot Token can never hold `search:read`
  ([ADR-0034](adr/0034-slack-engagement-axis.md)); switch to a User Token to
  use the engagement axis. It is `N/A`, not `MISSING`, because no scope you
  can add to the Bot Token fixes it.
- **READY is a scope verdict, not a membership guarantee.** A channel you have
  not joined (or, on a Bot Token, not `/invite`'d) still returns
  `not_in_channel` even when every scope is `READY` — see Troubleshooting
  §3.15. Thread-reply ingestion and the mention/DM demand digest need no
  scope beyond the history scopes above (ADR-0033).

## 6. Discover channel ids

```bash
opshub slack conversations --workspace acme --format=toml
```

This lists the conversations your token can see and prints a
**complete, paste-ready block** — the `[connectors.slack]` header with
`enabled = true`, **plus the `[connectors.slack.workspaces.acme]` table**
carrying a commented `channels = [...]` array (Phase 23-E, #535;
Phase 24-C, ADR-0041 §(f)). The `channels` array now lives under the
per-alias workspace table — the flat `[connectors.slack] channels` key
is rejected by the settings layer — so the printed block is keyed on the
alias the listing ran against.

Useful flags:

- `--types public,private,im,mpim` — pick which conversation kinds to
  enumerate (defaults to all four; each kind needs its listing scope).
- `--filter <substring>` — case-insensitive name / participant filter.
- `--sort name|last_self_post|last_activity` — ordering axis
  ([ADR-0035](adr/0035-slack-sort-axis-consolidation.md)). `name`
  (default) is alphabetical; the activity axes annotate each id with its
  last-post date.
- `--format table` — human-readable columns for eyeballing instead of
  pasting.

## 7. Paste into opshub.toml

Copy the printed block into `opshub.toml` (the starter written by
`opshub init` already carries a disabled `[connectors.slack]` section
you can overwrite). The pasted block enables the connector and lists the
channels under the workspace table:

```toml
[connectors.slack]
enabled = true

[connectors.slack.workspaces.acme]
channels = [
  "C0123ABC",  # general (public)
  "C0456DEF",  # leadership (private)
]
```

The `[connectors.slack.workspaces.<alias>]` table is **required** even
for a single workspace — there is no implicit default-workspace code
path (ADR-0041 §(c)). The flat `[connectors.slack] channels = [...]` form
from before Phase 24 is rejected with a `ConfigError` that names the
rewrite.

### channel ids via env var

`channels` also accepts a **comma-separated** env override
(Phase 23-E, #535) — no JSON quoting required for the common case. The
alias slots into the nested-config path:

```bash
export OPSHUB_CONNECTORS__SLACK__WORKSPACES__ACME__CHANNELS="C0123ABC,C0456DEF"
```

The JSON-document form is still accepted when you need per-channel
`since` floors:

```bash
export OPSHUB_CONNECTORS__SLACK__WORKSPACES__ACME__CHANNELS='[{"id":"C0123ABC","since":"30d"}]'
```

### optional date floor

`sync_since` caps the cold-start backfill so messages older than the
floor are never fetched (Phase 20,
[ADR-0036](adr/0036-slack-sync-date-floor.md)). It resolves in **three
tiers** (most specific wins): per-channel `since` →
per-workspace `sync_since` → connector-wide `sync_since`
(ADR-0041 §(c)):

```toml
[connectors.slack]
enabled = true
sync_since = "90d"               # connector-wide default (relative "90d"/"4w" or ISO; omit for full history)

[connectors.slack.workspaces.acme]
channels = ["C0123ABC"]
sync_since = "30d"               # per-workspace override of the connector-wide default
```

`thread_activity_window` (late-reply polling window, default `30d`) is
also overridable per workspace, symmetric with the floor:

```toml
[connectors.slack.workspaces.acme]
channels = ["C0123ABC"]
thread_activity_window = "7d"    # per-workspace override of the connector-wide default
```

Per-channel overrides use the channel table form inside the workspace
table (`since = "all"` opts one channel back into full history):

```toml
[[connectors.slack.workspaces.acme.channels]]
id = "C0123ABC"
since = "all"
```

## 8. Sync

```bash
opshub slack sync
```

Pulls new messages (and thread replies, including late replies) since
the stored cursor. The first run backfills history down to the
`sync_since` floor (or the full history if no floor is set).

With multiple workspaces, `opshub slack sync` (no flag) syncs **every**
configured workspace serially with **per-workspace error isolation**: one
workspace's failure (token revoked, rate-limit budget exhausted, …) does
not roll back another's progress, and each succeeded workspace's cursor is
checkpointed before the next runs (ADR-0041 §(b)). If any workspace
failed, the command exits non-zero and stderr names the failed alias(es).
`opshub slack sync --workspace acme` narrows to one workspace.

> **no-op guard (#535)**: if the connector is `enabled = false`, there is
> no `[connectors.slack.workspaces.<alias>]` table, or every workspace's
> `channels` list is empty, `opshub slack sync` prints a visible
> `notice:` line on stderr naming the reason and the fix (it would
> otherwise exit 0 with "0 items observed" and look like success).
> Suppress it with `-q` / `--quiet`.

After a successful sync the assistant skills can surface Slack content:

```bash
opshub search "ticket-1234"      # FTS5 across ingested bodies
# or ask your agent: "今日のまとめ" / "find that Slack thread about X"
```

### Periodic sync

OpsHub has no resident daemon; schedule `opshub slack sync` with the OS
scheduler:

```cron
# crontab -e
*/15 * * * * opshub slack sync
```

## Status / recovery

Slack sync state is a 3-axis compound resume cursor (`channels` forward
high-water / `backfill` low-water / `threads` late-reply marks). It is a
*resume* cursor, not a coverage ledger — a single low-water per channel
cannot represent gap-backfill holes, and Slack has no thread-late-reply delta
API, so a quiet window is indistinguishable from an unfetched one.

**Daily view — `opshub slack status`** (Phase 23-F, #536):

```bash
opshub slack status                  # per-workspace block; per channel: forward
                                     # high-water, backfill low-water, thread count
                                     # + "next sync will re-fetch history" when the
                                     # floor was lowered. Also names each alias's
                                     # bound workspace (team_id).
opshub slack status --workspace acme # filter to one workspace
opshub slack status --verbose        # raw per-alias cursor + raw ts (the old `cursor show`)
```

`status` shows the high-water and low-water as **separate facts** and never
asserts a continuous covered range (the cursor cannot prove one). With
multiple workspaces it prints one block per alias (Phase 24-D, ADR-0041 §(f)).

**Recovery / surgery — `opshub slack cursor`** (Phase 22-E,
[ADR-0038](adr/0038-slack-sync-gap-backfill.md) §(f)):

- `opshub slack cursor reset [--channel C1,C2 [--workspace acme] | --all [--workspace acme]]`
  — drop cursor entries so the selected channels cold-start on the next
  sync. `--all` alone resets **every** workspace's channels and **unbinds**
  every alias's `team_id`; `--all --workspace acme` narrows the drop (incl.
  unbind) to one alias. `--channel` follows the §(f) default rule (one
  workspace → implicit, multiple → `--workspace` required, since the same
  channel id can exist in two workspaces).
- `opshub slack cursor backfill --channel <id> --since <new> [--until <old>] [--workspace acme]`
  — explicit bounded backfill of a past window. `--until` is optional once
  the channel has ingested messages: it defaults to the oldest already-ingested
  message ts (#536). Pass it explicitly only for a never-synced channel.

> **`opshub projections rebuild` does NOT reset the Slack cursor** — it
> replays the event log and restores the same cursor value, so it cannot
> be used to re-fetch history. Use `opshub slack cursor reset` /
> `backfill` (ADR-0038 §Context corrects the earlier guidance).

## Constraints

- **Write-back is not supported**
  ([ADR-0010](adr/0010-connector-contract.md) §禁止事項). Replies are
  draft-only via `opshub propose generate --reply-to <source-id>`;
  posting to Slack is manual copy-paste.
- **Channel *names* (`#general`) are not accepted** — membership /
  access is keyed on the id and Slack does not guarantee name stability.
- **Cold threads past `thread_activity_window`** (default 30d) stop
  being polled for late replies (Phase 20-C, intended limitation).
- **Per-alias token binding** ([ADR-0041](adr/0041-slack-multi-workspace.md)
  §(a), Phase 24). Each workspace's first sync binds its Slack `team_id`
  into that alias's cursor entry; swapping the stored token under an alias
  to a *different* workspace makes the next `opshub slack sync` fail loud
  with a `ConfigError` (rather than silently mixing two workspaces).
  Registering two aliases that resolve to the **same** `team_id` is also a
  `ConfigError` (a workspace registered twice would corrupt cursor /
  digest semantics). `opshub slack status` shows each alias's bound
  workspace.

## Troubleshooting

### `opshub slack conversations` returns nothing

Usually a missing listing scope. Re-run `opshub slack auth test` and
check the `scopes` line: add `groups:read` / `im:read` / `mpim:read` for
the conversation types you expect, **re-install the app**, and store the
new token.

### `sync` reports `0 item(s) observed`

Check the `notice:` line (Step 8). Either `enabled = false`, `channels`
is empty, or every channel is already at its cursor (no new messages).

### `ConfigError: Slack token must start with 'xoxp-' ...`

The stored value is not a Slack OAuth token (a common mistake is pasting
the app-level `xapp-` token or a client secret). Copy the **User OAuth
Token** from **OAuth & Permissions** and re-run `opshub slack auth set`.

### `invalid_auth` at sync time

The token expired or was revoked (e.g. the app was re-installed). Issue
a fresh token and overwrite via `opshub slack auth set`.

## Bot Token (alternative principal)

When workspace policy denies User Token scopes:

1. Add the scopes under **Bot Token Scopes** (not User Token Scopes).
2. Install / re-install the app and copy the **Bot User OAuth Token**
   (`xoxb-...`).
3. Store it the same way (`opshub slack auth set --workspace acme` or
   `OPSHUB_CONNECTOR_SLACK_ACME_TOKEN`) — the auth helper accepts either
   prefix.
4. `/invite` the bot into every channel you want ingested (a Bot Token
   only sees channels the bot has joined).

Note the engagement-axis sort (`--sort=last_self_post`, which uses
`search.messages`) is **User Token only** — a Bot Token raises a
`ConfigError` directing you to `--sort=last_activity`.

## 9. Add a second workspace

[ADR-0041](adr/0041-slack-multi-workspace.md) (Phase 24) makes
`1 install = N Slack workspaces` first-class. Each workspace is named by
an operator-chosen **alias** and bound to its immutable Slack `team_id`.
To add a second workspace `oss`, repeat steps 1–7 against the other
Slack workspace with `--workspace oss`:

```bash
opshub slack auth set --workspace oss          # per-alias keyring slot: connector:slack:oss:token
opshub slack auth test --workspace oss         # verify under the oss token
opshub slack conversations --workspace oss --format=toml
```

Then add the second workspace table to `opshub.toml` alongside the first:

```toml
[connectors.slack]
enabled = true
sync_since = "90d"               # connector-wide default (per-workspace overridable)

[connectors.slack.workspaces.acme]
channels = ["C0123ABC"]
sync_since = "30d"               # override for acme only

[connectors.slack.workspaces.oss]
channels = ["C0789GHI"]
```

`opshub slack sync` (no flag) then walks **both** workspaces serially
with per-workspace error isolation (§8).

### Alias rules

- Aliases match `^[a-z0-9][a-z0-9_]*$` — lowercase, digits, and `_`
  only. `-` is rejected (it would collapse with `_` in the keyring env
  override name `OPSHUB_CONNECTOR_SLACK_<ALIAS>_TOKEN`, ADR-0041 §(a)).
- The alias is the **operation-layer** key (config tables, `--workspace`,
  keyring slots). The **data-layer** key is the workspace's Slack
  `team_id` (`external_id = f"{team_id}:{channel_id}:{ts}"`), which is
  immutable and rename-proof.
- **Renaming an alias** loses that workspace's cursor (the cursor nests
  under the alias key), so the next sync re-fetches the channels from the
  floor. Because `external_id` is keyed on `team_id` (not the alias), the
  re-fetch is an **idempotent upsert** — no duplicate sources, only the
  API fetch cost. To rename cheaply: change the table header, then either
  let the next sync re-fetch or accept the cold-start.
- Two aliases that resolve to the **same** `team_id` are a `ConfigError`
  (the same workspace registered twice would corrupt cursor / digest
  semantics).

### Per-workspace self-id (mention / DM digest)

The mention/DM demand digest resolves the operator's `U...` id
**per workspace** (a `U...` id is workspace-specific). Each alias's id is
normally resolved from its own token via `auth.test`. The env override is
per-alias and **team-qualified** — the value is `T...:U...` (the digest
keys rows on `team_id`, so the override must carry both halves, ADR-0041
§(g)):

```bash
export OPSHUB_SLACK_SELF_USER_ID__ACME="T0ACME:U0ACMESELF"
export OPSHUB_SLACK_SELF_USER_ID__OSS="T0OSS:U0OSSSELF"
```

This is what makes mention detection and own-post suppression work
independently across workspaces (a single install-wide id would miss
mentions in every other workspace — ADR-0041 §(g)). The pre-Phase-24
install-wide `OPSHUB_SLACK_SELF_USER_ID` variable is no longer read.

## Related docs

- [ADR-0041: Slack Multi-Workspace](adr/0041-slack-multi-workspace.md)
- [ADR-0018: Slack Token Principal](adr/0018-slack-token-principal.md)
- [ADR-0014: SaaS Token Storage](adr/0014-saas-token-storage.md)
- [ADR-0036: Slack Sync Date Floor](adr/0036-slack-sync-date-floor.md)
- [ADR-0038: Slack Sync Gap Backfill](adr/0038-slack-sync-gap-backfill.md)
- [docs/mcp-setup.md](mcp-setup.md) — wiring the assistant skills over MCP
- [SECURITY.md](../SECURITY.md) — local body retention posture
