"""Pins the CLI-level exception sanitisation for ``opshub github sync``.

The CLI driver in :mod:`opshub.cli._connector_common` catches every exception
raised inside :meth:`Connector.sync` and records a
:class:`ConnectorSyncFailed` event with ``error_message=type(exc).__name__``
— the exception **type name only**, never the original message — so
secrets / PII never reach the event log (ADR-0005, ADR-0010, ADR-0014).

Phase 7 has equivalent sanitisation tests for the Slack / MS365 / Box
connectors (:mod:`tests.integration.test_phase7_slack_sync` and
siblings). The Phase 3 GitHub connector lacked one despite running
through the same CLI driver, so a regression that swapped
``type(exc).__name__`` for ``str(exc)`` (or removed the sanitisation
entirely) would surface only at the operator-facing level, after a
real PAT or other secret had already been persisted.

This module fills that gap by mocking the lowest sensible boundary —
the GitHub fetch primitives in :mod:`opshub.connectors.github.api` —
to raise a custom exception whose message embeds a token-shaped string,
then asserting:

* The CLI exits with code 1 (sync failure).
* The recorded :class:`ConnectorSyncFailed` event's payload contains
  the exception type name (``"RuntimeError"``).
* The original message — including the embedded token-shaped substring
  — does NOT appear in the event payload.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from opshub.cli.app import app

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

_PathsDict = dict[str, Path]

# A deliberately realistic-looking GitHub Personal Access Token shape.
# The exact byte sequence is meaningless — the only contract this test
# pins is that NO substring of the exception message reaches the event
# log. We choose a token-shaped string because that is the canonical
# secret a GitHub-side error could leak (rate-limit headers, scoped-token
# 401 responses, etc. occasionally echo the PAT prefix back).
_TOKEN_SHAPED = "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"  # deliberate fixture
_EXCEPTION_MESSAGE = f"API call failed with PAT {_TOKEN_SHAPED}"


@pytest.fixture
def github_creds(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Inject the repo + PAT env vars the GitHub connector requires.

    Mirrors the fixture in
    :mod:`tests.integration.test_github_connector_lifecycle`. The
    actual PAT value is irrelevant — the fetcher is monkeypatched
    before it can read the env at runtime — but the connector's config
    check would short-circuit with :class:`ConfigError` before we
    reach the sanitisation arm if either env var is unset.
    """
    monkeypatch.setenv("OPSHUB_CONNECTOR_GITHUB_REPO", "owner/repo")
    monkeypatch.setenv("OPSHUB_CONNECTOR_GITHUB_PAT", "ghp_test_fixture_pat")
    yield


def _patch_github_fetcher_to_raise(monkeypatch: pytest.MonkeyPatch, *, error: Exception) -> None:
    """Replace :func:`list_issues_since` so the first fetch call raises ``error``.

    The connector hits :func:`list_issues_since` first (see
    :meth:`GitHubConnector.sync`), so patching it is enough to surface
    the exception through the CLI driver's ``try / except`` arm.
    Patching at this layer mirrors the Phase 7 Slack test which
    swaps out :class:`SlackFetcher` for the same reason.
    """
    from opshub.connectors.github import api as github_api

    def _raise_on_call(*_args: object, **_kwargs: object) -> Iterator[object]:
        raise error
        yield  # pragma: no cover  # makes the function a generator for typing

    monkeypatch.setattr(github_api, "list_issues_since", _raise_on_call)


def test_connector_sync_github_sanitises_exception_message(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
    github_creds: None,
) -> None:
    """A fetcher exception with a token in the message → only the type name persists.

    The contract under test (per :mod:`opshub.cli._connector_common`):

    * CLI exits with code 1.
    * The :class:`ConnectorSyncFailed` event's ``error_message`` field
      contains the exception **type name only** (``"RuntimeError"``).
    * No substring of the original exception message — including the
      embedded token-shaped value — appears anywhere in the event
      payload on disk.
    """
    db_path = isolated_env["db_path"]
    _patch_github_fetcher_to_raise(monkeypatch, error=RuntimeError(_EXCEPTION_MESSAGE))

    runner = CliRunner()
    result = runner.invoke(app, ["github", "sync"])

    # CLI driver maps ``Exception`` → sanitised event + exit code 1.
    assert result.exit_code == 1, result.stdout
    # The type name is the operator-facing breadcrumb; the message is
    # not.
    assert "RuntimeError" in result.stderr, result.stderr
    assert _TOKEN_SHAPED not in result.stderr
    assert _EXCEPTION_MESSAGE not in result.stderr

    # The event log must carry exactly one ``connector.sync_failed`` row
    # whose payload contains the type name only.
    from sqlalchemy import text

    from opshub.db.engine import create_engine_for_sqlite

    engine: Engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            failed_rows = [
                dict(row)
                for row in conn.execute(
                    text(
                        "SELECT event_type, payload FROM events "
                        "WHERE event_type = 'connector.sync_failed'"
                    )
                ).mappings()
            ]
        assert len(failed_rows) == 1, (
            f"expected exactly one connector.sync_failed event, got {len(failed_rows)}"
        )
        payload = failed_rows[0]["payload"]
        # Type name reaches the event log…
        assert "RuntimeError" in payload, payload
        # …but the original message and embedded token-shaped substring
        # do NOT. This is the whole point of the sanitisation: a future
        # refactor that swapped ``type(exc).__name__`` for ``str(exc)``
        # would let the token reach the event log here and fail this
        # assertion loudly.
        assert _TOKEN_SHAPED not in payload, (
            f"sanitisation broken: token-shaped substring leaked into event payload: {payload!r}"
        )
        assert "API call failed" not in payload, (
            f"sanitisation broken: exception message text leaked into event payload: {payload!r}"
        )
    finally:
        engine.dispose()
