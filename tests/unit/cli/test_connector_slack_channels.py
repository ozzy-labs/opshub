"""Tests for ``opshub connector slack channels`` (#341 PR2).

The CLI command wraps :func:`opshub.connectors.slack.channels.list_channels`
(PR1, [#344](https://github.com/ozzy-labs/opshub/pull/344)) and renders
the result in one of three formats — ``table`` (default), ``toml``, or
``json``. Behaviour worth pinning here:

1. Happy path through each output format, including the substring
   filter, the ``--limit`` cap, the ``--include-archived`` gate, and
   the ``--include-private`` gate (which flips the Slack request's
   ``types`` parameter).
2. Empty result: stderr hint surfaces and exit code stays ``0`` for
   ``table`` / ``toml``; JSON still emits ``[]`` on stdout.
3. Auth failures bubble up as ``ConfigError`` (no token / wrong prefix
   / extras missing) → exit 1 with a sanitised stderr message.
4. Runtime failures bubble up as ``ConnectorFailedError``
   (``invalid_auth`` / ``missing_scope`` / exhausted 429 retries) →
   exit 1 with a sanitised stderr message.
5. Invalid ``--format`` value raises a usage error (exit 2) with a
   helpful diagnostic.

The Slack SDK extras may not be installed in every CI environment,
so the file-level ``pytest.importorskip`` gates the whole module —
matching ``test_channels.py``'s strategy. The CLI tests stub the
underlying :func:`list_channels` iterator (rather than the
``WebClient`` boundary) so the format / filter / pagination logic
under test stays sharply scoped to the CLI surface; PR1's tests
already pin the iterator behaviour at the SDK boundary.
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
from opshub.connectors.slack.channels import SlackChannel
from opshub.core.errors import ConnectorFailedError

# ----- shared fixtures ---------------------------------------------------


def _channel(
    channel_id: str = "C01234567",
    *,
    name: str = "general",
    is_private: bool = False,
    is_archived: bool = False,
    purpose: str = "Company-wide announcements",
) -> SlackChannel:
    """Build a :class:`SlackChannel` fixture for the CLI tests.

    The PR1 iterator already pins the underlying API-shape → dataclass
    translation; CLI tests only care about which rows land where in
    the formatted output.
    """
    return SlackChannel(
        id=channel_id,
        name=name,
        is_private=is_private,
        is_archived=is_archived,
        purpose=purpose,
    )


class _CallRecord:
    """Record of one ``list_channels`` invocation.

    The CLI tests patch :func:`opshub.connectors.slack.channels.list_channels`
    in-place; the patch's side effect captures the keyword arguments
    so tests can assert ``include_private`` etc. propagated correctly.
    """

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}
        self.auth_token: str | None = None


def _patch_list_channels(
    rows: list[SlackChannel],
    *,
    record: _CallRecord,
    raises: Exception | None = None,
) -> AbstractContextManager[MagicMock]:
    """Build a patch context that returns ``rows`` for ``list_channels``.

    The patched function accepts the same keyword arguments as the
    real :func:`list_channels` and records them on ``record`` so
    tests can assert which knobs the CLI propagated.
    """

    def _fake_list_channels(auth: Any, **kwargs: Any) -> Iterator[SlackChannel]:
        record.auth_token = getattr(auth, "token", None)
        record.kwargs = dict(kwargs)
        if raises is not None:
            raise raises
        # Yield from a list so the iterator semantics match the real
        # implementation (PR1 returns ``Iterator[SlackChannel]``).
        yield from rows

    # Patch the original source so the helper's lazy import inside
    # ``run_channels_command`` picks up the stub. ``unittest.mock.patch``
    # cannot patch a name that has not yet been bound on the importing
    # module (the helper imports lazily inside the command body), so we
    # patch where ``list_channels`` is defined instead.
    return patch(
        "opshub.connectors.slack.channels.list_channels",
        side_effect=_fake_list_channels,
    )


@pytest.fixture
def _slack_token_env(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    """Plant a Slack OAuth token in the env so :class:`SlackAuth` constructs.

    The env-var override (``OPSHUB_CONNECTOR_SLACK_TOKEN``) wins over
    the keyring per ADR-0014, so this fixture keeps the tests
    keyring-independent. The token shape ``xoxb-test`` matches the
    auth module's validator.
    """
    monkeypatch.setenv("OPSHUB_CONNECTOR_SLACK_TOKEN", "xoxb-test")


# ----- happy path: each output format -----------------------------------


def test_channels_default_format_is_table(_slack_token_env: None) -> None:
    """No ``--format`` flag → table output with header + one row per channel."""
    rows = [
        _channel("C01234567", name="general", purpose="Company-wide"),
        _channel("C02345678", name="eng-backend", purpose="Backend eng"),
    ]
    record = _CallRecord()
    runner = CliRunner()
    with _patch_list_channels(rows, record=record):
        result = runner.invoke(app, ["connector", "slack", "channels"])

    assert result.exit_code == 0, result.stdout + result.stderr
    out = result.stdout
    # Header columns
    assert "ID" in out
    assert "NAME" in out
    assert "PRIVATE" in out
    assert "ARCHIVED" in out
    assert "PURPOSE" in out
    # Each channel renders one row
    assert "C01234567" in out
    assert "general" in out
    assert "C02345678" in out
    assert "eng-backend" in out
    # ``include_private`` / ``include_archived`` default to False so
    # the iterator was asked for live public channels only.
    assert record.kwargs["include_private"] is False
    assert record.kwargs["include_archived"] is False
    # The Slack token loaded from the env-var override propagates to
    # the iterator's auth — sanity-check that the lazy import path
    # works end-to-end.
    assert record.auth_token == "xoxb-test"


def test_channels_format_toml_emits_paste_ready_snippet(
    _slack_token_env: None,
) -> None:
    """``--format toml`` emits a ``channels = [...]`` snippet with comments."""
    rows = [
        _channel("C01234567", name="general"),
        _channel("G03456789", name="leadership", is_private=True, purpose=""),
    ]
    runner = CliRunner()
    with _patch_list_channels(rows, record=_CallRecord()):
        result = runner.invoke(
            app,
            ["connector", "slack", "channels", "--format", "toml"],
        )

    assert result.exit_code == 0, result.stdout + result.stderr
    out = result.stdout
    # Header records the count so reviewers spot truncated pastes.
    assert "# Slack channels (2)" in out
    assert "channels = [" in out
    # Each id sits inside quotes; comment carries the human name.
    assert '"C01234567"' in out
    assert "# general" in out
    assert '"G03456789"' in out
    # Private flag surfaces in the inline comment.
    assert "leadership (private)" in out
    assert out.rstrip().endswith("]")


def test_channels_format_json_emits_array_of_dataclass_dicts(
    _slack_token_env: None,
) -> None:
    """``--format json`` emits a JSON array of dataclass fields."""
    import json as _json

    rows = [
        _channel(
            "C01234567",
            name="general",
            is_private=False,
            is_archived=False,
            purpose="Company-wide",
        ),
    ]
    runner = CliRunner()
    with _patch_list_channels(rows, record=_CallRecord()):
        result = runner.invoke(
            app,
            ["connector", "slack", "channels", "--format", "json"],
        )

    assert result.exit_code == 0, result.stdout + result.stderr
    payload = _json.loads(result.stdout)
    assert payload == [
        {
            "id": "C01234567",
            "name": "general",
            "is_private": False,
            "is_archived": False,
            "purpose": "Company-wide",
        }
    ]


# ----- flag propagation -------------------------------------------------


def test_channels_filter_flag_propagates_to_iterator(_slack_token_env: None) -> None:
    """``--filter`` forwards verbatim so case-insensitive matching happens upstream."""
    record = _CallRecord()
    runner = CliRunner()
    with _patch_list_channels([], record=record):
        result = runner.invoke(
            app,
            ["connector", "slack", "channels", "--filter", "Backend"],
        )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert record.kwargs["filter_substring"] == "Backend"


def test_channels_filter_empty_string_normalised_to_none(
    _slack_token_env: None,
) -> None:
    """``--filter ""`` collapses to ``None`` upstream so the iterator no-ops.

    The CLI normalises an empty-string filter to ``None`` before
    handing it to :func:`list_channels`. The iterator itself also
    no-ops on the empty string (PR1 behaviour) — the CLI-side
    normalisation makes the stderr hint shape distinguishable from
    "user did pass a real filter".
    """
    record = _CallRecord()
    runner = CliRunner()
    with _patch_list_channels([_channel()], record=record):
        result = runner.invoke(
            app,
            ["connector", "slack", "channels", "--filter", ""],
        )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert record.kwargs["filter_substring"] is None


def test_channels_limit_flag_propagates_to_iterator(_slack_token_env: None) -> None:
    """``--limit N`` forwards as the iterator's ``limit`` kwarg."""
    record = _CallRecord()
    runner = CliRunner()
    with _patch_list_channels([_channel()], record=record):
        result = runner.invoke(
            app,
            ["connector", "slack", "channels", "--limit", "5"],
        )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert record.kwargs["limit"] == 5


def test_channels_include_private_flips_iterator_kwarg(
    _slack_token_env: None,
) -> None:
    """``--include-private`` forwards as ``include_private=True``.

    PR1's iterator translates that boolean into the
    ``"public_channel,private_channel"`` ``types`` parameter at the
    SDK boundary; PR2's CLI just needs to forward the flag verbatim.
    """
    record = _CallRecord()
    runner = CliRunner()
    with _patch_list_channels([_channel()], record=record):
        result = runner.invoke(
            app,
            ["connector", "slack", "channels", "--include-private"],
        )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert record.kwargs["include_private"] is True


def test_channels_include_archived_flips_iterator_kwarg(
    _slack_token_env: None,
) -> None:
    """``--include-archived`` forwards as ``include_archived=True``."""
    record = _CallRecord()
    runner = CliRunner()
    with _patch_list_channels([_channel(is_archived=True)], record=record):
        result = runner.invoke(
            app,
            ["connector", "slack", "channels", "--include-archived"],
        )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert record.kwargs["include_archived"] is True
    # The archived row should appear in the table output with the
    # ``yes`` flag in the ARCHIVED column.
    assert "yes" in result.stdout


# ----- empty result -----------------------------------------------------


def test_channels_empty_table_emits_header_and_stderr_hint(
    _slack_token_env: None,
) -> None:
    """No matches → table still shows the header; hint goes to stderr."""
    runner = CliRunner()
    with _patch_list_channels([], record=_CallRecord()):
        result = runner.invoke(app, ["connector", "slack", "channels"])

    assert result.exit_code == 0, result.stdout + result.stderr
    # Header survives so the operator sees the structure.
    assert "ID" in result.stdout
    # Hint surfaces on stderr so pipelines see only the (empty) stdout.
    assert "no channels matched" in result.stderr


def test_channels_empty_filter_hint_includes_filter_string(
    _slack_token_env: None,
) -> None:
    """The empty-state stderr hint quotes the filter value so the operator can correct it."""
    runner = CliRunner()
    with _patch_list_channels([], record=_CallRecord()):
        result = runner.invoke(
            app,
            ["connector", "slack", "channels", "--filter", "nope"],
        )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "no channels matched" in result.stderr
    assert "nope" in result.stderr


def test_channels_empty_toml_emits_empty_array_assignment(
    _slack_token_env: None,
) -> None:
    """No matches with ``--format toml`` still emits ``channels = []`` for paste-ability."""
    runner = CliRunner()
    with _patch_list_channels([], record=_CallRecord()):
        result = runner.invoke(
            app,
            ["connector", "slack", "channels", "--format", "toml"],
        )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "# Slack channels (0)" in result.stdout
    assert "channels = []" in result.stdout
    # Hint surfaces on stderr; stdout stays focused on the TOML.
    assert "no channels matched" in result.stderr


def test_channels_empty_json_emits_empty_array_no_stderr_hint(
    _slack_token_env: None,
) -> None:
    """No matches with ``--format json`` emits ``[]`` on stdout — no stderr hint.

    ``jq`` consumers expect a parseable array even when zero matches,
    and a stderr hint on top of an already-parseable empty array
    would be noise.
    """
    import json as _json

    runner = CliRunner()
    with _patch_list_channels([], record=_CallRecord()):
        result = runner.invoke(
            app,
            ["connector", "slack", "channels", "--format", "json"],
        )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert _json.loads(result.stdout) == []
    assert "no channels matched" not in result.stderr


# ----- error paths ------------------------------------------------------


def test_channels_config_error_exits_1_with_stderr_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing token → ``ConfigError`` → exit 1 + sanitised stderr.

    ``SlackAuth`` raises :class:`ConfigError` on construction when no
    token is configured. The CLI maps that to exit 1 with the
    ``ConfigError`` message echoed on stderr (the message itself is
    already sanitised by ``SlackAuth``).
    """
    # Clear both env-var and keyring sources so SlackAuth has nothing
    # to resolve. We monkey-patch ``get_secret`` to return ``None``
    # without actually probing the OS keychain (which may or may not
    # be available in CI).
    monkeypatch.delenv("OPSHUB_CONNECTOR_SLACK_TOKEN", raising=False)

    def _no_secret(_key: str) -> str | None:
        return None

    monkeypatch.setattr("opshub.core.secrets.get_secret", _no_secret)

    runner = CliRunner()
    result = runner.invoke(app, ["connector", "slack", "channels"])

    assert result.exit_code == 1, result.stdout + result.stderr
    # The ConfigError message points the operator at the
    # ``auth set`` command without echoing the token (there is no
    # token to echo on this path).
    assert "not configured" in result.stderr.lower() or "Error:" in result.stderr


def test_channels_connector_failed_error_exits_1_with_stderr_message(
    _slack_token_env: None,
) -> None:
    """``missing_scope`` from the iterator → exit 1 + scope name on stderr.

    The CLI surfaces :class:`ConnectorFailedError` verbatim. The
    iterator already includes the scope name in the message
    (``missing_scope (needed: ...)``) so the operator can extend
    their OAuth grant without round-tripping the docs.
    """
    failure = ConnectorFailedError(
        "Slack conversations.list failed: missing_scope "
        "(needed: 'groups:read'). See ADR-0018 §Decision (7) ..."
    )
    runner = CliRunner()
    with _patch_list_channels([], record=_CallRecord(), raises=failure):
        result = runner.invoke(
            app,
            ["connector", "slack", "channels", "--include-private"],
        )

    assert result.exit_code == 1, result.stdout + result.stderr
    assert "missing_scope" in result.stderr
    assert "groups:read" in result.stderr
    # Token must never appear in stderr on the error path.
    assert "xoxb-test" not in result.stderr


def test_channels_invalid_format_exits_2_with_helpful_stderr(
    _slack_token_env: None,
) -> None:
    """Unknown ``--format`` value → usage error (exit 2) listing valid options."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["connector", "slack", "channels", "--format", "yaml"],
    )

    assert result.exit_code == 2, result.stdout + result.stderr
    assert "yaml" in result.stderr
    # Diagnostic enumerates the valid choices so the operator can self-correct.
    assert "table" in result.stderr
    assert "toml" in result.stderr
    assert "json" in result.stderr


# ----- cold-start guard -------------------------------------------------


def test_channels_help_does_not_import_slack_sdk() -> None:
    """``connector slack channels --help`` keeps ``slack_sdk`` off the cold-start path.

    ADR-0001 budget: heavy SDK imports must stay inside the command
    callback. The Typer ``--help`` path short-circuits the body, so
    ``slack_sdk`` must not appear in ``sys.modules`` after the help
    invocation — even though the channels command itself uses it.

    The integration cold-start static check (``test_cli_imports``)
    catches module-level violations across every public ``cli/*.py``;
    this test is a belt-and-braces runtime probe specifically for the
    ``connector slack channels`` help path.
    """
    import sys

    # Pre-clean: if a previous test imported slack_sdk transitively,
    # the cold-start guarantee for this command is already broken at
    # process level. We can still assert that *invoking* the help
    # path does not require it, by reading the module's lazy-import
    # structure rather than ``sys.modules`` membership. Use a fresh
    # subprocess-equivalent: patch sys.modules to drop slack_sdk and
    # verify the help path tolerates the absence.
    saved = {
        name: sys.modules.pop(name) for name in list(sys.modules) if name.startswith("slack_sdk")
    }
    try:
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["connector", "slack", "channels", "--help"],
        )
        assert result.exit_code == 0, result.stdout
        # ``slack_sdk`` must still be absent — the help path never
        # exercises the lazy import inside the handler body.
        assert not any(
            name == "slack_sdk" or name.startswith("slack_sdk.") for name in sys.modules
        ), "slack_sdk leaked onto cold-start path via 'connector slack channels --help'"
    finally:
        # Restore so subsequent tests that *did* import slack_sdk are
        # not surprised by a missing module reference.
        sys.modules.update(saved)


# ----- format-specific rendering edge cases ------------------------------


def test_channels_table_truncates_long_purpose(_slack_token_env: None) -> None:
    """The PURPOSE column truncates at the documented cap so the row stays narrow.

    JSON output keeps the full string so downstream consumers see the
    truncation only in the human-readable table — PR1 made the
    dataclass field a verbatim copy of ``channel.purpose.value`` for
    exactly this reason.
    """
    long_purpose = "X" * 200
    rows = [_channel(purpose=long_purpose)]
    runner = CliRunner()
    with _patch_list_channels(rows, record=_CallRecord()):
        table_result = runner.invoke(app, ["connector", "slack", "channels"])
        json_result = runner.invoke(app, ["connector", "slack", "channels", "--format", "json"])

    assert table_result.exit_code == 0
    assert "…" in table_result.stdout
    # JSON keeps the full string verbatim.
    assert long_purpose in json_result.stdout
