"""End-to-end 2-workspace :class:`SlackConnector` lifecycle (Phase 24-E).

Phase 24 ([ADR-0041](../../docs/adr/0041-slack-multi-workspace.md)) makes
``1 install = N Slack workspaces`` first-class. This suite is the
integration-level closeout pin for the multi-workspace contract, driving
the **public CLI** (``opshub slack sync``) against two configured
workspaces (``acme`` / ``oss``) through the same ``isolated_env`` fixture
the single-workspace Phase 7 suite uses. It covers the four scenarios the
epic #552 §テスト計画 listed for 24-E end-to-end:

1. **bind → sync** — the first all-workspace sync binds each alias's cursor
   entry to its own Slack ``team_id`` and ingests each workspace's messages
   under its own 3-token ``external_id`` namespace
   (``f"{team_id}:{channel_id}:{ts}"``). A channel id reused across both
   workspaces lands as **two distinct sources** (the team_id prefix is what
   keeps them separate).
2. **re-sync idempotent** — a second all-workspace sync with no new yields
   advances no projection rows and leaves each alias's per-alias-nested
   cursor byte-identical.
3. **token swap reject** — swapping the stored token under one alias to a
   *different* workspace's ``team_id`` makes the next sync fail loud
   (``ConfigError`` via the per-alias bind guard) **before any fetch**, so
   no foreign-workspace message can enter that alias's namespace. The
   sibling alias is unaffected (per-workspace error isolation).
4. **alias rename → idempotent re-fetch** — renaming an alias (config table
   header change) loses that workspace's cursor entry (the cursor nests
   under the alias key), so the next sync cold-starts and re-fetches. But
   because ``external_id`` is keyed on ``team_id`` (not the alias), the
   re-fetch is an idempotent upsert: **no duplicate sources**, only the API
   fetch cost.

Why integration-level (not pure unit): the load-bearing invariants —
per-alias cursor nesting, the team_id-prefixed ``external_id`` uniqueness
across workspaces, and error isolation routing through the CLI driver's
terminal ``cursor_set`` — only hold when the connector runs end-to-end
through the same ``opshub slack sync`` path an operator drives. The unit
suites pin the pieces in isolation; this suite pins them composed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

pytest.importorskip(
    "slack_sdk",
    reason="Slack connector tests require the 'connectors-slack' extras",
)

from opshub.cli.app import app
from opshub.connectors import register_connector, unregister_all
from opshub.connectors.slack.connector import SlackConnector
from opshub.connectors.slack.fetcher import RawSlackMessage
from opshub.db.engine import create_engine_for_sqlite
from opshub.projections.sources import sources_table

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

_PathsDict = dict[str, Path]

# Two workspaces, each with its own stable Slack team_id. ``acme`` and
# ``oss`` deliberately share channel id ``C1`` so the suite proves the
# team_id prefix is what keeps their sources distinct.
_ACME_TEAM = "T-ACME"
_OSS_TEAM = "T-OSS"
_ACME_SELF = "U-ACME-SELF"
_OSS_SELF = "U-OSS-SELF"


# ---------------------------------------------------------------------- helpers


def _row_count(engine: Engine, table_name: str) -> int:
    from sqlalchemy import text

    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one())


def _cursor_value(engine: Engine) -> str | None:
    from sqlalchemy import text as sql_text

    with engine.connect() as conn:
        row = (
            conn.execute(
                sql_text(
                    "SELECT cursor_value FROM connector_cursors WHERE connector_name = 'slack'"
                )
            )
            .mappings()
            .one_or_none()
        )
    return None if row is None else row["cursor_value"]


def _external_ids(engine: Engine) -> set[str]:
    from sqlalchemy import select

    with engine.connect() as conn:
        return {
            row["external_id"]
            for row in conn.execute(select(sources_table.c.external_id)).mappings()
        }


def _raw_message(
    *,
    team_id: str,
    channel_id: str = "C1",
    channel_name: str = "general",
    ts: str,
    text: str,
) -> RawSlackMessage:
    return RawSlackMessage(
        team_id=team_id,
        channel_id=channel_id,
        channel_name=channel_name,
        ts=ts,
        text=text,
        user_id="U1",
        user_display_name="alice",
        permalink=f"https://{team_id}.slack.com/archives/{channel_id}/p{ts.replace('.', '')}",
        raw={},
    )


def _patch_fetcher_per_team(
    monkeypatch: pytest.MonkeyPatch,
    *,
    yields_by_team: dict[str, list[tuple[str, RawSlackMessage, str | None]]],
) -> None:
    """Patch :class:`SlackFetcher` so each workspace yields its own messages.

    The connector constructs ``SlackFetcher(auth, channels=..., team_id=...)``
    once per workspace; we route on the ``team_id`` kwarg so each alias's
    fetch returns only that workspace's payloads. A ``team_id`` absent from
    ``yields_by_team`` yields nothing (e.g. the swapped-token workspace,
    which never reaches fetch because the bind guard rejects it first).
    """
    from unittest.mock import MagicMock

    def _factory(auth: Any, *, channels: Any, team_id: str) -> Any:
        del auth, channels
        fetcher = MagicMock()

        def _fetch_messages(
            *,
            cursor_per_channel: dict[str, str | None],
            max_per_channel: int = 100,
            excludes: Any = None,
        ) -> Iterator[tuple[str, RawSlackMessage, str | None]]:
            del cursor_per_channel, max_per_channel, excludes
            return iter(yields_by_team.get(team_id, []))

        fetcher.fetch_messages.side_effect = _fetch_messages
        return fetcher

    monkeypatch.setattr(
        "opshub.connectors.slack.connector.SlackFetcher",
        MagicMock(side_effect=_factory),
    )


@pytest.fixture
def two_workspace_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    """Configure two Slack workspaces (acme / oss) hermetically.

    * Per-alias token overrides keep :class:`SlackAuth` off the keyring.
    * Per-alias channel lists register both ``workspaces.<alias>`` tables;
      both use channel ``C1`` so the suite can prove team_id namespacing.
    * ``SlackAuth.test_token`` is stubbed to report the alias's own team_id
      / self user id (the alias is observable via ``SlackAuth.alias``), so
      the per-alias bind guard binds the right workspace without a network
      round-trip. Returns the per-alias ``team_id`` map so the token-swap
      test can mutate it.
    """
    monkeypatch.setenv("OPSHUB_CONNECTOR_SLACK_ACME_TOKEN", "xoxp-acme")
    monkeypatch.setenv("OPSHUB_CONNECTOR_SLACK_OSS_TOKEN", "xoxp-oss")
    monkeypatch.setenv("OPSHUB_CONNECTORS__SLACK__WORKSPACES__ACME__CHANNELS", '["C1"]')
    monkeypatch.setenv("OPSHUB_CONNECTORS__SLACK__WORKSPACES__OSS__CHANNELS", '["C1"]')

    # Mutable alias→team_id map so the token-swap test can repoint an
    # alias's live workspace mid-suite.
    team_by_alias = {"acme": _ACME_TEAM, "oss": _OSS_TEAM}
    self_by_team = {_ACME_TEAM: _ACME_SELF, _OSS_TEAM: _OSS_SELF}

    from opshub.connectors.slack.auth import SlackAuth

    def _stub_test_token(self: SlackAuth) -> dict[str, str]:
        alias = self.alias or "acme"
        team_id = team_by_alias[alias]
        return {
            "team": alias,
            "team_id": team_id,
            "user": alias,
            "user_id": self_by_team[team_id],
            "principal": "user",
        }

    monkeypatch.setattr(SlackAuth, "test_token", _stub_test_token)
    yield team_by_alias


@pytest.fixture(autouse=True)
def _reset_registry() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Restore the slack connector around each test (Phase 7 precedent)."""
    unregister_all()
    register_connector(SlackConnector())
    yield
    unregister_all()


# ---------------------------------------------------------------------- scenarios


def test_two_workspace_bind_then_sync_separates_external_ids(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
    two_workspace_env: dict[str, str],
) -> None:
    """First all-workspace sync binds each alias and separates sources by team_id.

    Both workspaces use channel ``C1`` with the *same* ts; pre-Phase-24
    (2-token ``external_id = f"{channel_id}:{ts}"``) these would have
    collided into a single source. The 3-token re-key
    (``f"{team_id}:{channel_id}:{ts}"``) keeps them distinct, and each
    alias's cursor entry binds its own ``team_id``.
    """
    del two_workspace_env
    shared_ts = "1700000001.000100"
    _patch_fetcher_per_team(
        monkeypatch,
        yields_by_team={
            _ACME_TEAM: [
                ("C1", _raw_message(team_id=_ACME_TEAM, ts=shared_ts, text="acme msg"), shared_ts),
            ],
            _OSS_TEAM: [
                ("C1", _raw_message(team_id=_OSS_TEAM, ts=shared_ts, text="oss msg"), shared_ts),
            ],
        },
    )

    runner = CliRunner()
    result = runner.invoke(app, ["slack", "sync"])
    assert result.exit_code == 0, result.stdout

    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        # Two workspaces x one message each = two distinct sources, even
        # though both share channel C1 + ts. The team_id prefix is the
        # only thing keeping them apart.
        assert _row_count(engine, "sources") == 2
        assert _external_ids(engine) == {
            f"{_ACME_TEAM}:C1:{shared_ts}",
            f"{_OSS_TEAM}:C1:{shared_ts}",
        }

        # The cursor is the per-alias envelope; each alias bound its own
        # team_id (ADR-0041 §(d)).
        cursor_value = _cursor_value(engine)
        assert cursor_value is not None
        parsed = json.loads(cursor_value)
        assert parsed == {
            "workspaces": {
                "acme": {
                    "channels": {"C1": shared_ts},
                    "backfill": {},
                    "threads": {},
                    "team_id": _ACME_TEAM,
                },
                "oss": {
                    "channels": {"C1": shared_ts},
                    "backfill": {},
                    "threads": {},
                    "team_id": _OSS_TEAM,
                },
            }
        }
    finally:
        engine.dispose()


def test_two_workspace_resync_is_idempotent(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
    two_workspace_env: dict[str, str],
) -> None:
    """A second all-workspace sync with no new yields is a no-op on projections.

    Pins the per-alias cursor round-trip end-to-end: sync #1 advances both
    aliases; sync #2 (empty fetcher) re-parses the per-alias envelope, hands
    each alias its channels axis, yields nothing, and leaves the cursor
    byte-identical (deterministic ``_dump_cursors`` + unchanged inputs).
    """
    del two_workspace_env
    shared_ts = "1700000001.000100"
    _patch_fetcher_per_team(
        monkeypatch,
        yields_by_team={
            _ACME_TEAM: [
                ("C1", _raw_message(team_id=_ACME_TEAM, ts=shared_ts, text="acme"), shared_ts),
            ],
            _OSS_TEAM: [
                ("C1", _raw_message(team_id=_OSS_TEAM, ts=shared_ts, text="oss"), shared_ts),
            ],
        },
    )
    runner = CliRunner()
    first = runner.invoke(app, ["slack", "sync"])
    assert first.exit_code == 0, first.stdout

    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        assert _row_count(engine, "sources") == 2
        cursor_after_first = _cursor_value(engine)

        # Second sync: both workspaces yield nothing.
        _patch_fetcher_per_team(monkeypatch, yields_by_team={})
        second = runner.invoke(app, ["slack", "sync"])
        assert second.exit_code == 0, second.stdout

        # No new rows; cursor byte-identical.
        assert _row_count(engine, "sources") == 2
        assert _row_count(engine, "inbox_items") == 2
        assert _cursor_value(engine) == cursor_after_first
    finally:
        engine.dispose()


def test_two_workspace_token_swap_rejected_with_isolation(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
    two_workspace_env: dict[str, str],
) -> None:
    """Swapping one alias's token to another workspace fails loud; the sibling is fine.

    After a clean first sync binds ``acme`` → ``T-ACME`` and ``oss`` →
    ``T-OSS``, we repoint ``oss``'s live ``team_id`` to a *third* workspace.
    The per-alias bind guard rejects ``oss`` with a ``ConfigError`` **before
    any fetch**, so no foreign message enters ``oss``'s namespace. Per the
    error-isolation contract (ADR-0041 §(b)) ``acme`` still syncs to
    completion, and the overall run exits non-zero naming the failed alias.
    """
    team_by_alias = two_workspace_env
    ts1 = "1700000001.000100"
    _patch_fetcher_per_team(
        monkeypatch,
        yields_by_team={
            _ACME_TEAM: [("C1", _raw_message(team_id=_ACME_TEAM, ts=ts1, text="a1"), ts1)],
            _OSS_TEAM: [("C1", _raw_message(team_id=_OSS_TEAM, ts=ts1, text="o1"), ts1)],
        },
    )
    runner = CliRunner()
    first = runner.invoke(app, ["slack", "sync"])
    assert first.exit_code == 0, first.stdout

    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        assert _row_count(engine, "sources") == 2

        # ---- Swap the oss alias's live workspace to a different team_id.
        # The cursor still has oss bound to T-OSS, so the bind guard
        # detects the mismatch on the next sync.
        team_by_alias["oss"] = "T-OTHER"

        # acme yields a new message; oss must be rejected before fetch, so
        # its (unused) yield carries the would-be-foreign team_id.
        ts2 = "1700000002.000200"
        _patch_fetcher_per_team(
            monkeypatch,
            yields_by_team={
                _ACME_TEAM: [("C1", _raw_message(team_id=_ACME_TEAM, ts=ts2, text="a2"), ts2)],
                "T-OTHER": [("C1", _raw_message(team_id="T-OTHER", ts=ts2, text="x"), ts2)],
            },
        )
        second = runner.invoke(app, ["slack", "sync"])
        # Aggregate failure → non-zero exit; the failed alias is named.
        assert second.exit_code == 1, second.stdout
        assert "oss" in second.stderr

        # acme advanced (error isolation): its new message landed.
        assert f"{_ACME_TEAM}:C1:{ts2}" in _external_ids(engine)
        # No foreign-workspace source entered the store — the guard ran
        # before fetch. Neither the swapped team_id nor oss's would-be new
        # message is present.
        assert f"T-OTHER:C1:{ts2}" not in _external_ids(engine)
        assert f"{_OSS_TEAM}:C1:{ts2}" not in _external_ids(engine)

        # oss's cursor stays bound to the original T-OSS (the guard did not
        # rebind it; recovery is an explicit `cursor reset`).
        cursor_value = _cursor_value(engine)
        assert cursor_value is not None
        parsed = json.loads(cursor_value)
        assert parsed["workspaces"]["oss"]["team_id"] == _OSS_TEAM
    finally:
        engine.dispose()


def test_two_workspace_alias_rename_refetches_idempotently(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
    two_workspace_env: dict[str, str],
) -> None:
    """Renaming an alias loses its cursor → cold-start re-fetch → no duplicate sources.

    The ``oss`` workspace is renamed to ``oss2`` (config table header +
    token + team_id stub all move to the new alias). The cursor nests under
    the *old* alias key, so the renamed alias cold-starts and re-fetches the
    same messages. Because ``external_id`` is keyed on the stable
    ``team_id`` (not the alias), the re-fetch is an idempotent upsert — the
    source count for the oss workspace does not grow (ADR-0041 §(a)).
    """
    team_by_alias = two_workspace_env
    ts1 = "1700000001.000100"
    _patch_fetcher_per_team(
        monkeypatch,
        yields_by_team={
            _ACME_TEAM: [("C1", _raw_message(team_id=_ACME_TEAM, ts=ts1, text="a1"), ts1)],
            _OSS_TEAM: [("C1", _raw_message(team_id=_OSS_TEAM, ts=ts1, text="o1"), ts1)],
        },
    )
    runner = CliRunner()
    first = runner.invoke(app, ["slack", "sync"])
    assert first.exit_code == 0, first.stdout

    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        assert _row_count(engine, "sources") == 2
        oss_external_id = f"{_OSS_TEAM}:C1:{ts1}"
        assert oss_external_id in _external_ids(engine)

        # ---- Rename oss → oss2: move the config table, the token, and the
        # team_id stub entry to the new alias. The cursor still nests the
        # workspace state under the old "oss" key, so "oss2" is unknown to
        # the cursor and cold-starts.
        monkeypatch.delenv("OPSHUB_CONNECTORS__SLACK__WORKSPACES__OSS__CHANNELS")
        monkeypatch.delenv("OPSHUB_CONNECTOR_SLACK_OSS_TOKEN")
        monkeypatch.setenv("OPSHUB_CONNECTORS__SLACK__WORKSPACES__OSS2__CHANNELS", '["C1"]')
        monkeypatch.setenv("OPSHUB_CONNECTOR_SLACK_OSS2_TOKEN", "xoxp-oss2")
        team_by_alias["oss2"] = _OSS_TEAM
        del team_by_alias["oss"]

        # The renamed alias re-fetches the *same* oss message (cold-start);
        # acme yields nothing new.
        _patch_fetcher_per_team(
            monkeypatch,
            yields_by_team={
                _OSS_TEAM: [("C1", _raw_message(team_id=_OSS_TEAM, ts=ts1, text="o1"), ts1)],
            },
        )
        second = runner.invoke(app, ["slack", "sync"])
        assert second.exit_code == 0, second.stdout

        # No duplicate source: the re-fetched oss message upserts on the
        # team_id-keyed external_id. Pre-ADR-0041 (alias-keyed external_id)
        # the rename would have orphaned the old source and minted a new
        # one → 3 sources.
        assert _row_count(engine, "sources") == 2
        assert oss_external_id in _external_ids(engine)

        # The cursor now carries the new alias key (cold-bound to the same
        # team_id). The stale "oss" entry is preserved verbatim — the
        # connector does not prune workspaces dropped from the config
        # (SlackCursorEnvelope docstring; explicit `cursor reset
        # --workspace oss` is the supported drop). The two entries point at
        # the same team_id, which is exactly why the re-fetch upserted
        # rather than duplicated.
        cursor_value = _cursor_value(engine)
        assert cursor_value is not None
        parsed = json.loads(cursor_value)
        assert "oss2" in parsed["workspaces"]
        assert parsed["workspaces"]["oss2"]["team_id"] == _OSS_TEAM
        assert parsed["workspaces"]["oss2"]["channels"] == {"C1": ts1}
        # Stale entry kept verbatim (still bound to the same workspace).
        assert parsed["workspaces"]["oss"]["team_id"] == _OSS_TEAM
    finally:
        engine.dispose()
