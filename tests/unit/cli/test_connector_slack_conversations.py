"""Tests for ``opshub connector slack conversations`` (#366).

The CLI command wraps
:func:`opshub.connectors.slack.conversations.list_conversations` and
renders the result in one of three formats — ``table`` (default),
``toml``, or ``json``. Behaviour worth pinning here:

1. Happy path through each output format with the DM / MPIM type
   surfacing (TYPE / NAME-PARTICIPANTS / ARCHIVED columns).
2. Flag propagation: ``--filter``, ``--limit``, ``--types``,
   ``--include-archived``, ``--all`` all reach the iterator's
   keyword arguments unchanged.
3. ``--types`` parsing: comma-separated tokens, validation errors for
   unknown tokens / empty input.
4. ``--progress`` / ``--no-progress`` precedence is honoured via the
   shared :mod:`opshub.cli._progress` plumbing.
5. Empty result: stderr hint surfaces for ``table`` / ``toml``; JSON
   still emits ``[]`` on stdout.
6. Auth / runtime / usage error paths map to the documented exit
   codes (1 / 1 / 2 respectively).
7. ``--help`` keeps ``slack_sdk`` off the cold-start path.
8. MPIM participant truncation: 4+ participants render as
   ``a, b, c +N``.

The Slack SDK extras may not be installed in every CI environment,
so the file-level ``pytest.importorskip`` gates the whole module.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

pytest.importorskip(
    "slack_sdk",
    reason="Slack CLI tests require the 'connectors-slack' extras",
)

from opshub.cli.app import app
from opshub.connectors.slack.conversations import SlackConversation
from opshub.core.errors import ConnectorFailedError

# ----- shared fixtures ---------------------------------------------------


def _public_row(
    channel_id: str = "C01234567",
    *,
    name: str = "general",
    is_archived: bool = False,
    purpose: str = "Company-wide announcements",
) -> SlackConversation:
    """Build a public-channel :class:`SlackConversation` fixture."""
    return SlackConversation(
        id=channel_id,
        type="public",
        name=name,
        display_name=name,
        is_private=False,
        is_archived=is_archived,
        purpose=purpose,
        participants=(),
    )


def _private_row(
    channel_id: str = "G03456789",
    *,
    name: str = "leadership",
    purpose: str = "",
) -> SlackConversation:
    return SlackConversation(
        id=channel_id,
        type="private",
        name=name,
        display_name=name,
        is_private=True,
        is_archived=False,
        purpose=purpose,
        participants=(),
    )


def _im_row(
    channel_id: str = "D04567890",
    *,
    peer: str = "Alice Johnson",
) -> SlackConversation:
    return SlackConversation(
        id=channel_id,
        type="im",
        name=None,
        display_name=peer,
        is_private=True,
        is_archived=False,
        purpose="",
        participants=(),
    )


def _mpim_row(
    channel_id: str = "G05678901",
    *,
    participants: tuple[str, ...] = ("Bob", "Carol", "Dave"),
) -> SlackConversation:
    return SlackConversation(
        id=channel_id,
        type="mpim",
        name=None,
        display_name=", ".join(participants),
        is_private=True,
        is_archived=False,
        purpose="",
        participants=participants,
    )


class _CallRecord:
    """Capture of one ``list_conversations`` invocation."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}
        self.auth_token: str | None = None


def _patch_list_conversations(
    rows: list[SlackConversation],
    *,
    record: _CallRecord,
    raises: Exception | None = None,
) -> AbstractContextManager[MagicMock]:
    """Patch ``list_conversations`` to return ``rows`` and capture kwargs."""

    def _fake_list(auth: Any, **kwargs: Any) -> Iterator[SlackConversation]:
        record.auth_token = getattr(auth, "token", None)
        record.kwargs = dict(kwargs)
        if raises is not None:
            raise raises
        yield from rows

    return patch(
        "opshub.connectors.slack.conversations.list_conversations",
        side_effect=_fake_list,
    )


@pytest.fixture
def _slack_token_env(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    """Plant a Slack OAuth token in the env so :class:`SlackAuth` constructs."""
    monkeypatch.setenv("OPSHUB_CONNECTOR_SLACK_TOKEN", "xoxb-test")


# ----- happy path: each output format -----------------------------------


def test_conversations_default_format_is_table(_slack_token_env: None) -> None:
    """No ``--format`` flag → table output with the new TYPE / NAME-PARTICIPANTS columns."""
    rows = [
        _public_row("C01234567", name="general", purpose="Company-wide"),
        _im_row("D04567890", peer="Alice Johnson"),
    ]
    record = _CallRecord()
    runner = CliRunner()
    with _patch_list_conversations(rows, record=record):
        result = runner.invoke(app, ["connector", "slack", "conversations"])

    assert result.exit_code == 0, result.stdout + result.stderr
    out = result.stdout
    assert "ID" in out
    assert "TYPE" in out
    assert "NAME / PARTICIPANTS" in out
    assert "ARCHIVED" in out
    assert "PURPOSE" in out
    assert "C01234567" in out
    assert "general" in out
    assert "D04567890" in out
    assert "Alice Johnson" in out
    # Default types tuple covers all four kinds.
    assert record.kwargs["types"] == ("public", "private", "im", "mpim")
    assert record.kwargs["all"] is False
    assert record.auth_token == "xoxb-test"


def test_conversations_table_marks_dm_archive_column_as_dash(
    _slack_token_env: None,
) -> None:
    """DM / MPIM rows render ``-`` in the ARCHIVED column (DMs never archive)."""
    rows = [_im_row("D1", peer="Alice")]
    runner = CliRunner()
    with _patch_list_conversations(rows, record=_CallRecord()):
        result = runner.invoke(app, ["connector", "slack", "conversations"])

    assert result.exit_code == 0
    # Header row contains "ARCHIVED"; the value row must contain "-"
    # somewhere after the type column. We assert the substring is
    # present (full layout assertion is too fragile to fixed widths).
    assert " - " in result.stdout


def test_conversations_table_renders_mpim_participants_with_cap(
    _slack_token_env: None,
) -> None:
    """MPIM with 4+ participants renders ``a, b, c +N``.

    The display cap (3 names + remainder) keeps the column width
    bounded on group DMs with many participants. The JSON output
    keeps the full participant list for downstream consumers.
    """
    rows = [
        _mpim_row("G1", participants=("alice", "bob", "carol", "dave", "ellen")),
    ]
    runner = CliRunner()
    with _patch_list_conversations(rows, record=_CallRecord()):
        result = runner.invoke(app, ["connector", "slack", "conversations"])

    assert result.exit_code == 0
    assert "alice, bob, carol +2" in result.stdout


def test_conversations_format_toml_emits_paste_ready_snippet(
    _slack_token_env: None,
) -> None:
    """``--format toml`` emits a ``channels = [...]`` snippet with type comments."""
    rows = [
        _public_row("C01234567", name="general"),
        _private_row("G03456789", name="leadership"),
        _im_row("D04567890", peer="Alice"),
    ]
    runner = CliRunner()
    with _patch_list_conversations(rows, record=_CallRecord()):
        result = runner.invoke(
            app,
            ["connector", "slack", "conversations", "--format", "toml"],
        )

    assert result.exit_code == 0, result.stdout + result.stderr
    out = result.stdout
    assert "# Slack conversations (3)" in out
    assert "channels = [" in out
    assert '"C01234567"' in out
    assert "general (public)" in out
    assert "leadership (private)" in out
    assert '"D04567890"' in out
    # DM rows render the resolved peer name in the comment.
    assert "Alice (im)" in out
    assert out.rstrip().endswith("]")


def test_conversations_format_json_emits_array_of_dataclass_dicts(
    _slack_token_env: None,
) -> None:
    """``--format json`` emits a JSON array including type / display_name / participants."""
    import json as _json

    rows = [
        _public_row("C01234567", name="general", purpose="Company-wide"),
        _mpim_row("G05", participants=("alice", "bob")),
    ]
    runner = CliRunner()
    with _patch_list_conversations(rows, record=_CallRecord()):
        result = runner.invoke(
            app,
            ["connector", "slack", "conversations", "--format", "json"],
        )

    assert result.exit_code == 0, result.stdout + result.stderr
    payload = _json.loads(result.stdout)
    assert payload == [
        {
            "id": "C01234567",
            "type": "public",
            "name": "general",
            "display_name": "general",
            "is_private": False,
            "is_archived": False,
            "purpose": "Company-wide",
            "participants": [],
        },
        {
            "id": "G05",
            "type": "mpim",
            "name": None,
            "display_name": "alice, bob",
            "is_private": True,
            "is_archived": False,
            "purpose": "",
            "participants": ["alice", "bob"],
        },
    ]


# ----- flag propagation -------------------------------------------------


def test_conversations_filter_flag_propagates(_slack_token_env: None) -> None:
    """``--filter`` forwards verbatim."""
    record = _CallRecord()
    runner = CliRunner()
    with _patch_list_conversations([], record=record):
        result = runner.invoke(
            app,
            ["connector", "slack", "conversations", "--filter", "Backend"],
        )

    assert result.exit_code == 0
    assert record.kwargs["filter_substring"] == "Backend"


def test_conversations_filter_empty_string_normalised_to_none(
    _slack_token_env: None,
) -> None:
    """``--filter ""`` collapses to ``None`` upstream."""
    record = _CallRecord()
    runner = CliRunner()
    with _patch_list_conversations([_public_row()], record=record):
        result = runner.invoke(
            app,
            ["connector", "slack", "conversations", "--filter", ""],
        )

    assert result.exit_code == 0
    assert record.kwargs["filter_substring"] is None


def test_conversations_limit_flag_propagates(_slack_token_env: None) -> None:
    """``--limit N`` forwards as the iterator's ``limit`` kwarg."""
    record = _CallRecord()
    runner = CliRunner()
    with _patch_list_conversations([_public_row()], record=record):
        result = runner.invoke(
            app,
            ["connector", "slack", "conversations", "--limit", "5"],
        )

    assert result.exit_code == 0
    assert record.kwargs["limit"] == 5


def test_conversations_types_flag_parses_subset(_slack_token_env: None) -> None:
    """``--types public,im`` → ``types=("public", "im")`` upstream."""
    record = _CallRecord()
    runner = CliRunner()
    with _patch_list_conversations([], record=record):
        result = runner.invoke(
            app,
            ["connector", "slack", "conversations", "--types", "public,im"],
        )

    assert result.exit_code == 0
    assert record.kwargs["types"] == ("public", "im")


def test_conversations_types_flag_deduplicates(_slack_token_env: None) -> None:
    """``--types public,public,im`` → deduplicated to ``("public", "im")``."""
    record = _CallRecord()
    runner = CliRunner()
    with _patch_list_conversations([], record=record):
        result = runner.invoke(
            app,
            [
                "connector",
                "slack",
                "conversations",
                "--types",
                "public,public,im",
            ],
        )

    assert result.exit_code == 0
    assert record.kwargs["types"] == ("public", "im")


def test_conversations_types_flag_rejects_unknown(_slack_token_env: None) -> None:
    """Unknown ``--types`` value → usage error (exit 2) listing valid options."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["connector", "slack", "conversations", "--types", "public,bot"],
    )

    assert result.exit_code == 2, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "bot" in combined
    # Diagnostic lists at least one valid token so the operator can self-correct.
    assert "public" in combined


def test_conversations_types_flag_rejects_empty(_slack_token_env: None) -> None:
    """Empty ``--types`` → usage error.

    Without this guard, an empty ``--types`` would silently collapse
    the API request's ``types=`` parameter and Slack would default
    behaviour (returning public channels only), surprising the
    operator.
    """
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["connector", "slack", "conversations", "--types", ""],
    )

    assert result.exit_code == 2, result.stdout + result.stderr


def test_conversations_types_flag_rejects_whitespace_and_comma_only(
    _slack_token_env: None,
) -> None:
    """``--types " , , "`` → usage error.

    Whitespace-only chunks individually skip (via the parser's per-
    chunk ``strip()``) so a payload that is *only* whitespace + commas
    collapses to zero parsed types. The parser must catch this case
    via the post-loop ``if not parts`` guard rather than silently
    sending an empty ``types`` parameter to the Slack API.
    """
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["connector", "slack", "conversations", "--types", " , , "],
    )

    assert result.exit_code == 2, result.stdout + result.stderr


def test_conversations_all_flag_propagates(_slack_token_env: None) -> None:
    """``--all`` → ``all=True`` upstream (workspace-wide endpoint)."""
    record = _CallRecord()
    runner = CliRunner()
    with _patch_list_conversations([], record=record):
        result = runner.invoke(
            app,
            ["connector", "slack", "conversations", "--all"],
        )

    assert result.exit_code == 0
    assert record.kwargs["all"] is True


def test_conversations_include_archived_flips_iterator_kwarg(
    _slack_token_env: None,
) -> None:
    """``--include-archived`` forwards as ``include_archived=True``."""
    record = _CallRecord()
    runner = CliRunner()
    with _patch_list_conversations([_public_row(is_archived=True)], record=record):
        result = runner.invoke(
            app,
            ["connector", "slack", "conversations", "--include-archived"],
        )

    assert result.exit_code == 0
    assert record.kwargs["include_archived"] is True
    # Archived channel renders ``yes`` in the ARCHIVED column.
    assert "yes" in result.stdout


# ----- progress flag ----------------------------------------------------


def test_conversations_no_progress_flag_disables_reporter(
    _slack_token_env: None,
) -> None:
    """``--no-progress`` → the iterator receives a no-op reporter."""
    record = _CallRecord()
    runner = CliRunner()
    with _patch_list_conversations([_public_row()], record=record):
        result = runner.invoke(
            app,
            ["--no-progress", "connector", "slack", "conversations"],
        )

    assert result.exit_code == 0, result.stdout + result.stderr
    reporter = record.kwargs["reporter"]
    # The no-op reporter has the same advance / update surface but
    # never affects stderr — driving it should not raise even in a
    # captured-output context.
    reporter.advance(1)
    reporter.update(total=10, description="x")


def test_conversations_progress_flag_forces_reporter(
    _slack_token_env: None,
) -> None:
    """``--progress`` → forces the rich reporter even in captured-stderr tests."""
    record = _CallRecord()
    runner = CliRunner()
    with _patch_list_conversations([_public_row()], record=record):
        result = runner.invoke(
            app,
            ["--progress", "connector", "slack", "conversations"],
        )

    assert result.exit_code == 0, result.stdout + result.stderr
    # Even with progress forced on, the reporter accepts advance().
    record.kwargs["reporter"].advance(1)


# ----- empty result -----------------------------------------------------


def test_conversations_empty_table_emits_header_and_stderr_hint(
    _slack_token_env: None,
) -> None:
    """No matches → table still shows the header; hint goes to stderr."""
    runner = CliRunner()
    with _patch_list_conversations([], record=_CallRecord()):
        result = runner.invoke(app, ["connector", "slack", "conversations"])

    assert result.exit_code == 0
    assert "ID" in result.stdout
    assert "no conversations matched" in result.stderr


def test_conversations_empty_filter_hint_includes_filter_string(
    _slack_token_env: None,
) -> None:
    """The empty-state stderr hint quotes the filter value for self-correction."""
    runner = CliRunner()
    with _patch_list_conversations([], record=_CallRecord()):
        result = runner.invoke(
            app,
            ["connector", "slack", "conversations", "--filter", "nope"],
        )

    assert result.exit_code == 0
    assert "no conversations matched" in result.stderr
    assert "nope" in result.stderr


def test_conversations_empty_toml_emits_empty_array(_slack_token_env: None) -> None:
    """No matches with ``--format toml`` still emits ``channels = []``."""
    runner = CliRunner()
    with _patch_list_conversations([], record=_CallRecord()):
        result = runner.invoke(
            app,
            ["connector", "slack", "conversations", "--format", "toml"],
        )

    assert result.exit_code == 0
    assert "# Slack conversations (0)" in result.stdout
    assert "channels = []" in result.stdout
    assert "no conversations matched" in result.stderr


def test_conversations_empty_json_emits_empty_array_no_stderr_hint(
    _slack_token_env: None,
) -> None:
    """``--format json`` empty result → ``[]`` on stdout, no stderr hint."""
    import json as _json

    runner = CliRunner()
    with _patch_list_conversations([], record=_CallRecord()):
        result = runner.invoke(
            app,
            ["connector", "slack", "conversations", "--format", "json"],
        )

    assert result.exit_code == 0
    assert _json.loads(result.stdout) == []
    assert "no conversations matched" not in result.stderr


# ----- error paths ------------------------------------------------------


def test_conversations_config_error_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing token → ``ConfigError`` → exit 1 + sanitised stderr."""
    monkeypatch.delenv("OPSHUB_CONNECTOR_SLACK_TOKEN", raising=False)

    def _no_secret(_key: str) -> str | None:
        return None

    monkeypatch.setattr("opshub.core.secrets.get_secret", _no_secret)

    runner = CliRunner()
    result = runner.invoke(app, ["connector", "slack", "conversations"])

    assert result.exit_code == 1, result.stdout + result.stderr
    assert "not configured" in result.stderr.lower() or "Error:" in result.stderr


def test_conversations_connector_failed_error_exits_1_with_scope(
    _slack_token_env: None,
) -> None:
    """``missing_scope`` from the iterator → exit 1 + scope name on stderr."""
    failure = ConnectorFailedError(
        "Slack users.conversations failed: missing_scope "
        "(needed: 'im:read'). See ADR-0018 §Decision (7) ..."
    )
    runner = CliRunner()
    with _patch_list_conversations([], record=_CallRecord(), raises=failure):
        result = runner.invoke(app, ["connector", "slack", "conversations"])

    assert result.exit_code == 1, result.stdout + result.stderr
    assert "missing_scope" in result.stderr
    assert "im:read" in result.stderr
    assert "xoxb-test" not in result.stderr


def test_conversations_invalid_format_exits_2(_slack_token_env: None) -> None:
    """Unknown ``--format`` value → usage error (exit 2) listing valid options."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["connector", "slack", "conversations", "--format", "yaml"],
    )

    assert result.exit_code == 2, result.stdout + result.stderr
    assert "yaml" in result.stderr
    assert "table" in result.stderr
    assert "toml" in result.stderr
    assert "json" in result.stderr


# ----- cold-start guard -------------------------------------------------


def test_conversations_help_does_not_import_slack_sdk() -> None:
    """``connector slack conversations --help`` keeps ``slack_sdk`` off the cold-start path."""
    import sys

    saved = {
        name: sys.modules.pop(name) for name in list(sys.modules) if name.startswith("slack_sdk")
    }
    try:
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["connector", "slack", "conversations", "--help"],
        )
        assert result.exit_code == 0, result.stdout
        assert not any(
            name == "slack_sdk" or name.startswith("slack_sdk.") for name in sys.modules
        ), "slack_sdk leaked onto cold-start path via 'connector slack conversations --help'"
    finally:
        sys.modules.update(saved)


# ----- format-specific rendering edge cases ------------------------------


def test_conversations_table_truncates_long_purpose(_slack_token_env: None) -> None:
    """The PURPOSE column truncates at the documented cap; JSON keeps the full string."""
    long_purpose = "X" * 200
    rows = [_public_row(purpose=long_purpose)]
    runner = CliRunner()
    with _patch_list_conversations(rows, record=_CallRecord()):
        table_result = runner.invoke(app, ["connector", "slack", "conversations"])
        json_result = runner.invoke(
            app, ["connector", "slack", "conversations", "--format", "json"]
        )

    assert table_result.exit_code == 0
    assert "…" in table_result.stdout
    assert long_purpose in json_result.stdout
