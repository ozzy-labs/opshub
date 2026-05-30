# Security Policy

## Supported versions

OpsHub is in pre-1.0 (`0.x`) development. Security fixes are issued only for
the latest minor version line:

| Version  | Status      | Security fixes                          |
| -------- | ----------- | --------------------------------------- |
| 0.1.x    | Current     | Yes                                     |
| < 0.1.0  | Pre-release | No (development snapshots only)         |

When the next minor version ships (e.g. 0.2.0), the previous line (0.1.x)
will receive a 30-day grace period for backported security fixes before
being declared end-of-life.

## Reporting a vulnerability

If you discover a security vulnerability in OpsHub, please report it through
[GitHub Private Vulnerability Reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability):

1. Go to the **Security** tab of this repository
2. Click **Report a vulnerability**
3. Fill in the details and submit

We acknowledge reports within **3 business days** and aim to release a fix as
soon as the severity-appropriate timeline allows:

| Severity                                 | Target time to fix |
| ---------------------------------------- | ------------------ |
| Critical (RCE, secret leak, auth bypass) | 7 days             |
| High                                     | 14 days            |
| Medium / Low                             | 30 days            |

**Please do not open a public issue for security vulnerabilities.**

## Scope

OpsHub is a **local-first single-user CLI / secretary agent platform**
([`docs/principles.md`](docs/principles.md) §1 Local-first). The threat model assumes:

- The CLI runs on a workstation the operator trusts and controls
- The SQLite database, config file, and keyring contents are protected by OS
  user-level access controls
- LLM / embedding / connector API tokens are stored in the OS keychain
  ([ADR-0014](docs/adr/0014-saas-token-storage.md)) — they never appear in
  the event log, the source body store, MCP tool arguments, or stdout
- The MCP server runs in **stdio one transport** only — no HTTP / SSE
  listener is implemented ([ADR-0022](docs/adr/0022-mcp-server-surface.md)
  §(a)). Network-listen surface is structurally non-applicable

### Phase 10 body retention — what changed

Phase 10 ([ADR-0020](docs/adr/0020-full-local-content-retention.md), supersedes ADR-0005) switched OpsHub from "retain summaries only" to **retain full source body locally** (Slack message bodies, GitHub issue / PR bodies, Outlook bodies, Box text extraction). This is the unavoidable backbone of the secretary agent platform — recall, cross-source search, and reply-draft style replication all require body access. The shift carries operator responsibilities:

- **Encryption at rest** (`[storage] encryption = true` in `opshub.toml`, [ADR-0021](docs/adr/0021-encryption-at-rest.md)) is *opt-in*. Operators handling regulated / sensitive bodies should enable it; the SQLCipher backend ships under the `encryption` extras and reuses the keyring path (`db:encryption_key` slot, env override `OPSHUB_DB_ENCRYPTION_KEY`). Without encryption, the DB stores bodies as on-disk plaintext — pinned by `tests/unit/core/test_encryption.py::test_unencrypted_db_leaks_body_as_plaintext` so the regression cannot land silently.
- **Ingest excludes** (`~/.config/opshub/excludes.yaml`, ADR-0020 §(b)) keep specific channels / senders / repos / paths out of the body store entirely — operators decide what *not* to retain.
- **Backups** — encrypted or not, the SQLite file under `$XDG_DATA_HOME/opshub/db/` is now the canonical store of your source body history. Treat it like you would your password vault.
- **Multi-user / shared workstation operation is unchanged: out of scope.**

### Phase 11 body retention — Office documents, Teams chat, Outlook deep body

Phase 11 widens the body-retention surface to:

- **Microsoft Teams chat bodies** ([ADR-0010](docs/adr/0010-connector-contract.md) §改訂 (a)) — chat messages ingested through Microsoft Graph delta query land in `sources.body` tagged `provenance_origin="external"` + `provenance_trust="untrusted"` (ADR-0020 §(e)). The `teams` connector uses **User Token (delegated permissions)** by default ([ADR-0010](docs/adr/0010-connector-contract.md) §改訂 (d)); a Bot Token (application permissions) is an alternative that grants tenant-wide chat access — operators must consider the privacy implication before opting in (`docs/teams-setup.md` "Bot Token" section).
- **Outlook deep body** ([ADR-0010](docs/adr/0010-connector-contract.md) §改訂 (a) + Phase 11 F3) — the existing `ms365` connector's Outlook mapper now retains the full email body (head-truncated at 500K chars). Pre-Phase-11 rows with `body = NULL` continue to round-trip cleanly.
- **Office document content extraction** ([ADR-0025](docs/adr/0025-office-document-content-extraction.md)) — when an operator opts in (`[connectors.box_drive] content_extraction = true` / `[connectors.onedrive_drive] content_extraction = true`), `.docx` / `.xlsx` / `.pptx` files on the scanned FS mount are extracted via markitdown and stored under new `source_type`s (`word_document` / `excel_spreadsheet` / `powerpoint_slide_deck`). The opt-in narrows the ADR-0019 §不変条件 (b) `open()` ban to a single extractor path (ADR-0019 §決定 (b')) — diff path (`stat()`-only fingerprint) stays unchanged, so CldAPI / File Provider Extension hydration cost is bounded.

Phase 11 operator responsibilities continue along the same axes as Phase 10:

- **Encryption at rest** covers the new body classes transparently — the `[storage] encryption = true` switch already encrypts the entire SQLite DB, so Office body content and Teams chat bodies inherit the same protection without additional knobs.
- **Ingest excludes** extend to Teams via the existing `channels` / `senders` selector (matched against `chat_id` / `chat_topic` / `sender_id`) and to FS-scan connectors via the existing `paths` selector (matched against `rel_path`). The same `~/.config/opshub/excludes.yaml` covers all 7 connectors.
- **Teams token handling** — Microsoft Graph User Tokens are JWT-shaped bearers (not the `xoxp-` form Slack uses). Tokens flow through the existing `core/secrets` + keyring path (ADR-0014 reuse) with `OPSHUB_CONNECTOR_TEAMS_TOKEN` env override. The mapper never embeds the token in observed messages, and the MCP layer's `redact_secrets` continues to strip JWT-shaped fragments from tool returns ([ADR-0022](docs/adr/0022-mcp-server-surface.md) §(b)).
- **Office extraction is `content_extraction = true` opt-in** — default-off keeps Phase 9/10 operators on the metadata-only FS scan path. Enabling it pulls only the markitdown sub-deps the extras declare (`mammoth` / `openpyxl` / `python-pptx`), so the cold-install footprint stays small.
- **External write-back remains forbidden** ([ADR-0010](docs/adr/0010-connector-contract.md) §禁止事項 7). The Teams connector deliberately exposes no `send` / `post` / `reply` callable; reply-draft skill output is local only.

### In scope

We treat the following as security issues:

- **API token leaks** — a token reaching `stdout` / `stderr` / event payload /
  log files / MCP tool arguments / MCP tool results / source body store. The `core.sanitise.sanitise_error_message` helper redacts known
  patterns (`sk-...` / `ghp_...` / `Bearer ...`); the MCP layer additionally runs
  every tool return through `opshub.mcp._redact.redact_secrets`
  ([ADR-0022](docs/adr/0022-mcp-server-surface.md) §(b)) — bypasses are bugs
  ([ADR-0015](docs/adr/0015-llm-usage-strategy.md) §決定 (g)).
- **Prompt injection / memory poisoning** — an attacker who can write content
  to a source body (e.g. a GitHub issue body or Slack message that gets ingested via a
  connector) causing the LLM to execute attacker-controlled instructions
  past the `<source>...</source>` boundary established by
  [ADR-0015](docs/adr/0015-llm-usage-strategy.md) §決定 (f). Phase 10 added
  the new attack surface "poisoned body in the local store" ([ADR-0020](docs/adr/0020-full-local-content-retention.md)
  §Negative) — mitigated by `provenance_origin="external"` +
  `provenance_trust="untrusted"` tags on all connector-fetched bodies and the
  append-only rollback path (rebuild from event log discards poisoned projections).
- **Event-log integrity** — bypassing `EventStore.append` to mutate state
  without an event
  ([ADR-0002](docs/adr/0002-event-sourced-architecture.md)).
- **Apply-path bypass** — `ProposalService.apply` reaching `tasks` /
  `decisions` projections without going through `TaskService.create_task` /
  `DecisionService.record_decision`
  ([ADR-0016](docs/adr/0016-action-loop-and-structured-output.md) §決定 (g)).
- **Auto-apply** — any code path that bypasses the human-in-the-loop apply
  contract (ADR-0016 §決定 (c) — `opshub propose apply` must be
  operator-triggered).
- **MCP boundary violations** — a write-class MCP tool advertising
  `readOnlyHint=true`, an MCP tool input schema accepting a SaaS token, the
  MCP server opening a network listener (HTTP / SSE), or an MCP handler
  echoing full body text instead of a ≤200-char snippet ([ADR-0022](docs/adr/0022-mcp-server-surface.md)
  §(a)–§(d)). Pinned by `tests/unit/mcp/test_no_network_listen` and
  `tests/unit/mcp/test_registry_policy`.
- **External write-back path appearing** — any code that posts to Slack /
  GitHub / Box / MS365 / writes back to SaaS ([ADR-0010](docs/adr/0010-connector-contract.md)
  §禁止事項 7, Phase 10 Sub-issue E). The secretary deliberately drafts only;
  the operator sends. A future PR adding a `send` / `post` / `comment_create`
  connector method without a separate ADR + opt-in is a security regression.

### Out of scope

- Running OpsHub on an untrusted multi-user host (the threat model is
  single-user; multi-user is not supported)
- Compromise of the operator's workstation (keychain access, filesystem
  access, swap / hibernate disk recovery) — those are upstream OS concerns
- Phishing / social engineering of the operator
- LLM hallucinations that produce incorrect but non-malicious content
  (this is an LLM quality issue, not a vulnerability)
- Memory consumption with maliciously large inputs (file an issue as a
  bug if you hit it, but not a security advisory)
- Operator choice **not** to enable `[storage] encryption` while retaining
  sensitive bodies — this is a documented opt-in (see [Phase 10 body
  retention — what changed](#phase-10-body-retention--what-changed) above)

## Cryptographic dependencies

OpsHub does not implement custom cryptography. We rely on:

- **OS keychain** (via [keyring](https://pypi.org/project/keyring/)) for
  SaaS token storage and the SQLCipher DB key (ADR-0014 reuse, [ADR-0021](docs/adr/0021-encryption-at-rest.md) §(b))
- **TLS** (via the upstream SDK or `httpx`) for all SaaS API calls
- **SQLCipher** (via `sqlcipher3-binary`, `encryption` extras) for at-rest
  AES-256 encryption of the entire SQLite DB ([ADR-0021](docs/adr/0021-encryption-at-rest.md),
  opt-in). Operators not enabling it should layer OS-level filesystem
  encryption (FileVault / LUKS / BitLocker) instead

## Acknowledgements

We will credit reporters in the [CHANGELOG](CHANGELOG.md) and the
corresponding GitHub Security Advisory unless anonymity is requested.
