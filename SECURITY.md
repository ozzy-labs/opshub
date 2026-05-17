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

OpsHub is a **local-first single-user CLI**
(`docs/principles.md` §1 Local-first). The threat model assumes:

- The CLI runs on a workstation the operator trusts and controls
- The SQLite database, config file, and keyring contents are protected by OS
  user-level access controls
- LLM / embedding / connector API tokens are stored in the OS keychain
  ([ADR-0014](docs/adr/0014-saas-token-storage.md)) — they never appear in
  the event log or stdout

### In scope

We treat the following as security issues:

- **API token leaks** — a token reaching `stdout` / `stderr` / event payload /
  log files. The `core.sanitise.sanitise_error_message` helper redacts known
  patterns (`sk-...` / `ghp_...` / `Bearer ...`) — bypasses are bugs
  ([ADR-0015](docs/adr/0015-llm-usage-strategy.md) §決定 (g)).
- **Prompt injection** — an attacker who can write content to a source body
  (e.g. a GitHub issue body or Slack message that gets ingested via a
  connector) causing the LLM to execute attacker-controlled instructions
  past the `<source>...</source>` boundary established by
  [ADR-0015](docs/adr/0015-llm-usage-strategy.md) §決定 (f).
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

### Out of scope

- Running OpsHub on an untrusted multi-user host (the threat model is
  single-user; multi-user is not supported)
- Compromise of the operator's workstation (keychain access, filesystem
  access) — those are upstream OS concerns
- Phishing / social engineering of the operator
- LLM hallucinations that produce incorrect but non-malicious content
  (this is an LLM quality issue, not a vulnerability)
- Memory consumption with maliciously large inputs (file an issue as a
  bug if you hit it, but not a security advisory)

## Cryptographic dependencies

OpsHub does not implement custom cryptography. We rely on:

- **OS keychain** (via [keyring](https://pypi.org/project/keyring/)) for
  token storage
- **TLS** (via the upstream SDK or `httpx`) for all SaaS API calls
- **SQLite** for data-at-rest (no encryption by default; users wanting
  encryption-at-rest should use OS-level filesystem encryption)

## Acknowledgements

We will credit reporters in the [CHANGELOG](CHANGELOG.md) and the
corresponding GitHub Security Advisory unless anonymity is requested.
