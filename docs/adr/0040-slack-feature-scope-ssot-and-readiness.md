# 0040. Slack feature→scope SSOT + auth-test readiness (capability model)

- Status: Accepted + Landed (Phase 23-I, epic [#530](https://github.com/ozzy-labs/opshub/issues/530))
- Date: 2026-06-10
- Deciders: opshub maintainers
- Related: [ADR-0018](0018-slack-token-principal.md) (token principal + scope catalogue — the prose rationale that now points here for the data), [ADR-0033](0033-slack-mention-demand-digest.md) (demand digest is covered by history scopes — no independent readiness row), [ADR-0034](0034-slack-engagement-axis.md) (`search:read` is User-Token-only — the basis for the engagement axis `N/A` verdict), [#533](https://github.com/ozzy-labs/opshub/issues/533) (granted-scope display, the feature this builds on)

## Context

`opshub slack auth test` (#533) already shows the token's **granted** scopes
(from the `x-oauth-scopes` header). The natural next question — *"given these
scopes, what can I actually do, and what scope do I add to unlock the rest?"* —
was deferred from #533's critique for two reasons:

1. The feature → scope correspondence lived as a **second source** scattered
   across `auth.py` docstring, `conversations.py` (`_HISTORY_SCOPE_FOR_TYPE`),
   `slack.py` help, `_slack_conversations.py`, `fetcher.py`, and ADRs
   0018/0033/0034/0035 — **6+ sites**, a textbook drift source.
2. A naïve "is feature X ready?" check risks a **new false negative/positive**:
   channel membership (`not_in_channel`) is *not* a scope property. A token can
   hold `channels:history` yet fail to read a channel it has not joined. A
   readiness check that implied membership would manufacture the very silent-
   wrong-answer pathology epic #530 set out to kill.

## Decision

1. **SSOT = a single code const.** The feature → scope mapping lives in one new
   leaf module `src/opshub/connectors/slack/scopes.py` (`FEATURE_SCOPES`). The
   **code const is canonical**; ADRs carry prose rationale and *point* at it
   rather than re-tabulating the data (a table duplicated as ADR data would be
   a second SSOT). `conversations.py`'s `_HISTORY_SCOPE_FOR_TYPE` is repointed
   to derive from it (`history_scope_for_type`), behaviour unchanged.
2. **Readiness is a capability model — scope layer only.** `assess_readiness`
   answers solely "does the granted scope set satisfy feature X's *scope*
   preconditions?" It reads **no config and no channel membership**, makes
   **zero extra API calls** (it derives from the `scopes` + `principal` already
   in the `test_token()` response). Membership is explicitly a *different
   layer*, surfaced once as a footnote (`READY = scope granted; does not
   guarantee channel membership — …not_in_channel`).
3. **Surface = a `features:` block on `auth test` stdout.** No new verb. Each
   feature renders `READY` / `READY (degraded: +<scope> …)` /
   `MISSING <scopes>` / `N/A (User Token only)`. The block is part of the
   command's primary value, so it goes to **stdout** (the `next:` hint stays
   stderr).
4. **Readiness covers ingestion features only** (public / private / DM /
   group-DM sync + the engagement axis). `listing` (`opshub slack
   conversations`) is excluded: it already self-reports a per-type
   `missing_scope` warning, so a readiness row would over-claim (`channels:read`
   alone would read `READY` while only public channels enumerate). The demand
   digest and thread-reply ingestion are covered by the history scopes
   (ADR-0033), so they are footnotes, not independent rows.

### Why not config-aware / membership-aware readiness

The issue originally imagined "for the *configured* channels/types, is each
feature ready?". Carried to its conclusion this is unsafe: config `channels`
hold **ids only** (no type), and a channel id prefix (`C`/`D`/`G`) does not
determine type. Resolving type needs `conversations.info` **per channel** —
i.e. membership resolution — which is exactly the `not_in_channel` layer and a
per-channel API cost. So readiness is bounded at the scope layer (the safe-side
answer to the issue's "what does readiness guarantee?" question).

## Consequences

- The 6+ scattered scope sites collapse to one const; `auth.py` docstring and
  `conversations.py` now reference it instead of restating it.
- `auth test` becomes a one-call preflight: token validity + granted scopes +
  per-feature capability, no extra round-trip.
- Readiness never over-claims: it asserts scope truth only, and the membership
  caveat is stated once so `READY` is not misread as "can read any channel".
- A Bot Token sees the engagement axis as `N/A (User Token only)` rather than a
  `MISSING` it could never satisfy (ADR-0034).
- `slack.py` / `_slack_conversations.py` help strings that mention `search:read`
  in prose are **left as-is** (residual static copy — they cannot literal-
  reference a const, and repointing them is over-reach).

## Alternatives Considered

- **config-aware / membership-aware readiness** — rejected (above): reintroduces
  `not_in_channel`, adds per-channel API cost.
- **ADR holds the correspondence table as data** — rejected: a second SSOT
  that drifts from the code const.
- **A new `opshub slack preflight` verb** — rejected: epic #530 reduces surface;
  `auth test` already resolves the token and is the natural home.
- **Include `listing` as a readiness feature** — rejected: `channels:read` alone
  would read `READY` while only public channels enumerate (over-claim);
  `conversations` already self-reports per-type `missing_scope`.

## Validation

Pinned by `tests/unit/connectors/slack/test_scopes.py` (full-scope all-READY /
channels-only degraded+missing mix / Bot principal → engagement `N/A` /
scopes-unavailable degrade / recommended-only → degraded / every feature
labelled / membership footnote present / `history_scope_for_type` legacy values)
and `tests/unit/cli/test_slack_auth.py` (the `features:` block on stdout, the
Bot-token `N/A`, and the no-scopes-header degrade). The repoint is behaviour-
neutral: the existing per-type `test_conversations.py` warnings pin
`_HISTORY_SCOPE_FOR_TYPE` indirectly and stay green.
