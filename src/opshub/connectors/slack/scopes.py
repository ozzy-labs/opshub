"""SSOT for Slack feature → OAuth scope mapping + readiness assessment.

Phase 23-I (#539, [ADR-0040](../../../../docs/adr/0040-slack-feature-scope-ssot-and-readiness.md)).

This leaf module is the **single source of truth** for *which OAuth scopes each
Slack ingestion feature needs*. Before #539 that knowledge was scattered across
``auth.py`` docstring / ``conversations.py`` / ``slack.py`` help / ADRs — a
drift source. :data:`FEATURE_SCOPES` is canonical; the ADRs carry the prose
rationale and point here rather than re-tabulating the data (which would be a
second SSOT).

The readiness model is a **capability model**: :func:`assess_readiness` answers
"does this token's granted scope set satisfy the *scope* preconditions for
feature X?" and nothing more. It deliberately does **not** read config or
resolve channel membership — ``not_in_channel`` is a different layer (a channel
you have not joined, or a bot not ``/invite``'d, still fails at fetch time even
with every scope granted). ADR-0040 records why config-aware / membership-aware
readiness was rejected: it would need per-channel ``conversations.info`` calls
and reintroduce the very ``not_in_channel`` "lie" the design set out to avoid.
The single membership caveat is surfaced once, as a footnote, by
:func:`render_features_block`.

``listing`` (the ``opshub slack conversations`` discovery path) is intentionally
**not** a readiness feature — it already self-reports a per-type
``missing_scope`` warning, so folding it into readiness would over-claim
(ADR-0040 §A).

Cold-start guard: this module imports nothing heavier than stdlib + dataclasses,
so importing it on the ``opshub --help`` path is free.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = [
    "FEATURE_SCOPES",
    "FeatureReadiness",
    "FeatureScopeSpec",
    "SlackFeature",
    "assess_readiness",
    "history_scope_for_type",
    "render_features_block",
]

#: Ingestion features whose scope preconditions ``opshub slack auth test``
#: reports on. Bare string literals (not :data:`ConversationType` from
#: ``conversations.py``) so this leaf module imports nothing from its peers.
SlackFeature = Literal[
    "sync_public",
    "sync_private",
    "sync_dm",
    "sync_mpim",
    "engagement_axis",
]


@dataclass(frozen=True, slots=True)
class FeatureScopeSpec:
    """The scope preconditions for one Slack feature (one SSOT row)."""

    #: Human display label shown in the ``auth test`` ``features:`` block.
    label: str
    #: Scopes that MUST all be granted for the feature to work at all.
    required: tuple[str, ...]
    #: Scopes that improve the feature but are not load-bearing (e.g.
    #: ``users:read`` resolves author display names). Absence downgrades the
    #: verdict to ``READY (degraded: ...)`` rather than ``MISSING``.
    recommended: tuple[str, ...]
    #: ``True`` when the feature is only reachable with a User Token — a Bot
    #: Token structurally cannot hold the scope (``search:read``, ADR-0034).
    user_token_only: bool


#: Feature → scope SSOT. Every other site that needs the mapping derives from
#: here (see :func:`history_scope_for_type`). The order is the display order of
#: the ``features:`` block.
FEATURE_SCOPES: dict[SlackFeature, FeatureScopeSpec] = {
    "sync_public": FeatureScopeSpec(
        label="public channel sync",
        required=("channels:history",),
        recommended=("users:read",),
        user_token_only=False,
    ),
    "sync_private": FeatureScopeSpec(
        label="private channel sync",
        required=("groups:history",),
        recommended=("users:read",),
        user_token_only=False,
    ),
    "sync_dm": FeatureScopeSpec(
        label="DM sync",
        required=("im:history",),
        recommended=("users:read",),
        user_token_only=False,
    ),
    "sync_mpim": FeatureScopeSpec(
        label="group-DM (mpim) sync",
        required=("mpim:history",),
        recommended=("users:read",),
        user_token_only=False,
    ),
    "engagement_axis": FeatureScopeSpec(
        label="engagement axis (--sort=last_self_post)",
        required=("search:read",),
        recommended=(),
        user_token_only=True,
    ),
}


#: Conversation-type string → the feature whose ``required`` scope is the
#: ``*:history`` scope for that type. The keys equal ``conversations.py``'s
#: ``ConversationType`` literal values (``public`` / ``private`` / ``im`` /
#: ``mpim``) — matching by string keeps this module free of a peer import.
_TYPE_TO_FEATURE: dict[str, SlackFeature] = {
    "public": "sync_public",
    "private": "sync_private",
    "im": "sync_dm",
    "mpim": "sync_mpim",
}


def history_scope_for_type(conversation_type: str) -> str:
    """Return the ``*:history`` scope required to fetch ``conversation_type``.

    The single derivation of the type → history-scope mapping that
    ``conversations.py`` repoints to (drift removal; behaviour unchanged —
    ``public`` → ``channels:history`` etc.). Raises :class:`KeyError` for an
    unknown type, matching the old dict-literal's ``KeyError`` contract.
    """
    return FEATURE_SCOPES[_TYPE_TO_FEATURE[conversation_type]].required[0]


@dataclass(frozen=True, slots=True)
class FeatureReadiness:
    """The assessed readiness of one feature for a given token.

    ``status`` is the fully-rendered display string (``READY`` /
    ``MISSING <scopes>`` / ``N/A (User Token only)`` /
    ``READY (degraded: +<scope> for display names)``); ``ready`` is the
    programmatic boolean (``True`` for READY and degraded-READY); ``missing``
    lists the unmet **required** scopes (empty unless the status is MISSING).
    """

    feature: SlackFeature
    label: str
    ready: bool
    status: str
    missing: tuple[str, ...]


def assess_readiness(scopes: str, principal: str) -> list[FeatureReadiness]:
    """Assess each feature's *scope* readiness from a token's granted scopes.

    Pure function: takes the comma-separated ``scopes`` string and
    ``principal`` from :meth:`SlackAuth.test_token` and returns one
    :class:`FeatureReadiness` per feature, in :data:`FEATURE_SCOPES` order. No
    I/O, no config, no API calls — a scope-layer capability model only
    (ADR-0040). An empty ``scopes`` yields all-``MISSING`` rows; the CLI
    short-circuits that case to a single "cannot assess" line via
    :func:`render_features_block`, so callers rarely see it here.
    """
    granted = {s.strip() for s in scopes.split(",") if s.strip()}
    results: list[FeatureReadiness] = []
    for feature, spec in FEATURE_SCOPES.items():
        if spec.user_token_only and principal == "bot":
            results.append(
                FeatureReadiness(
                    feature=feature,
                    label=spec.label,
                    ready=False,
                    status="N/A (User Token only)",
                    missing=(),
                )
            )
            continue
        missing = tuple(s for s in spec.required if s not in granted)
        if missing:
            results.append(
                FeatureReadiness(
                    feature=feature,
                    label=spec.label,
                    ready=False,
                    status=f"MISSING {' '.join(missing)}",
                    missing=missing,
                )
            )
            continue
        missing_recommended = [s for s in spec.recommended if s not in granted]
        if missing_recommended:
            detail = " ".join(f"+{s}" for s in missing_recommended)
            results.append(
                FeatureReadiness(
                    feature=feature,
                    label=spec.label,
                    ready=True,
                    status=f"READY (degraded: {detail} for display names)",
                    missing=(),
                )
            )
            continue
        results.append(
            FeatureReadiness(
                feature=feature, label=spec.label, ready=True, status="READY", missing=()
            )
        )
    return results


#: Surfaced once at the foot of the ``features:`` block. Readiness is a scope
#: verdict only; a granted scope does not imply the token can actually read a
#: given channel (membership is a separate layer — ADR-0040).
_MEMBERSHIP_FOOTNOTE = (
    "  note: READY = scope granted; it does not guarantee channel membership "
    "— a channel you have not joined, or a bot not /invite'd, still returns "
    "not_in_channel."
)


def render_features_block(scopes: str, principal: str) -> list[str]:
    """Render the ``features:`` block **body** for ``auth test`` (lines after the header).

    Handles the scopes-unavailable degrade (Slack omitted the
    ``x-oauth-scopes`` header → empty ``scopes``): a single explanatory line
    instead of per-feature verdicts (ADR-0040 / readiness algorithm step 1).
    Otherwise: one ``  <label>: <status>`` line per feature plus the
    membership footnote.
    """
    if not scopes.strip():
        return ["  (scopes header unavailable — cannot assess readiness)"]
    lines = [f"  {r.label}: {r.status}" for r in assess_readiness(scopes, principal)]
    lines.append(_MEMBERSHIP_FOOTNOTE)
    return lines
