"""Tests for :mod:`opshub.services.operator_identity` (Phase 25-A).

Pins the per-connector ``is_authored_by_operator`` resolution:

* email connectors (``google_mail`` / ``ms365`` / ``google_calendar`` /
  ``google_workspace``) compare ``author_handle`` against
  ``operator_email`` case-insensitively;
* ``github`` compares against ``operator_login`` case-insensitively;
* ``teams`` compares against ``operator_id`` exactly (opaque GUID);
* ``slack`` resolves the per-workspace self user id (env override path)
  keyed on the ``team_id`` parsed from the source ``external_id``;
* every "cannot tell" case (no handle, unconfigured identity, authorless
  connector) degrades to ``False``.

Env overrides flow through pydantic-settings' nested-env path
(``OPSHUB_CONNECTORS__<CONN>__<FIELD>``) and the slack env override
(``OPSHUB_SLACK_SELF_USER_ID__<ALIAS>``) so the tests drive the helper
without a keyring / live Slack API.
"""

from __future__ import annotations

import pytest

from opshub.services.operator_identity import SourceAuthor, is_authored_by_operator

# ---- safe defaults --------------------------------------------------------


def test_no_handle_is_not_operator() -> None:
    """A source with no author (``author_handle is None``) is never self."""
    assert is_authored_by_operator(SourceAuthor("github", None)) is False
    assert is_authored_by_operator(SourceAuthor("github", "  ")) is False


def test_authorless_connectors_are_not_operator() -> None:
    """Local-FS / web connectors have no self concept → always ``False``."""
    for connector in ("box_drive", "onedrive_drive", "web", "unknown_connector"):
        assert is_authored_by_operator(SourceAuthor(connector, "anything")) is False


def test_unconfigured_identity_degrades_to_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no ``operator_*`` configured the helper never guesses self."""
    monkeypatch.delenv("OPSHUB_CONNECTORS__GITHUB__OPERATOR_LOGIN", raising=False)
    monkeypatch.delenv("OPSHUB_CONNECTORS__MS365__OPERATOR_EMAIL", raising=False)
    assert is_authored_by_operator(SourceAuthor("github", "octocat")) is False
    assert is_authored_by_operator(SourceAuthor("ms365", "me@example.com")) is False


# ---- github ---------------------------------------------------------------


def test_github_login_matches_case_insensitively(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSHUB_CONNECTORS__GITHUB__OPERATOR_LOGIN", "OctoCat")
    assert is_authored_by_operator(SourceAuthor("github", "octocat")) is True
    assert is_authored_by_operator(SourceAuthor("github", "OCTOCAT")) is True
    assert is_authored_by_operator(SourceAuthor("github", "someone-else")) is False


# ---- email connectors -----------------------------------------------------


@pytest.mark.parametrize(
    ("connector", "env"),
    [
        ("google_mail", "OPSHUB_CONNECTORS__GOOGLE_MAIL__OPERATOR_EMAIL"),
        ("ms365", "OPSHUB_CONNECTORS__MS365__OPERATOR_EMAIL"),
        ("google_calendar", "OPSHUB_CONNECTORS__GOOGLE_CALENDAR__OPERATOR_EMAIL"),
        ("google_workspace", "OPSHUB_CONNECTORS__GOOGLE_WORKSPACE__OPERATOR_EMAIL"),
    ],
)
def test_email_connectors_match_case_insensitively(
    connector: str, env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(env, "Me@Example.com")
    assert is_authored_by_operator(SourceAuthor(connector, "me@example.com")) is True
    # The mappers lower-case the handle, but the helper case-folds the
    # configured value too so a mixed-case config still matches.
    assert is_authored_by_operator(SourceAuthor(connector, "ME@EXAMPLE.COM")) is True
    assert is_authored_by_operator(SourceAuthor(connector, "other@example.com")) is False


# ---- teams ----------------------------------------------------------------


def test_teams_id_matches_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Teams ids are opaque GUIDs — compared exactly (no case folding)."""
    monkeypatch.setenv("OPSHUB_CONNECTORS__TEAMS__OPERATOR_ID", "GUID-abc-123")
    assert is_authored_by_operator(SourceAuthor("teams", "GUID-abc-123")) is True
    # A case variant must NOT match — Graph ids are case-sensitive.
    assert is_authored_by_operator(SourceAuthor("teams", "guid-abc-123")) is False


# ---- slack ----------------------------------------------------------------


def _patch_slack_alias(monkeypatch: pytest.MonkeyPatch, alias: str = "acme") -> None:
    """Make the slack self-id cascade see a single configured workspace alias."""
    monkeypatch.setattr(
        "opshub.projections.slack_demand_digest._configured_workspace_aliases",
        lambda: [alias],
    )


def test_slack_matches_self_id_in_team(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Slack handle is compared against the self id of its own workspace."""
    _patch_slack_alias(monkeypatch)
    monkeypatch.setenv("OPSHUB_SLACK_SELF_USER_ID__ACME", "TACME:USELF")
    # ``external_id`` leads with the team_id (Phase 24-B re-key).
    author = SourceAuthor("slack", "USELF", external_id="TACME:C1:1700000000.0001")
    assert is_authored_by_operator(author) is True


def test_slack_other_user_is_not_operator(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_slack_alias(monkeypatch)
    monkeypatch.setenv("OPSHUB_SLACK_SELF_USER_ID__ACME", "TACME:USELF")
    author = SourceAuthor("slack", "UOTHER", external_id="TACME:C1:1700000000.0001")
    assert is_authored_by_operator(author) is False


def test_slack_team_mismatch_is_not_operator(monkeypatch: pytest.MonkeyPatch) -> None:
    """The self id only matches inside its own workspace, not another team's."""
    _patch_slack_alias(monkeypatch)
    monkeypatch.setenv("OPSHUB_SLACK_SELF_USER_ID__ACME", "TACME:USELF")
    # Same user id token but a different team — must not match (the self
    # id is workspace-scoped; ``USELF`` in team TOTHER is a different
    # person who happens to share a raw id string).
    author = SourceAuthor("slack", "USELF", external_id="TOTHER:C1:1700000000.0001")
    assert is_authored_by_operator(author) is False


def test_slack_missing_external_id_degrades_to_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an ``external_id`` there is no team_id to disambiguate → ``False``."""
    _patch_slack_alias(monkeypatch)
    monkeypatch.setenv("OPSHUB_SLACK_SELF_USER_ID__ACME", "TACME:USELF")
    assert is_authored_by_operator(SourceAuthor("slack", "USELF", external_id=None)) is False
    assert is_authored_by_operator(SourceAuthor("slack", "USELF", external_id="")) is False
