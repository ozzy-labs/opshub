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
checklist. The end goal: a configured `[connectors.slack]` section in
`opshub.toml` that `opshub slack sync` walks, after which the assistant
skills (`personal-brief` / `find-document` / etc.) can surface Slack
context. **A skill can only see Slack content that an `opshub slack sync`
has already ingested** — sync is the prerequisite for every read path.

## Checklist at a glance

```text
1. Create a Slack app                          → https://api.slack.com/apps
2. Add the MVP 3 User Token scopes             → channels:history, channels:read, users:read
3. Install to workspace + copy the xoxp- token
4. opshub slack auth set                       # store the token (keyring)
5. opshub slack auth test                      # verify token + granted scopes
6. opshub slack conversations --format=toml    # discover channel ids
7. paste the printed block into opshub.toml    # already includes enabled = true
8. opshub slack sync                           # ingest — skills can now see Slack
```

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
opshub slack auth set
# paste the xoxp- token at the hidden prompt
```

The token is stored in the OS keychain under the single slot
`connector:slack:token` ([ADR-0014](adr/0014-saas-token-storage.md) /
ADR-0018). User Token and the Bot Token alternative share this one slot.

### (b) env var (CI / containers / WSL2)

```bash
export OPSHUB_CONNECTOR_SLACK_TOKEN="xoxp-..."
```

The env var wins over the keyring, so CI / Docker / WSL2 (where the OS
keychain may be unreachable) can inject the token without keyring setup.

> **env-var naming note (#535)**: the *token* env var is
> `OPSHUB_CONNECTOR_SLACK_TOKEN` (singular `CONNECTOR`, single
> underscore) because it is resolved by the keyring secret layer, which
> uses a flat naming scheme shared by every connector
> (`OPSHUB_CONNECTOR_<NAME>_TOKEN`, ADR-0014). Every *non-secret* Slack
> setting uses the nested config form `OPSHUB_CONNECTORS__SLACK__*`
> (plural `CONNECTORS`, double underscore) because those flow through
> the pydantic-settings nested-config loader (ADR-0032). The asymmetry
> is intentional and bounded to the secret slot; #535 left the token env
> on the secret-layer scheme rather than introducing a Slack-only third
> form (see issue #535 §"token env 統一は要 cross-connector 判断").

## 5. Verify the token

```bash
opshub slack auth test
```

Prints `status: ok` with the team / user / principal and the **granted
scopes** (Phase 23-C, #533) so you can confirm the scopes you intended
were actually approved. A missing scope here is the usual cause of an
empty `opshub slack conversations` result.

## 6. Discover channel ids

```bash
opshub slack conversations --format=toml
```

This lists the conversations your token can see and prints a
**complete, paste-ready `[connectors.slack]` block** — the section
header, `enabled = true`, and a commented `channels = [...]` array
(Phase 23-E, #535). Earlier versions printed a bare `channels = [...]`
array that silently landed in the wrong table when pasted; the
self-contained block removes that trap.

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
channels:

```toml
[connectors.slack]
enabled = true
channels = [
  "C0123ABC",  # general (public)
  "C0456DEF",  # leadership (private)
]
```

### channel ids via env var

`channels` also accepts a **comma-separated** env override
(Phase 23-E, #535) — no JSON quoting required for the common case:

```bash
export OPSHUB_CONNECTORS__SLACK__CHANNELS="C0123ABC,C0456DEF"
```

The JSON-document form is still accepted when you need per-channel
`since` floors:

```bash
export OPSHUB_CONNECTORS__SLACK__CHANNELS='[{"id":"C0123ABC","since":"30d"}]'
```

### optional date floor

`sync_since` caps the cold-start backfill so messages older than the
floor are never fetched (Phase 20,
[ADR-0036](adr/0036-slack-sync-date-floor.md)):

```toml
[connectors.slack]
enabled = true
channels = ["C0123ABC"]
sync_since = "90d"               # relative "90d"/"4w" or ISO "2026-01-01"; omit for full history
```

Per-channel overrides use the table form (`since = "all"` opts one
channel back into full history):

```toml
[[connectors.slack.channels]]
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

> **no-op guard (#535)**: if the connector is `enabled = false`, or
> `channels` is empty, `opshub slack sync` prints a visible
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

## Cursor / recovery

Slack cursor state is a 3-axis compound (`channels` / `backfill` /
`threads`). The working operations are the `opshub slack cursor`
subcommands (Phase 22-E,
[ADR-0038](adr/0038-slack-sync-gap-backfill.md) §(f)):

- `opshub slack cursor show` — pretty-print the cursor.
- `opshub slack cursor reset [--channel C1,C2 | --all]` — drop cursor
  entries so the selected channels cold-start on the next sync.
- `opshub slack cursor backfill --channel <id> --since <new> [--until <old>]`
  — explicit bounded backfill of a past window.

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
3. Store it the same way (`opshub slack auth set` or
   `OPSHUB_CONNECTOR_SLACK_TOKEN`) — the auth helper accepts either
   prefix.
4. `/invite` the bot into every channel you want ingested (a Bot Token
   only sees channels the bot has joined).

Note the engagement-axis sort (`--sort=last_self_post`, which uses
`search.messages`) is **User Token only** — a Bot Token raises a
`ConfigError` directing you to `--sort=last_activity`.

## Related docs

- [ADR-0018: Slack Token Principal](adr/0018-slack-token-principal.md)
- [ADR-0014: SaaS Token Storage](adr/0014-saas-token-storage.md)
- [ADR-0036: Slack Sync Date Floor](adr/0036-slack-sync-date-floor.md)
- [ADR-0038: Slack Sync Gap Backfill](adr/0038-slack-sync-gap-backfill.md)
- [docs/mcp-setup.md](mcp-setup.md) — wiring the assistant skills over MCP
- [SECURITY.md](../SECURITY.md) — local body retention posture
