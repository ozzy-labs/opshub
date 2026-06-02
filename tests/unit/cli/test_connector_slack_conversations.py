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
from typing import Any, cast
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


# ----- type-bucket sort + --since filter (issue #374) -------------------


def _row_with_activity(
    base: SlackConversation,
    *,
    last_activity_ts: float,
) -> SlackConversation:
    """Clone ``base`` with an explicit ``last_activity_ts`` (frozen dataclass copy)."""
    from dataclasses import replace

    return replace(base, last_activity_ts=last_activity_ts)


def test_parse_since_relative_days() -> None:
    """``"7d"`` → ``now() - 7 days`` (within a small wall-clock tolerance)."""
    from datetime import UTC, datetime, timedelta

    from opshub.cli._slack_conversations import parse_since

    before = datetime.now(UTC) - timedelta(days=7)
    result = parse_since("7d")
    after = datetime.now(UTC) - timedelta(days=7)
    assert before <= result <= after
    assert result.tzinfo is not None


def test_parse_since_relative_weeks() -> None:
    """``"2w"`` → ``now() - 14 days``."""
    from datetime import UTC, datetime, timedelta

    from opshub.cli._slack_conversations import parse_since

    before = datetime.now(UTC) - timedelta(weeks=2)
    result = parse_since("2w")
    after = datetime.now(UTC) - timedelta(weeks=2)
    assert before <= result <= after


def test_parse_since_absolute_iso_date_defaults_to_utc() -> None:
    """``"2026-05-01"`` → ``datetime(2026, 5, 1, 0, 0, 0, UTC)``."""
    from datetime import UTC, datetime

    from opshub.cli._slack_conversations import parse_since

    assert parse_since("2026-05-01") == datetime(2026, 5, 1, tzinfo=UTC)


def test_parse_since_absolute_iso_with_timezone_normalised_to_utc() -> None:
    """A tz-aware ISO string is converted to UTC equivalent."""
    from datetime import UTC, datetime

    from opshub.cli._slack_conversations import parse_since

    # 2026-05-01T09:00:00+09:00 is 2026-05-01T00:00:00Z
    assert parse_since("2026-05-01T09:00:00+09:00") == datetime(2026, 5, 1, tzinfo=UTC)


def test_parse_since_zulu_suffix_accepted() -> None:
    """``"...Z"`` is rewritten to ``+00:00`` so :func:`datetime.fromisoformat` accepts it."""
    from datetime import UTC, datetime

    from opshub.cli._slack_conversations import parse_since

    assert parse_since("2026-05-01T00:00:00Z") == datetime(2026, 5, 1, tzinfo=UTC)


def test_parse_since_rejects_empty() -> None:
    """Empty / whitespace input → :class:`typer.BadParameter`."""
    import typer

    from opshub.cli._slack_conversations import parse_since

    with pytest.raises(typer.BadParameter):
        parse_since("")
    with pytest.raises(typer.BadParameter):
        parse_since("   ")


def test_parse_since_rejects_unknown_unit() -> None:
    """``"7x"`` / ``"5h"`` (hours unsupported) / ``"30m"`` → :class:`typer.BadParameter`.

    Hours / minutes / months / years are intentionally unsupported —
    the discovery command's filter granularity is days. Operators who
    need finer cuts can pass an absolute ISO datetime.
    """
    import typer

    from opshub.cli._slack_conversations import parse_since

    with pytest.raises(typer.BadParameter):
        parse_since("7x")
    with pytest.raises(typer.BadParameter):
        parse_since("5h")
    with pytest.raises(typer.BadParameter):
        parse_since("30m")
    with pytest.raises(typer.BadParameter):
        parse_since("foobar")


def test_parse_since_rejects_bare_integer() -> None:
    """``"30"`` (no unit suffix) → :class:`typer.BadParameter`.

    Without a unit the value is ambiguous (days? seconds since
    epoch?). Forcing the unit suffix removes the foot-gun.
    """
    import typer

    from opshub.cli._slack_conversations import parse_since

    with pytest.raises(typer.BadParameter):
        parse_since("30")


def test_conversations_since_flag_propagates(_slack_token_env: None) -> None:
    """``--since 7d`` forwards a tz-aware datetime to the iterator."""
    from datetime import UTC, datetime, timedelta

    record = _CallRecord()
    runner = CliRunner()
    before = datetime.now(UTC) - timedelta(days=7)
    with _patch_list_conversations([], record=record):
        result = runner.invoke(
            app,
            ["connector", "slack", "conversations", "--since", "7d"],
        )

    assert result.exit_code == 0, result.stdout + result.stderr
    parsed_since = record.kwargs["since"]
    assert parsed_since is not None
    assert parsed_since.tzinfo is not None
    after = datetime.now(UTC) - timedelta(days=7)
    assert before <= parsed_since <= after


def test_conversations_since_invalid_value_exits_2(_slack_token_env: None) -> None:
    """Unknown ``--since`` value → usage error (exit 2, no API call)."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["connector", "slack", "conversations", "--since", "garbage"],
    )

    assert result.exit_code == 2, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "garbage" in combined or "since" in combined


def test_conversations_default_sort_groups_by_type(_slack_token_env: None) -> None:
    """Mixed-type rows sort into ``public → private → mpim → im`` buckets.

    Within each bucket the no-``--since`` sort is ``display_name`` asc
    (case-insensitive), so the visible order is fully deterministic.
    """
    rows = [
        _im_row("D1", peer="zelda"),
        _mpim_row("G-mpim-A", participants=("alice", "bob")),
        _private_row("G-priv-A", name="leadership"),
        _public_row("C-pub-B", name="random"),
        _public_row("C-pub-A", name="general"),
        _im_row("D2", peer="alice"),
        _mpim_row("G-mpim-B", participants=("dave", "eve")),
    ]
    record = _CallRecord()
    runner = CliRunner()
    with _patch_list_conversations(rows, record=record):
        result = runner.invoke(
            app,
            ["connector", "slack", "conversations", "--format", "toml"],
        )

    assert result.exit_code == 0, result.stdout + result.stderr
    # Extract the id order from the TOML emission line-by-line. The
    # ids on consecutive ``  "X",`` lines reflect post-sort ordering.
    id_order = [line.split('"')[1] for line in result.stdout.splitlines() if line.startswith('  "')]
    # Expected: public (alphabetical by name) → private → mpim (by
    # display_name = "alice, bob" < "dave, eve") → im (alice < zelda).
    assert id_order == [
        "C-pub-A",
        "C-pub-B",
        "G-priv-A",
        "G-mpim-A",
        "G-mpim-B",
        "D2",
        "D1",
    ]


def test_conversations_since_sort_orders_by_activity_desc(
    _slack_token_env: None,
) -> None:
    """With ``--since`` set, rows within each bucket sort by ``last_activity_ts`` desc."""
    rows = [
        _row_with_activity(_public_row("C-old", name="alpha"), last_activity_ts=1_000_000.0),
        _row_with_activity(_public_row("C-new", name="zulu"), last_activity_ts=2_000_000.0),
        _row_with_activity(_im_row("D1", peer="alice"), last_activity_ts=1_500_000.0),
    ]
    runner = CliRunner()
    with _patch_list_conversations(rows, record=_CallRecord()):
        result = runner.invoke(
            app,
            ["connector", "slack", "conversations", "--since", "30d", "--format", "toml"],
        )

    assert result.exit_code == 0, result.stdout + result.stderr
    id_order = [line.split('"')[1] for line in result.stdout.splitlines() if line.startswith('  "')]
    # Within ``public`` bucket: ``C-new`` (ts=2M) before ``C-old`` (ts=1M).
    # Then ``im`` bucket trails.
    assert id_order == ["C-new", "C-old", "D1"]


def test_conversations_since_table_shows_last_activity_column(
    _slack_token_env: None,
) -> None:
    """``--since`` enables the ``LAST_ACTIVITY`` column with UTC YYYY-MM-DD values."""
    rows = [
        _row_with_activity(
            _public_row("C1", name="general"),
            last_activity_ts=1_717_200_000.0,  # 2024-06-01 04:00:00 UTC
        ),
    ]
    runner = CliRunner()
    with _patch_list_conversations(rows, record=_CallRecord()):
        result = runner.invoke(
            app,
            ["connector", "slack", "conversations", "--since", "30d"],
        )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "LAST_ACTIVITY" in result.stdout
    assert "2024-06-01" in result.stdout


def test_conversations_no_since_table_omits_last_activity_column(
    _slack_token_env: None,
) -> None:
    """Without ``--since``, ``LAST_ACTIVITY`` is hidden so #366's layout is preserved."""
    rows = [_public_row("C1")]
    runner = CliRunner()
    with _patch_list_conversations(rows, record=_CallRecord()):
        result = runner.invoke(app, ["connector", "slack", "conversations"])

    assert result.exit_code == 0
    assert "LAST_ACTIVITY" not in result.stdout


def test_conversations_since_json_includes_last_activity_ts(
    _slack_token_env: None,
) -> None:
    """``--since`` + ``--format json`` → ``last_activity_ts`` populated in payload."""
    import json as _json

    rows = [_row_with_activity(_public_row("C1", name="general"), last_activity_ts=1_717_200_000.0)]
    runner = CliRunner()
    with _patch_list_conversations(rows, record=_CallRecord()):
        result = runner.invoke(
            app,
            [
                "connector",
                "slack",
                "conversations",
                "--since",
                "30d",
                "--format",
                "json",
            ],
        )

    assert result.exit_code == 0
    payload = _json.loads(result.stdout)
    assert payload[0]["last_activity_ts"] == 1_717_200_000.0


def test_conversations_no_since_json_omits_last_activity_ts(
    _slack_token_env: None,
) -> None:
    """Without ``--since``, ``last_activity_ts`` is omitted from JSON entirely.

    Keeps #366's payload contract intact for operators who never opt
    into activity probing — no meaningless nulls in their pipeline.
    """
    import json as _json

    rows = [_public_row("C1")]
    runner = CliRunner()
    with _patch_list_conversations(rows, record=_CallRecord()):
        result = runner.invoke(
            app,
            ["connector", "slack", "conversations", "--format", "json"],
        )

    assert result.exit_code == 0
    payload = _json.loads(result.stdout)
    assert "last_activity_ts" not in payload[0]


def test_conversations_since_toml_comment_includes_activity_date(
    _slack_token_env: None,
) -> None:
    """``--since`` + ``--format toml`` → comment includes ``last YYYY-MM-DD``."""
    rows = [_row_with_activity(_public_row("C1", name="general"), last_activity_ts=1_717_200_000.0)]
    runner = CliRunner()
    with _patch_list_conversations(rows, record=_CallRecord()):
        result = runner.invoke(
            app,
            [
                "connector",
                "slack",
                "conversations",
                "--since",
                "30d",
                "--format",
                "toml",
            ],
        )

    assert result.exit_code == 0
    assert "last 2024-06-01" in result.stdout


def test_conversations_warnings_from_iterator_surface_on_stderr_in_order(
    _slack_token_env: None,
) -> None:
    """Iterator-populated warnings flush to stderr in append order after the spinner closes.

    Pin the CLI boundary: the driver passes a ``warnings`` list to
    ``list_conversations``, then echoes each entry on stderr in the
    order the iterator appended them. Regression in either direction
    (driver swallowing warnings, or re-ordering them) would miss the
    per-type ``missing_scope`` UX from #374.
    """

    def _fake_list(auth: Any, **kwargs: Any) -> Iterator[SlackConversation]:
        bucket_obj = kwargs.get("warnings")
        if isinstance(bucket_obj, list):
            bucket = cast("list[str]", bucket_obj)
            bucket.append("warning: skipping public conversations: missing_scope ...")
            bucket.append("warning: skipping mpim conversations: missing_scope ...")
        del auth
        return iter(())

    runner = CliRunner()
    with patch(
        "opshub.connectors.slack.conversations.list_conversations",
        side_effect=_fake_list,
    ):
        result = runner.invoke(
            app,
            ["connector", "slack", "conversations", "--since", "7d"],
        )

    assert result.exit_code == 0, result.stdout + result.stderr
    stderr = result.stderr
    public_idx = stderr.find("skipping public conversations")
    mpim_idx = stderr.find("skipping mpim conversations")
    assert public_idx != -1, stderr
    assert mpim_idx != -1, stderr
    assert public_idx < mpim_idx, "warnings must surface in iterator-append order"


def test_sort_rows_activity_mode_pushes_missing_ts_to_bucket_tail() -> None:
    """``_sort_rows(by_activity=True)`` parks ``last_activity_ts is None`` at bucket end.

    The docstring marks the ``None`` branch as defensive (``--since``
    should always populate the ts), but the branch is reachable if a
    thin proxy returns a non-numeric ts that ``_fetch_last_activity_ts``
    cannot parse — we keep the row, with ``None`` ts. The CLI sort
    must place such rows last within their type bucket so the
    presented order does not falsely promote a "no signal" row above
    a row with a real recent ts.
    """
    from opshub.cli._slack_conversations import _sort_rows  # pyright: ignore[reportPrivateUsage]

    rows = [
        _row_with_activity(_public_row("C-mid", name="charlie"), last_activity_ts=1_500_000.0),
        # ``last_activity_ts=None`` via the dataclass default — public bucket fallback.
        _public_row("C-none", name="bravo"),
        _row_with_activity(_public_row("C-new", name="alpha"), last_activity_ts=2_000_000.0),
    ]

    result = _sort_rows(rows, by_activity=True)

    assert [r.id for r in result] == ["C-new", "C-mid", "C-none"]


# ----- audit followup: defensive arm + edge case coverage ---------------


def test_parse_since_rejects_overflowingly_large_amount() -> None:
    """``parse_since('99999999999d')`` → :class:`typer.BadParameter` (not raw OverflowError).

    The relative grammar accepts ``\\d+`` without an upper bound, so a
    typo (or paste from another tool) can produce an integer that
    overflows :class:`datetime.timedelta`. The guard in ``parse_since``
    translates the overflow into the documented exit-code-2
    ``typer.BadParameter`` contract; this test pins that translation
    so a future refactor of the relative-form code cannot accidentally
    drop the guard and surface an unfriendly traceback to operators.
    """
    import typer

    from opshub.cli._slack_conversations import parse_since

    with pytest.raises(typer.BadParameter) as excinfo:
        parse_since("99999999999d")
    assert "too far in the past" in str(excinfo.value) or "ISO date" in str(excinfo.value)


def test_conversations_since_json_drops_only_none_rows_in_mixed_payload(
    _slack_token_env: None,
) -> None:
    """JSON renderer drops ``last_activity_ts`` per-row when ``None``, keeps it when populated.

    Earlier tests pin the all-populated and all-``None`` cases in
    isolation. This mixed payload — one populated row + one defensive
    ``None`` row in the same emission — exercises the per-row pop
    logic so a regression that strips the key from every row (or
    none) is caught.
    """
    import json as _json

    rows = [
        _row_with_activity(_public_row("C-pop", name="alpha"), last_activity_ts=1_717_200_000.0),
        _public_row("C-none", name="bravo"),  # dataclass default → last_activity_ts=None
    ]
    runner = CliRunner()
    with _patch_list_conversations(rows, record=_CallRecord()):
        result = runner.invoke(
            app,
            [
                "connector",
                "slack",
                "conversations",
                "--since",
                "30d",
                "--format",
                "json",
            ],
        )

    assert result.exit_code == 0
    payload = _json.loads(result.stdout)
    assert len(payload) == 2
    # Sort order is activity-desc; the populated row comes first.
    populated, defensive = payload[0], payload[1]
    assert populated["id"] == "C-pop"
    assert populated["last_activity_ts"] == 1_717_200_000.0
    assert defensive["id"] == "C-none"
    assert "last_activity_ts" not in defensive


def test_sort_rows_unknown_type_falls_to_tail_bucket() -> None:
    """``_sort_rows`` parks rows of an unrecognised ``type`` after all known buckets.

    Defensive arm: a future code path that adds a new conversation
    type to Slack but forgets to extend :data:`_TYPE_BUCKET_ORDER`
    would land its rows at the bottom rather than crash the sort
    (the type literal is enforced at the dataclass level so this
    requires a typing-bypassed payload, e.g. a thin proxy or a
    forward-compat schema). The Python runtime does not enforce
    :class:`typing.Literal`, so ``type='unknown'`` constructs fine.
    """
    from typing import cast

    from opshub.cli._slack_conversations import _sort_rows  # pyright: ignore[reportPrivateUsage]
    from opshub.connectors.slack.conversations import ConversationType, SlackConversation

    unknown = SlackConversation(
        id="X-future",
        type=cast(ConversationType, "future_kind"),  # Literal-bypass for the defensive arm
        name=None,
        display_name="future-kind-conversation",
        is_private=False,
        is_archived=False,
        purpose="",
        participants=(),
    )

    rows = [
        unknown,
        _public_row("C-pub", name="alpha"),
        _im_row("D-dm", peer="alice"),
    ]

    result = _sort_rows(rows, by_activity=False)

    # Known buckets (public → im) come first, unknown bucket lands last.
    assert [r.id for r in result] == ["C-pub", "D-dm", "X-future"]
