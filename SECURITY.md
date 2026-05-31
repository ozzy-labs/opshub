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
- **Ingest excludes** extend to Teams via the existing `channels` / `senders` selector (matched against `chat_id` / `chat_topic` / `sender_id`) and to FS-scan connectors via the existing `paths` selector (matched against `rel_path`). The same `~/.config/opshub/excludes.yaml` covers all 7 connectors. The file uses **top-level flat keys only** (`channels` / `senders` / `repos` / `paths` per ADR-0020 §(b)); nested per-connector forms (e.g. `teams: { channels: [...] }`) are **rejected** with `ConfigError` so a typo / stale doc never silently disables an intended exclusion (pinned by `tests/unit/core/test_excludes.py::test_nested_form_raises`).
- **Teams token handling** — Microsoft Graph User Tokens are JWT-shaped bearers (not the `xoxp-` form Slack uses). Tokens flow through the existing `core/secrets` + keyring path (ADR-0014 reuse) with `OPSHUB_CONNECTOR_TEAMS_TOKEN` env override. The mapper never embeds the token in observed messages, and the MCP layer's `redact_secrets` continues to strip JWT-shaped fragments from tool returns ([ADR-0022](docs/adr/0022-mcp-server-surface.md) §(b)).
- **Office extraction is `content_extraction = true` opt-in** — default-off keeps Phase 9/10 operators on the metadata-only FS scan path. Enabling it pulls only the markitdown sub-deps the extras declare (`mammoth` / `openpyxl` / `python-pptx`), so the cold-install footprint stays small.
- **External write-back remains forbidden** ([ADR-0010](docs/adr/0010-connector-contract.md) §禁止事項 7). The Teams connector deliberately exposes no `send` / `post` / `reply` callable; reply-draft skill output is local only.

### Phase 12 Secretary skills expansion — what changed

Phase 12 ([ADR-0004](docs/adr/0004-agent-runtime-boundary.md) revision §決定 (c-2) + [ADR-0022](docs/adr/0022-mcp-server-surface.md) revision §決定 (f) + [ADR-0016](docs/adr/0016-action-loop-and-structured-output.md) revision §決定 (l)) grows the secretary skill catalog from 5 to 14 and widens the MCP surface with 4 new tools (`search` FTS5 + `propose.apply` HITL idempotent + physical-column time filters on the existing 4 read tools). The MCP boundary surface picks up **one carve-out** but no relaxations:

- **`propose.apply` is the first write-class MCP tool to advertise `destructive=false`** ([ADR-0022](docs/adr/0022-mcp-server-surface.md) §決定 (f), `WriteCategory.PROPOSE_APPLY`, `destructive=false` + `idempotent=true`). All other write tools (`task.create` / `inbox.add` / `connector.sync` / `propose.generate`) keep `destructive=true`. The carve-out is admissible because the apply path is idempotent at the handler layer: the handler catches `OpsHubError("already applied" / "already rejected")` from `ProposalService.apply` and normalises the second-call response to `{ok: true, already_applied: true, applied_entity_id}` so retries never throw and never produce a second persist. The annotation honesty contract (`tests/unit/mcp/test_registry_policy`) pins this — a write tool advertising `destructive=false` without an idempotent handler is a regression.
- **The HITL apply contract is unchanged** ([ADR-0016](docs/adr/0016-action-loop-and-structured-output.md) §決定 (c)). `propose.apply` is still operator-triggered — the host LLM cannot self-dispatch it; the MCP annotation policy continues to require human confirmation on every write tool ([ADR-0022](docs/adr/0022-mcp-server-surface.md) annotation policy, Phase 10).
- **No new external write-back path.** The 4 new HITL-write skills (`reply-draft` was already Phase 10; `inbox-triage` / `source-extract` / `meeting-followup` are new) all draft into local `proposals` / `inbox_items` / `tasks` projections; none of them post back to SaaS. The Phase 10/11 write-back ban ([ADR-0010](docs/adr/0010-connector-contract.md) §禁止事項 7, 緊張点③) continues to hold. The two text-only draft skills (`handoff-draft` / `announcement-draft`) deliberately have **no persist path at all** — they emit Markdown for the operator to copy/paste, and there is no `propose apply` route for them.
- **No DB schema changes / no event-schema changes / no new connectors.** The new MCP tools read from existing projections (`task.list` / `inbox.list` / `decision.list` / `source.list` / `sources_fts`) or wrap existing services (`ProposalService.apply`). No new attack surface in storage / projections / connectors.
- **`search` MCP tool surface**: FTS5 input is phrase-quoted by default; the CLI-only `--raw-query` flag is intentionally absent from the MCP schema so a host LLM cannot smuggle raw MATCH operators through the boundary. Pinned by the schema test in `tests/unit/mcp/test_phase12_handlers.py`.

### Phase 13 Google Workspace ingest — what changed

Phase 13 ([ADR-0010](docs/adr/0010-connector-contract.md) revision §Phase 13 (e)-(h) + [ADR-0014](docs/adr/0014-saas-token-storage.md) revision (rotation pin list 3rd entry) + [ADR-0025](docs/adr/0025-office-document-content-extraction.md) revision (§決定 (d') + (j))) widens the body-retention surface to:

- **Google Workspace native bodies** ([ADR-0025](docs/adr/0025-office-document-content-extraction.md) §決定 (d') + (j)) — when an operator opts in (`[connectors.google_workspace] content_extraction = true` + `[office]` extras), `google_doc` / `google_slides` / `google_sheets` files are exported via Drive API `files.export(fileId, mimeType=<MS Office mediatype>)` and routed through the Phase 11 markitdown path (`core/document_extract.extract_workspace_export(bytes, source_type)`). The bodies land in `sources.body` tagged `provenance_origin="external"` + `provenance_trust="untrusted"` ([ADR-0020](docs/adr/0020-full-local-content-retention.md) §(e)) — same poisoning-mitigation contract as Phase 10 / 11 bodies. Non-native Drive files (the catch-all `google_workspace_file` source_type) stay metadata-only regardless of the opt-in.

Phase 13 operator responsibilities continue along the same axes as Phase 10 / 11:

- **Encryption at rest** covers the new Google Workspace body classes transparently — the `[storage] encryption = true` switch already encrypts the entire SQLite DB, so `google_doc` / `google_slides` / `google_sheets` bodies inherit the same protection without additional knobs.
- **Ingest excludes** extend to the Google Workspace connector via the existing `paths` selector in `~/.config/opshub/excludes.yaml` ([ADR-0020](docs/adr/0020-full-local-content-retention.md) §(b)) — matched against the Drive item path so an operator can keep a specific Drive folder out of the body store at fetch time. The top-level flat key convention is the same as Phase 10 / 11 (nested `google_workspace: {...}` forms are rejected with `ConfigError`).
- **Token handling** — Google Refresh Tokens flow through the existing `core/secrets` + keyring path ([ADR-0014](docs/adr/0014-saas-token-storage.md) reuse) under `connector:google_workspace:refresh_token` with `OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN` env override. The connector follows the MS365 / Box rotation-write-back pattern ([ADR-0010](docs/adr/0010-connector-contract.md) §Phase 13 改訂 (h)): when Google returns a fresh refresh token during a refresh round-trip, the new value is written back to the keychain so the next process resumes cleanly. This is **deliberately separate from the Teams verbatim user-token pattern** — the two patterns coexist in ADR-0010 because Google Drive's ~1 hour access-token TTL makes verbatim-token storage operator-hostile (re-paste every hour), while Teams' operator-driven MSAL device-code flow is verbatim by design. Tokens never appear in observed Drive items, and the MCP layer's `redact_secrets` continues to strip JWT-shaped fragments from tool returns ([ADR-0022](docs/adr/0022-mcp-server-surface.md) §(b)).
- **Workspace export extraction is `content_extraction = true` opt-in** — default-off keeps Phase 13 G3 operators on the metadata-only path (no `files.export` round-trip, no markitdown cost). Enabling it requires both the `[connectors-google-workspace]` extras (for the Drive API client) and the `[office]` extras (for markitdown). The Phase 11 size / chars caps ([ADR-0025](docs/adr/0025-office-document-content-extraction.md) §決定 (b)) carry over verbatim; the cap is applied to the exported MS Office bytes rather than the Google-native size that Drive does not expose for native Docs / Sheets / Slides.
- **External write-back remains forbidden** ([ADR-0010](docs/adr/0010-connector-contract.md) §禁止事項 7). The `google_workspace` connector deliberately exposes no Drive write API callables (`files.update` / `files.create` / `files.copy` / `comments.create` / `permissions.*`); reply-draft / handoff / announcement skill output is local only. The `forbidden_callables` structural guard in the lifecycle integration tests covers the new connector too.
- **Drive push notification (`files.watch`) is forbidden** ([ADR-0010](docs/adr/0010-connector-contract.md) §Phase 13 改訂 (e) §禁止事項拡張). The connector polls Drive API `changes.list` only — `files.watch` would inject server-driven activity into opshub and break form-A (no proactive runtime, [ADR-0004](docs/adr/0004-agent-runtime-boundary.md)). Re-enabling `files.watch` requires a new ADR + opt-in + form-A revisit.

### Phase 14 Gmail + Google Calendar ingest — what changed

Phase 14 ([ADR-0010](docs/adr/0010-connector-contract.md) revision §Phase 14 (i)-(m) + [ADR-0014](docs/adr/0014-saas-token-storage.md) revision (scope expansion + shared auth foundation)) adds two new connectors — `google_mail` (Gmail) and `google_calendar` (Google Calendar) — that share the existing Phase 13 Google OAuth principal via a new shared `connectors/google_auth/` foundation. **No new DB schema, no breaking CLI changes, no new extras** (`connectors-google-workspace` is reused for httpx). The body-retention surface widens to:

- **Gmail bodies** (`gmail_message`、Outlook と symmetric な抽出) — text/plain preferred → text/html raw kept; **no markitdown**; **no attachment retention**. Bodies land in `sources.body` tagged `provenance_origin="external"` + `provenance_trust="untrusted"` ([ADR-0020](docs/adr/0020-full-local-content-retention.md) §(e)) — same poisoning-mitigation contract as Phase 10 / 11 / 13 bodies. Labels prepend as `[Labels: INBOX, ImportantWork]`; truncation appends `[gmail body truncated: N / M chars]`; `threadId` is kept as a field.
- **Google Calendar bodies** (`google_calendar`、MS365 Calendar と symmetric な抽出) — master event only (`recurringEventId` 無し)。Google API returns recurring overrides as standalone events; these are emitted as **separate `SourceObserved` records sharing `source_type="google_calendar"`** with `Override of: <master_id> (originalStart: <iso>)` back-pointer in the body. Summary follows `f"{start_iso} - {end_iso} ({attendees_count} attendees)"`; attendee email list / description / location are appended to the body; RRULE is kept as a field.

Phase 14 operator responsibilities continue along the same axes as Phase 13:

- **Encryption at rest** covers the new Gmail / Calendar body classes transparently — the `[storage] encryption = true` switch already encrypts the entire SQLite DB, so `gmail_message` / `google_calendar` bodies inherit the same protection without additional knobs.
- **Ingest excludes** extend to both connectors via the existing top-level flat selectors in `~/.config/opshub/excludes.yaml` ([ADR-0020](docs/adr/0020-full-local-content-retention.md) §(b)) — `senders` for Gmail addresses, `paths` / event titles for Calendar.
- **Token handling** — Gmail / Calendar reuse the existing `connector:google_workspace:refresh_token` keyring slot with `OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN` env override. Phase 14 widens the scope at the OAuth principal layer to the 3-scope fixed list `drive.readonly + gmail.readonly + calendar.readonly` ([ADR-0014](docs/adr/0014-saas-token-storage.md) revision). **One re-consent applies to all three connectors** (1 Google account = 1 principal sharing Drive + Gmail + Calendar). The token-rotation-write-back pattern from Phase 13 ([ADR-0010](docs/adr/0010-connector-contract.md) §Phase 13 改訂 (h)) carries over unchanged; the rotation pin test moved to the shared auth foundation (`tests/unit/connectors/google_auth/`) so a single test covers all three connectors.
- **External write-back remains forbidden** ([ADR-0010](docs/adr/0010-connector-contract.md) §禁止事項 7). Gmail `send` API and Calendar `events.insert` / `events.patch` / `events.delete` are deliberately not implemented in either connector; reply-draft / handoff / announcement skill output is local only. The `forbidden_callables` structural guard in the lifecycle integration tests covers both new connectors too.
- **Gmail / Calendar push notification (`users.watch` / `events.watch`) is forbidden** ([ADR-0010](docs/adr/0010-connector-contract.md) §Phase 14 改訂 (i)). Both connectors poll only — `watch` would inject server-driven activity into opshub and break form-A. Re-enabling requires a new ADR + opt-in + form-A revisit (Phase 15+ proactive-secretary work re-evaluates Drive / Gmail / Calendar push notifications together).
- **Mapper symmetry** is mechanically verified by `tests/unit/connectors/test_mapper_symmetry.py` — Outlook ↔ Gmail (8 cases) and ms365_calendar ↔ google_calendar (6 cases), 14 cases total. The symmetry stops mapper drift from creating asymmetric body shapes that would surprise downstream skills (e.g. `find-document` filtering by source_type).

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
