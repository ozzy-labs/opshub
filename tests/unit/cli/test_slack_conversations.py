"""Tests for ``opshub slack conversations`` (#366; Phase 17-B path update).

Carried over from the legacy ``tests/unit/cli/test_connector_slack_conversations.py``
with the legacy 3-segment ``["connector", "slack", "conversations", ...]``
invocations rewritten to the new per-noun 2-segment form
``["slack", "conversations", ...]``. All behavioural invariants
(output formats / sort order / flag propagation / cold-start guard)
are unchanged from #366 / #374.
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
    rows = [
        _public_row("C01234567", name="general", purpose="Company-wide"),
        _im_row("D04567890", peer="Alice Johnson"),
    ]
    record = _CallRecord()
    runner = CliRunner()
    with _patch_list_conversations(rows, record=record):
        result = runner.invoke(app, ["slack", "conversations"])

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
    assert record.kwargs["types"] == ("public", "private", "im", "mpim")
    assert record.kwargs["all"] is False
    assert record.auth_token == "xoxb-test"


def test_conversations_table_marks_dm_archive_column_as_dash(
    _slack_token_env: None,
) -> None:
    rows = [_im_row("D1", peer="Alice")]
    runner = CliRunner()
    with _patch_list_conversations(rows, record=_CallRecord()):
        result = runner.invoke(app, ["slack", "conversations"])

    assert result.exit_code == 0
    assert " - " in result.stdout


def test_conversations_table_renders_mpim_participants_with_cap(
    _slack_token_env: None,
) -> None:
    rows = [
        _mpim_row("G1", participants=("alice", "bob", "carol", "dave", "ellen")),
    ]
    runner = CliRunner()
    with _patch_list_conversations(rows, record=_CallRecord()):
        result = runner.invoke(app, ["slack", "conversations"])

    assert result.exit_code == 0
    assert "alice, bob, carol +2" in result.stdout


def test_conversations_format_toml_emits_paste_ready_snippet(
    _slack_token_env: None,
) -> None:
    rows = [
        _public_row("C01234567", name="general"),
        _private_row("G03456789", name="leadership"),
        _im_row("D04567890", peer="Alice"),
    ]
    runner = CliRunner()
    with _patch_list_conversations(rows, record=_CallRecord()):
        result = runner.invoke(
            app,
            ["slack", "conversations", "--format", "toml"],
        )

    assert result.exit_code == 0, result.stdout + result.stderr
    out = result.stdout
    assert "# Slack conversations (3)" in out
    assert "channels = [" in out
    assert '"C01234567"' in out
    assert "general (public)" in out
    assert "leadership (private)" in out
    assert '"D04567890"' in out
    assert "Alice (im)" in out
    assert out.rstrip().endswith("]")


def test_conversations_format_json_emits_array_of_dataclass_dicts(
    _slack_token_env: None,
) -> None:
    import json as _json

    rows = [
        _public_row("C01234567", name="general", purpose="Company-wide"),
        _mpim_row("G05", participants=("alice", "bob")),
    ]
    runner = CliRunner()
    with _patch_list_conversations(rows, record=_CallRecord()):
        result = runner.invoke(
            app,
            ["slack", "conversations", "--format", "json"],
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
    record = _CallRecord()
    runner = CliRunner()
    with _patch_list_conversations([], record=record):
        result = runner.invoke(
            app,
            ["slack", "conversations", "--filter", "Backend"],
        )

    assert result.exit_code == 0
    assert record.kwargs["filter_substring"] == "Backend"


def test_conversations_filter_empty_string_normalised_to_none(
    _slack_token_env: None,
) -> None:
    record = _CallRecord()
    runner = CliRunner()
    with _patch_list_conversations([_public_row()], record=record):
        result = runner.invoke(
            app,
            ["slack", "conversations", "--filter", ""],
        )

    assert result.exit_code == 0
    assert record.kwargs["filter_substring"] is None


def test_conversations_limit_flag_propagates(_slack_token_env: None) -> None:
    record = _CallRecord()
    runner = CliRunner()
    with _patch_list_conversations([_public_row()], record=record):
        result = runner.invoke(
            app,
            ["slack", "conversations", "--limit", "5"],
        )

    assert result.exit_code == 0
    assert record.kwargs["limit"] == 5


def test_conversations_types_flag_parses_subset(_slack_token_env: None) -> None:
    record = _CallRecord()
    runner = CliRunner()
    with _patch_list_conversations([], record=record):
        result = runner.invoke(
            app,
            ["slack", "conversations", "--types", "public,im"],
        )

    assert result.exit_code == 0
    assert record.kwargs["types"] == ("public", "im")


def test_conversations_types_flag_deduplicates(_slack_token_env: None) -> None:
    record = _CallRecord()
    runner = CliRunner()
    with _patch_list_conversations([], record=record):
        result = runner.invoke(
            app,
            [
                "slack",
                "conversations",
                "--types",
                "public,public,im",
            ],
        )

    assert result.exit_code == 0
    assert record.kwargs["types"] == ("public", "im")


def test_conversations_types_flag_rejects_unknown(_slack_token_env: None) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["slack", "conversations", "--types", "public,bot"],
    )

    assert result.exit_code == 2, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "bot" in combined
    assert "public" in combined


def test_conversations_types_flag_rejects_empty(_slack_token_env: None) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["slack", "conversations", "--types", ""],
    )

    assert result.exit_code == 2, result.stdout + result.stderr


def test_conversations_types_flag_rejects_whitespace_and_comma_only(
    _slack_token_env: None,
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["slack", "conversations", "--types", " , , "],
    )

    assert result.exit_code == 2, result.stdout + result.stderr


def test_conversations_all_flag_propagates(_slack_token_env: None) -> None:
    record = _CallRecord()
    runner = CliRunner()
    with _patch_list_conversations([], record=record):
        result = runner.invoke(
            app,
            ["slack", "conversations", "--all"],
        )

    assert result.exit_code == 0
    assert record.kwargs["all"] is True


def test_conversations_include_archived_flips_iterator_kwarg(
    _slack_token_env: None,
) -> None:
    record = _CallRecord()
    runner = CliRunner()
    with _patch_list_conversations([_public_row(is_archived=True)], record=record):
        result = runner.invoke(
            app,
            ["slack", "conversations", "--include-archived"],
        )

    assert result.exit_code == 0
    assert record.kwargs["include_archived"] is True
    assert "yes" in result.stdout


# ----- progress flag ----------------------------------------------------


def test_conversations_no_progress_flag_disables_reporter(
    _slack_token_env: None,
) -> None:
    record = _CallRecord()
    runner = CliRunner()
    with _patch_list_conversations([_public_row()], record=record):
        result = runner.invoke(
            app,
            ["--no-progress", "slack", "conversations"],
        )

    assert result.exit_code == 0, result.stdout + result.stderr
    reporter = record.kwargs["reporter"]
    reporter.advance(1)
    reporter.update(total=10, description="x")


def test_conversations_progress_flag_forces_reporter(
    _slack_token_env: None,
) -> None:
    record = _CallRecord()
    runner = CliRunner()
    with _patch_list_conversations([_public_row()], record=record):
        result = runner.invoke(
            app,
            ["--progress", "slack", "conversations"],
        )

    assert result.exit_code == 0, result.stdout + result.stderr
    record.kwargs["reporter"].advance(1)


# ----- empty result -----------------------------------------------------


def test_conversations_empty_table_emits_header_and_stderr_hint(
    _slack_token_env: None,
) -> None:
    runner = CliRunner()
    with _patch_list_conversations([], record=_CallRecord()):
        result = runner.invoke(app, ["slack", "conversations"])

    assert result.exit_code == 0
    assert "ID" in result.stdout
    assert "no conversations matched" in result.stderr


def test_conversations_empty_filter_hint_includes_filter_string(
    _slack_token_env: None,
) -> None:
    runner = CliRunner()
    with _patch_list_conversations([], record=_CallRecord()):
        result = runner.invoke(
            app,
            ["slack", "conversations", "--filter", "nope"],
        )

    assert result.exit_code == 0
    assert "no conversations matched" in result.stderr
    assert "nope" in result.stderr


def test_conversations_empty_toml_emits_empty_array(_slack_token_env: None) -> None:
    runner = CliRunner()
    with _patch_list_conversations([], record=_CallRecord()):
        result = runner.invoke(
            app,
            ["slack", "conversations", "--format", "toml"],
        )

    assert result.exit_code == 0
    assert "# Slack conversations (0)" in result.stdout
    assert "channels = []" in result.stdout
    assert "no conversations matched" in result.stderr


def test_conversations_empty_json_emits_empty_array_no_stderr_hint(
    _slack_token_env: None,
) -> None:
    import json as _json

    runner = CliRunner()
    with _patch_list_conversations([], record=_CallRecord()):
        result = runner.invoke(
            app,
            ["slack", "conversations", "--format", "json"],
        )

    assert result.exit_code == 0
    assert _json.loads(result.stdout) == []
    assert "no conversations matched" not in result.stderr


# ----- error paths ------------------------------------------------------


def test_conversations_config_error_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPSHUB_CONNECTOR_SLACK_TOKEN", raising=False)

    def _no_secret(_key: str) -> str | None:
        return None

    monkeypatch.setattr("opshub.core.secrets.get_secret", _no_secret)

    runner = CliRunner()
    result = runner.invoke(app, ["slack", "conversations"])

    assert result.exit_code == 1, result.stdout + result.stderr
    assert "not configured" in result.stderr.lower() or "Error:" in result.stderr


def test_conversations_connector_failed_error_exits_1_with_scope(
    _slack_token_env: None,
) -> None:
    failure = ConnectorFailedError(
        "Slack users.conversations failed: missing_scope "
        "(needed: 'im:read'). See ADR-0018 §Decision (7) ..."
    )
    runner = CliRunner()
    with _patch_list_conversations([], record=_CallRecord(), raises=failure):
        result = runner.invoke(app, ["slack", "conversations"])

    assert result.exit_code == 1, result.stdout + result.stderr
    assert "missing_scope" in result.stderr
    assert "im:read" in result.stderr
    assert "xoxb-test" not in result.stderr


def test_conversations_invalid_format_exits_2(_slack_token_env: None) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["slack", "conversations", "--format", "yaml"],
    )

    assert result.exit_code == 2, result.stdout + result.stderr
    assert "yaml" in result.stderr
    assert "table" in result.stderr
    assert "toml" in result.stderr
    assert "json" in result.stderr


# ----- cold-start guard -------------------------------------------------


def test_conversations_help_does_not_import_slack_sdk() -> None:
    """``slack conversations --help`` keeps ``slack_sdk`` off the cold-start path."""
    import sys

    saved = {
        name: sys.modules.pop(name) for name in list(sys.modules) if name.startswith("slack_sdk")
    }
    try:
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["slack", "conversations", "--help"],
        )
        assert result.exit_code == 0, result.stdout
        assert not any(
            name == "slack_sdk" or name.startswith("slack_sdk.") for name in sys.modules
        ), "slack_sdk leaked onto cold-start path via 'slack conversations --help'"
    finally:
        sys.modules.update(saved)


# ----- format-specific rendering edge cases ------------------------------


def test_conversations_table_truncates_long_purpose(_slack_token_env: None) -> None:
    long_purpose = "X" * 200
    rows = [_public_row(purpose=long_purpose)]
    runner = CliRunner()
    with _patch_list_conversations(rows, record=_CallRecord()):
        table_result = runner.invoke(app, ["slack", "conversations"])
        json_result = runner.invoke(app, ["slack", "conversations", "--format", "json"])

    assert table_result.exit_code == 0
    assert "…" in table_result.stdout
    assert long_purpose in json_result.stdout


# ----- type-bucket sort + --since filter (issue #374) -------------------


def _row_with_activity(
    base: SlackConversation,
    *,
    last_activity_ts: float,
) -> SlackConversation:
    from dataclasses import replace

    return replace(base, last_activity_ts=last_activity_ts)


def test_parse_since_relative_days() -> None:
    from datetime import UTC, datetime, timedelta

    from opshub.cli._slack_conversations import parse_since

    before = datetime.now(UTC) - timedelta(days=7)
    result = parse_since("7d")
    after = datetime.now(UTC) - timedelta(days=7)
    assert before <= result <= after
    assert result.tzinfo is not None


def test_parse_since_relative_weeks() -> None:
    from datetime import UTC, datetime, timedelta

    from opshub.cli._slack_conversations import parse_since

    before = datetime.now(UTC) - timedelta(weeks=2)
    result = parse_since("2w")
    after = datetime.now(UTC) - timedelta(weeks=2)
    assert before <= result <= after


def test_parse_since_absolute_iso_date_defaults_to_utc() -> None:
    from datetime import UTC, datetime

    from opshub.cli._slack_conversations import parse_since

    assert parse_since("2026-05-01") == datetime(2026, 5, 1, tzinfo=UTC)


def test_parse_since_absolute_iso_with_timezone_normalised_to_utc() -> None:
    from datetime import UTC, datetime

    from opshub.cli._slack_conversations import parse_since

    assert parse_since("2026-05-01T09:00:00+09:00") == datetime(2026, 5, 1, tzinfo=UTC)


def test_parse_since_zulu_suffix_accepted() -> None:
    from datetime import UTC, datetime

    from opshub.cli._slack_conversations import parse_since

    assert parse_since("2026-05-01T00:00:00Z") == datetime(2026, 5, 1, tzinfo=UTC)


def test_parse_since_rejects_empty() -> None:
    import typer

    from opshub.cli._slack_conversations import parse_since

    with pytest.raises(typer.BadParameter):
        parse_since("")
    with pytest.raises(typer.BadParameter):
        parse_since("   ")


def test_parse_since_rejects_unknown_unit() -> None:
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
    import typer

    from opshub.cli._slack_conversations import parse_since

    with pytest.raises(typer.BadParameter):
        parse_since("30")


def test_conversations_since_flag_propagates(_slack_token_env: None) -> None:
    from datetime import UTC, datetime, timedelta

    record = _CallRecord()
    runner = CliRunner()
    before = datetime.now(UTC) - timedelta(days=7)
    with _patch_list_conversations([], record=record):
        result = runner.invoke(
            app,
            ["slack", "conversations", "--since", "7d"],
        )

    assert result.exit_code == 0, result.stdout + result.stderr
    parsed_since = record.kwargs["since"]
    assert parsed_since is not None
    assert parsed_since.tzinfo is not None
    after = datetime.now(UTC) - timedelta(days=7)
    assert before <= parsed_since <= after


def test_conversations_since_invalid_value_exits_2(_slack_token_env: None) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["slack", "conversations", "--since", "garbage"],
    )

    assert result.exit_code == 2, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "garbage" in combined or "since" in combined


def test_conversations_default_sort_groups_by_type(_slack_token_env: None) -> None:
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
            ["slack", "conversations", "--format", "toml"],
        )

    assert result.exit_code == 0, result.stdout + result.stderr
    id_order = [line.split('"')[1] for line in result.stdout.splitlines() if line.startswith('  "')]
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
    rows = [
        _row_with_activity(_public_row("C-old", name="alpha"), last_activity_ts=1_000_000.0),
        _row_with_activity(_public_row("C-new", name="zulu"), last_activity_ts=2_000_000.0),
        _row_with_activity(_im_row("D1", peer="alice"), last_activity_ts=1_500_000.0),
    ]
    runner = CliRunner()
    with _patch_list_conversations(rows, record=_CallRecord()):
        result = runner.invoke(
            app,
            ["slack", "conversations", "--since", "30d", "--format", "toml"],
        )

    assert result.exit_code == 0, result.stdout + result.stderr
    id_order = [line.split('"')[1] for line in result.stdout.splitlines() if line.startswith('  "')]
    assert id_order == ["C-new", "C-old", "D1"]


def test_conversations_since_table_shows_last_activity_column(
    _slack_token_env: None,
) -> None:
    rows = [
        _row_with_activity(
            _public_row("C1", name="general"),
            last_activity_ts=1_717_200_000.0,
        ),
    ]
    runner = CliRunner()
    with _patch_list_conversations(rows, record=_CallRecord()):
        result = runner.invoke(
            app,
            ["slack", "conversations", "--since", "30d"],
        )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "LAST_ACTIVITY" in result.stdout
    assert "2024-06-01" in result.stdout


def test_conversations_no_since_table_omits_last_activity_column(
    _slack_token_env: None,
) -> None:
    rows = [_public_row("C1")]
    runner = CliRunner()
    with _patch_list_conversations(rows, record=_CallRecord()):
        result = runner.invoke(app, ["slack", "conversations"])

    assert result.exit_code == 0
    assert "LAST_ACTIVITY" not in result.stdout


def test_conversations_since_json_includes_last_activity_ts(
    _slack_token_env: None,
) -> None:
    import json as _json

    rows = [_row_with_activity(_public_row("C1", name="general"), last_activity_ts=1_717_200_000.0)]
    runner = CliRunner()
    with _patch_list_conversations(rows, record=_CallRecord()):
        result = runner.invoke(
            app,
            [
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
    import json as _json

    rows = [_public_row("C1")]
    runner = CliRunner()
    with _patch_list_conversations(rows, record=_CallRecord()):
        result = runner.invoke(
            app,
            ["slack", "conversations", "--format", "json"],
        )

    assert result.exit_code == 0
    payload = _json.loads(result.stdout)
    assert "last_activity_ts" not in payload[0]


def test_conversations_since_toml_comment_includes_activity_date(
    _slack_token_env: None,
) -> None:
    rows = [_row_with_activity(_public_row("C1", name="general"), last_activity_ts=1_717_200_000.0)]
    runner = CliRunner()
    with _patch_list_conversations(rows, record=_CallRecord()):
        result = runner.invoke(
            app,
            [
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
            ["slack", "conversations", "--since", "7d"],
        )

    assert result.exit_code == 0, result.stdout + result.stderr
    stderr = result.stderr
    public_idx = stderr.find("skipping public conversations")
    mpim_idx = stderr.find("skipping mpim conversations")
    assert public_idx != -1, stderr
    assert mpim_idx != -1, stderr
    assert public_idx < mpim_idx, "warnings must surface in iterator-append order"


def test_sort_rows_activity_mode_pushes_missing_ts_to_bucket_tail() -> None:
    from opshub.cli._slack_conversations import _sort_rows  # pyright: ignore[reportPrivateUsage]

    rows = [
        _row_with_activity(_public_row("C-mid", name="charlie"), last_activity_ts=1_500_000.0),
        _public_row("C-none", name="bravo"),
        _row_with_activity(_public_row("C-new", name="alpha"), last_activity_ts=2_000_000.0),
    ]

    result = _sort_rows(rows, by_activity=True)

    assert [r.id for r in result] == ["C-new", "C-mid", "C-none"]


def test_parse_since_rejects_overflowingly_large_amount() -> None:
    import typer

    from opshub.cli._slack_conversations import parse_since

    with pytest.raises(typer.BadParameter) as excinfo:
        parse_since("99999999999d")
    assert "too far in the past" in str(excinfo.value) or "ISO date" in str(excinfo.value)


def test_conversations_since_json_drops_only_none_rows_in_mixed_payload(
    _slack_token_env: None,
) -> None:
    import json as _json

    rows = [
        _row_with_activity(_public_row("C-pop", name="alpha"), last_activity_ts=1_717_200_000.0),
        _public_row("C-none", name="bravo"),
    ]
    runner = CliRunner()
    with _patch_list_conversations(rows, record=_CallRecord()):
        result = runner.invoke(
            app,
            [
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
    by_id = {row["id"]: row for row in payload}
    assert by_id["C-pop"]["last_activity_ts"] == 1_717_200_000.0
    assert "last_activity_ts" not in by_id["C-none"]


def test_sort_rows_unknown_type_falls_to_tail_bucket() -> None:
    from typing import cast

    from opshub.cli._slack_conversations import _sort_rows  # pyright: ignore[reportPrivateUsage]
    from opshub.connectors.slack.conversations import ConversationType, SlackConversation

    unknown = SlackConversation(
        id="X-future",
        type=cast(ConversationType, "future_kind"),
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

    assert [r.id for r in result] == ["C-pub", "D-dm", "X-future"]
