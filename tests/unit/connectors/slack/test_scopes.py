"""Tests for the Slack feature→scope SSOT + readiness (Phase 23-I, #539, ADR-0040).

``scopes.py`` is a pure leaf module (no I/O, no config, no API). These tests
pin the capability model: a token's granted ``scopes`` + ``principal`` map to a
per-feature READY / MISSING / N/A verdict, and nothing reads channel membership.
"""

from __future__ import annotations

from opshub.connectors.slack.scopes import (
    FEATURE_SCOPES,
    assess_readiness,
    history_scope_for_type,
    render_features_block,
)

# A User Token that has been granted every scope the readiness model knows about.
_FULL_USER_SCOPES = "channels:history,groups:history,im:history,mpim:history,users:read,search:read"


def _status_by_feature(scopes: str, principal: str) -> dict[str, str]:
    return {r.feature: r.status for r in assess_readiness(scopes, principal)}


# ----- case 1: full User scope → every feature READY -----------------------


def test_full_user_scope_all_ready() -> None:
    statuses = _status_by_feature(_FULL_USER_SCOPES, "user")
    assert statuses == {
        "sync_public": "READY",
        "sync_private": "READY",
        "sync_dm": "READY",
        "sync_mpim": "READY",
        "engagement_axis": "READY",
    }
    assert all(r.ready for r in assess_readiness(_FULL_USER_SCOPES, "user"))


# ----- case 2: channels:history only → public degraded, others MISSING -----


def test_channels_history_only_public_degraded_others_missing() -> None:
    statuses = _status_by_feature("channels:history", "user")
    assert statuses["sync_public"] == "READY (degraded: +users:read for display names)"
    assert statuses["sync_private"] == "MISSING groups:history"
    assert statuses["sync_dm"] == "MISSING im:history"
    assert statuses["sync_mpim"] == "MISSING mpim:history"
    assert statuses["engagement_axis"] == "MISSING search:read"


# ----- case 3: Bot principal → engagement is N/A, not MISSING --------------


def test_bot_principal_engagement_is_not_applicable() -> None:
    """A Bot Token structurally cannot hold ``search:read`` (ADR-0034), so the
    engagement axis is ``N/A (User Token only)`` — not a ``MISSING`` an
    operator could "fix" by adding a scope."""
    bot_scopes = "channels:history,groups:history,im:history,mpim:history,users:read"
    results = {r.feature: r for r in assess_readiness(bot_scopes, "bot")}
    engagement = results["engagement_axis"]
    assert engagement.status == "N/A (User Token only)"
    assert engagement.ready is False
    assert engagement.missing == ()
    # The history syncs remain READY for a Bot Token with the scopes.
    assert results["sync_public"].status == "READY"


# ----- case 4: scopes unavailable → single "cannot assess" line ------------


def test_empty_scopes_renders_cannot_assess() -> None:
    """When Slack omits ``x-oauth-scopes`` the block degrades to one line
    rather than emitting a misleading all-MISSING verdict."""
    assert render_features_block("", "user") == [
        "  (scopes header unavailable — cannot assess readiness)"
    ]
    # Whitespace-only is treated identically.
    assert render_features_block("   ", "user")[0].startswith("  (scopes header unavailable")


# ----- case 5: recommended-only missing → degraded READY -------------------


def test_recommended_only_missing_is_degraded_ready() -> None:
    results = {r.feature: r for r in assess_readiness("channels:history", "user")}
    public = results["sync_public"]
    assert public.ready is True
    assert public.status == "READY (degraded: +users:read for display names)"
    assert public.missing == ()


# ----- case 6: every feature has a display label (output stability) --------


def test_every_feature_has_a_label() -> None:
    for feature, spec in FEATURE_SCOPES.items():
        assert spec.label, f"{feature} is missing a display label"
        assert spec.required, f"{feature} must have at least one required scope"


# ----- render: per-feature lines + membership footnote ---------------------


def test_render_block_includes_membership_footnote() -> None:
    lines = render_features_block(_FULL_USER_SCOPES, "user")
    # One line per feature + the membership footnote.
    assert len(lines) == len(FEATURE_SCOPES) + 1
    assert any("public channel sync: READY" in line for line in lines)
    footnote = lines[-1]
    assert "does not guarantee channel membership" in footnote
    assert "not_in_channel" in footnote


# ----- SSOT repoint: history_scope_for_type is the single derivation -------


def test_history_scope_for_type_matches_legacy_values() -> None:
    """The values ``conversations.py`` previously hard-coded, now derived."""
    assert history_scope_for_type("public") == "channels:history"
    assert history_scope_for_type("private") == "groups:history"
    assert history_scope_for_type("im") == "im:history"
    assert history_scope_for_type("mpim") == "mpim:history"
