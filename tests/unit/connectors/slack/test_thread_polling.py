"""Tests for the Phase 20-C late-reply polling phase + activity window pruning.

Phase 20-C ([epic #465](https://github.com/ozzy-labs/opshub/issues/465),
ADR-0030 §(d) revised) splits Slack sync into two phases:

1. ``conversations.history`` (Phase 7 / 20-A semantics, unchanged) —
   yields top-level messages and the initial snapshot of every
   thread reply via ``conversations.replies``.
2. **Late-reply polling** (new, Phase 20-C) — for each thread already
   tracked in the ``threads`` axis of the compound cursor that is
   still inside the activity window, call
   ``conversations.replies(oldest=last_reply_ts)`` to pick up replies
   the workspace landed since the last sync. Threads whose
   ``last_reply_ts`` falls outside the window are pruned from the
   cursor so the threads axis stays bounded.

These tests pin the polling phase + window pruning at the unit level.
The fetcher's ``conversations.history`` path is replaced with a stub
that yields :class:`RawSlackMessage` rows so the polling phase is
isolated from the channel-history pagination logic; the fetcher's
``fetch_thread_replies`` method is also stubbed so we can drive
arbitrary thread-cursor states without spinning up a Slack mock.

The full end-to-end happy paths (with the real fetcher + a mocked
:class:`slack_sdk.WebClient`) live in the Phase 7 integration suite
:mod:`tests.integration.test_phase7_slack_sync`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip(
    "slack_sdk",
    reason="Slack connector tests require the 'connectors-slack' extras",
)

from opshub.connectors.context import ConnectorContext
from opshub.connectors.slack.connector import (
    SlackConnector,
    _dump_cursors,  # pyright: ignore[reportPrivateUsage]
    _load_cursors,  # pyright: ignore[reportPrivateUsage]
)
from opshub.connectors.slack.fetcher import RawSlackMessage
from opshub.core.config import (
    SLACK_DEFAULT_THREAD_ACTIVITY_WINDOW,
    OpsHubSettings,
)
from opshub.core.time import now_utc, since_to_ts

# ---------------------------------------------------------------------- helpers


class _RecordingSourceService:
    """Test double mirroring the :class:`SourceService` keyword-only signature."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.cursor_set_calls: list[dict[str, Any]] = []

    def observe(
        self,
        *,
        connector_name: str,
        external_id: str,
        source_type: str,
        title: str,
        url: str | None = None,
        summary: str | None = None,
        body: str | None = None,
        provenance_origin: str | None = None,
        provenance_trust: str | None = None,
        author_id: str | None = None,
    ) -> None:
        self.calls.append(
            {
                "connector_name": connector_name,
                "external_id": external_id,
                "source_type": source_type,
                "title": title,
                "url": url,
                "summary": summary,
                "body": body,
                "provenance_origin": provenance_origin,
                "provenance_trust": provenance_trust,
                "author_id": author_id,
            }
        )

    def cursor_set(
        self,
        connector_name: str,
        value: str | None,
        *,
        sync_started: bool = False,
    ) -> None:
        self.cursor_set_calls.append(
            {
                "connector_name": connector_name,
                "value": value,
                "sync_started": sync_started,
            }
        )


def _raw_message(
    *,
    team_id: str = "T-test",
    channel_id: str = "C1",
    channel_name: str = "general",
    ts: str,
    text: str = "hello",
    user_id: str = "U1",
    user_display_name: str = "alice",
    permalink: str = "https://acme.slack.com/archives/C1/p1",
    raw: dict[str, Any] | None = None,
    thread_ts: str | None = None,
) -> RawSlackMessage:
    return RawSlackMessage(
        team_id=team_id,
        channel_id=channel_id,
        channel_name=channel_name,
        ts=ts,
        text=text,
        user_id=user_id,
        user_display_name=user_display_name,
        permalink=permalink,
        raw=raw if raw is not None else {},
        thread_ts=thread_ts,
    )


def _context(
    service: _RecordingSourceService, *, cursor_value: str | None = None
) -> ConnectorContext:
    return ConnectorContext(
        source_service=service,
        cursor_value=cursor_value,
        secrets=None,
        logger=MagicMock(),
    )


def _dump_state(state: Any) -> str:
    """Wrap one workspace's state in the Phase 24-C envelope and serialise."""
    return _dump_cursors({"workspaces": {"acme": state}})


def _load_state(value: str | None) -> Any:
    """Parse a persisted cursor and unwrap the test workspace's state."""
    return _load_cursors(value)["workspaces"]["acme"]


def _patch_settings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    channels: list[str],
    sync_since: str | None = None,
    thread_activity_window: timedelta | None = None,
) -> None:
    """Patch :class:`OpsHubSettings` to return a controllable Slack section.

    Mirrors :func:`tests.unit.connectors.slack.test_connector._patch_settings`
    (Phase 24-C: ``channels`` lands under the single ``"acme"`` workspace
    table) but defaults the activity window to the production default
    (:data:`SLACK_DEFAULT_THREAD_ACTIVITY_WINDOW`) so tests opt into a
    narrower window only when they exercise the prune behaviour.
    """
    from opshub.core.config import SlackChannelSpec, SlackWorkspaceSettings

    workspace = SlackWorkspaceSettings(channels=[SlackChannelSpec(id=cid) for cid in channels])
    fake_settings = MagicMock()
    fake_settings.connectors.slack.workspaces = {"acme": workspace}
    fake_settings.connectors.slack.sync_since = sync_since
    fake_settings.connectors.slack.sync_workspace_filter = None
    fake_settings.connectors.slack.backfill_on_floor_lower = True
    fake_settings.connectors.slack.thread_activity_window = (
        thread_activity_window
        if thread_activity_window is not None
        else SLACK_DEFAULT_THREAD_ACTIVITY_WINDOW
    )
    monkeypatch.setattr(
        "opshub.core.config.OpsHubSettings",
        lambda: fake_settings,
    )


def _patch_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch :class:`SlackAuth` so construction never reads the keyring."""
    fake_auth_cls = MagicMock()
    fake_auth_cls.return_value.token = "xoxb-fake"
    # Phase 23-H (#538, ADR-0039): the sync hot path calls ``auth.test_token``
    # for the single-workspace bind guard; return a stable identity so the
    # guard binds ``T-test`` and the sync proceeds.
    fake_auth_cls.return_value.test_token.return_value = {
        "team": "t",
        "team_id": "T-test",
        "user": "u",
        "user_id": "U1",
        "principal": "bot",
    }
    monkeypatch.setattr(
        "opshub.connectors.slack.connector.SlackAuth",
        fake_auth_cls,
    )


def _patch_fetcher_with_thread_replies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    history_yields: list[tuple[str, RawSlackMessage, str | None]] | None = None,
    thread_replies: dict[tuple[str, str], list[RawSlackMessage]] | None = None,
) -> tuple[MagicMock, list[dict[str, Any]]]:
    """Patch :class:`SlackFetcher` for the 2-phase sync.

    ``history_yields`` is what Phase 1 (``conversations.history`` +
    initial thread snapshot) emits — same ``(channel_id, message,
    new_cursor)`` triple as :meth:`SlackFetcher.fetch_messages`.

    ``thread_replies`` keys ``(channel_id, thread_ts)`` to the list of
    :class:`RawSlackMessage` rows the polling phase yields. The fetcher
    stub records each ``fetch_thread_replies`` call (including the
    ``oldest_reply_ts`` kwarg) so tests can assert which threads were
    actually polled.
    """
    history_yields = history_yields or []
    thread_replies = thread_replies or {}
    polling_calls: list[dict[str, Any]] = []

    fake_fetcher_cls = MagicMock()

    def _fetch_messages(
        *,
        cursor_per_channel: dict[str, str | None],
        max_per_channel: int = 100,
        excludes: Any = None,
    ) -> Iterator[tuple[str, RawSlackMessage, str | None]]:
        del cursor_per_channel, max_per_channel, excludes
        return iter(history_yields)

    def _fetch_thread_replies(
        *,
        channel_id: str,
        thread_ts: str,
        oldest_reply_ts: str | None,
        channel_name: str | None = None,
    ) -> Iterator[RawSlackMessage]:
        del channel_name
        polling_calls.append(
            {
                "channel_id": channel_id,
                "thread_ts": thread_ts,
                "oldest_reply_ts": oldest_reply_ts,
            }
        )
        return iter(thread_replies.get((channel_id, thread_ts), []))

    fake_fetcher_cls.return_value.fetch_messages.side_effect = _fetch_messages
    fake_fetcher_cls.return_value.fetch_thread_replies.side_effect = _fetch_thread_replies
    monkeypatch.setattr(
        "opshub.connectors.slack.connector.SlackFetcher",
        fake_fetcher_cls,
    )
    return fake_fetcher_cls, polling_calls


# ---------------------------------------------------------------------- config


def test_thread_activity_window_default_is_30_days() -> None:
    """The Phase 20-C activity window default is 30 days (ADR-0030 §(d) revised).

    Pinned as a literal value so a future drift (e.g. someone bumps
    the default to 7d to chase a transient rate-limit complaint)
    surfaces in CI before it lands on operators.
    """
    assert SLACK_DEFAULT_THREAD_ACTIVITY_WINDOW == timedelta(days=30)
    # Constructed settings adopt the constant.
    settings = OpsHubSettings()
    assert settings.connectors.slack.thread_activity_window == timedelta(days=30)


def test_thread_activity_window_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """``OPSHUB_CONNECTORS__SLACK__THREAD_ACTIVITY_WINDOW=7d`` overrides the default.

    Mirrors the ``sync_since`` env-var override pattern. Accepts the
    ``"7d"`` / ``"4w"`` grammar uniform with the other Slack-side
    duration knobs.
    """
    monkeypatch.setenv("OPSHUB_CONNECTORS__SLACK__THREAD_ACTIVITY_WINDOW", "7d")
    settings = OpsHubSettings()
    assert settings.connectors.slack.thread_activity_window == timedelta(days=7)


def test_thread_activity_window_accepts_weeks_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    """``4w`` parses to 28 days (uniform with the ``parse_since`` grammar)."""
    monkeypatch.setenv("OPSHUB_CONNECTORS__SLACK__THREAD_ACTIVITY_WINDOW", "4w")
    settings = OpsHubSettings()
    assert settings.connectors.slack.thread_activity_window == timedelta(weeks=4)


def test_thread_activity_window_accepts_iso_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pydantic's native ISO 8601 duration coercion still works (``P30D``).

    Operators can use either the opshub ``"30d"`` grammar or the
    pydantic-native ISO 8601 ``"P30D"`` form. We don't promote the
    ISO form in docs (the ``"30d"`` style is uniform with
    ``sync_since``) but pinning both forms surfaces a regression that
    accidentally narrows the accept-list.
    """
    monkeypatch.setenv("OPSHUB_CONNECTORS__SLACK__THREAD_ACTIVITY_WINDOW", "P30D")
    settings = OpsHubSettings()
    assert settings.connectors.slack.thread_activity_window == timedelta(days=30)


# ---------------------------------------------------------------------- polling


def test_polling_phase_fetches_thread_replies_for_in_window_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A threads cursor entry inside the activity window is polled with ``oldest=last_reply_ts``.

    The contract: after Phase 1 (``conversations.history``) drains,
    every in-window thread is handed to
    :meth:`SlackFetcher.fetch_thread_replies` with the persisted
    ``last_reply_ts`` as the ``oldest_reply_ts`` kwarg. The polling
    phase yields are observed via :meth:`SourceService.observe` and
    advance the ``threads`` axis of the compound cursor.
    """
    _patch_settings(monkeypatch, channels=["C1"])
    _patch_auth(monkeypatch)

    # Set up an existing thread cursor inside the activity window
    # (last reply 1 day ago — well within the default 30d window).
    # The polling phase should call ``fetch_thread_replies(oldest=...)``
    # and observe each late reply.
    now_dt = now_utc()
    recent_last_reply_ts = since_to_ts(now_dt - timedelta(days=1))
    new_reply_ts = since_to_ts(now_dt - timedelta(hours=1))
    late_reply = _raw_message(
        ts=new_reply_ts,
        text="late reply",
        thread_ts="1700000010.000100",
    )
    _, polling_calls = _patch_fetcher_with_thread_replies(
        monkeypatch,
        history_yields=[],
        thread_replies={
            ("C1", "1700000010.000100"): [late_reply],
        },
    )

    prior_cursor = _dump_state(
        {
            "team_id": None,
            "channels": {"C1": "1700000010.000100"},
            "backfill": {},
            "threads": {"C1:1700000010.000100": recent_last_reply_ts},
        }
    )

    service = _RecordingSourceService()
    result = SlackConnector().sync(_context(service, cursor_value=prior_cursor))

    # The polling phase fired once with the persisted ``last_reply_ts``
    # as the ``oldest_reply_ts`` kwarg.
    assert polling_calls == [
        {
            "channel_id": "C1",
            "thread_ts": "1700000010.000100",
            "oldest_reply_ts": recent_last_reply_ts,
        }
    ]
    # The late reply was observed.
    assert result.observed_count == 1
    assert service.calls[0]["external_id"] == f"T-test:C1:{new_reply_ts}"
    # The threads cursor advanced to the new reply ts.
    parsed = _load_state(result.new_cursor)
    assert parsed["threads"] == {"C1:1700000010.000100": new_reply_ts}


def test_polling_phase_skips_out_of_window_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Threads whose ``last_reply_ts`` is older than the activity window are skipped.

    The threads cursor stays bounded by the operator-tunable window
    (default 30d). A cold thread (last reply > window ago) gets no
    polling round-trip and is pruned from the cursor at the end of
    the sync. This is the "limitation pin" mentioned in the Phase
    20-C plan: late replies on cold threads are intentionally
    out-of-scope.
    """
    _patch_settings(
        monkeypatch,
        channels=["C1"],
        thread_activity_window=timedelta(days=30),
    )
    _patch_auth(monkeypatch)

    # Build a cold threads-axis cursor: last reply 60 days ago, well
    # outside the 30-day window.
    cold_ts = since_to_ts(now_utc() - timedelta(days=60))
    cold_key = "C1:1700000010.000100"
    prior_cursor = _dump_state(
        {
            "team_id": None,
            "channels": {"C1": "1700000010.000100"},
            "backfill": {},
            "threads": {cold_key: cold_ts},
        }
    )

    _, polling_calls = _patch_fetcher_with_thread_replies(
        monkeypatch,
        history_yields=[],
        thread_replies={},
    )

    service = _RecordingSourceService()
    result = SlackConnector().sync(_context(service, cursor_value=prior_cursor))

    # The cold thread was not polled.
    assert polling_calls == []
    # The cold thread was pruned from the cursor.
    parsed = _load_state(result.new_cursor)
    assert cold_key not in parsed["threads"]


def test_new_parent_initialises_thread_cursor_at_latest_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 1 parent ingest seeds ``threads[channel:thread_ts] = latest_reply``.

    ADR-0030 §(d) revised: when a new parent with replies is ingested
    in Phase 1, the connector seeds the threads cursor at the
    parent's ``latest_reply`` so the next sync's polling phase does
    not re-fetch the snapshot Phase 20-A already yielded.
    """
    _patch_settings(monkeypatch, channels=["C1"])
    _patch_auth(monkeypatch)

    # A parent message with replies. The fetcher (Phase 20-A) sets
    # ``thread_ts == ts`` and exposes ``latest_reply`` on the raw
    # payload — that's our cursor seed. Use recent ts so the
    # prune phase doesn't immediately drop the freshly-seeded
    # entry (the seed is in-window).
    now_dt = now_utc()
    parent_ts = since_to_ts(now_dt - timedelta(days=1))
    latest_reply_ts = since_to_ts(now_dt - timedelta(hours=1))
    parent = _raw_message(
        ts=parent_ts,
        text="parent",
        thread_ts=parent_ts,
        raw={"latest_reply": latest_reply_ts},
    )
    _patch_fetcher_with_thread_replies(
        monkeypatch,
        history_yields=[("C1", parent, parent_ts)],
        thread_replies={},
    )

    service = _RecordingSourceService()
    result = SlackConnector().sync(_context(service, cursor_value=None))

    parsed = _load_state(result.new_cursor)
    # The threads axis gained a new entry seeded at ``latest_reply``,
    # so the next sync's polling phase will pass ``oldest=<latest_reply>``
    # and skip the snapshot replies.
    assert parsed["threads"] == {f"C1:{parent_ts}": latest_reply_ts}


def test_phase1_initial_snapshot_replies_advance_threads_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 1's initial reply snapshot also advances the threads axis.

    Phase 20-A yields replies via ``_iter_thread_replies`` as part of
    the main ``fetch_messages`` stream. Each reply has
    ``thread_ts != ts`` (it points at the parent). The connector
    must advance the threads cursor for those yields too, otherwise
    the next sync's polling phase would re-fetch every reply in the
    snapshot.
    """
    _patch_settings(monkeypatch, channels=["C1"])
    _patch_auth(monkeypatch)

    now_dt = now_utc()
    parent_ts = since_to_ts(now_dt - timedelta(days=1))
    reply_ts = since_to_ts(now_dt - timedelta(hours=1))
    parent = _raw_message(
        ts=parent_ts,
        text="parent",
        thread_ts=parent_ts,
        raw={"latest_reply": reply_ts},
    )
    reply = _raw_message(
        ts=reply_ts,
        text="snapshot reply",
        thread_ts=parent_ts,
    )
    _patch_fetcher_with_thread_replies(
        monkeypatch,
        history_yields=[
            ("C1", parent, parent_ts),
            # Phase 20-A: reply yielded under the parent's cursor anchor.
            ("C1", reply, parent_ts),
        ],
        thread_replies={},
    )

    service = _RecordingSourceService()
    result = SlackConnector().sync(_context(service, cursor_value=None))

    parsed = _load_state(result.new_cursor)
    # Threads cursor reflects ``max(latest_reply, reply.ts)`` — they
    # coincide here, but a reply newer than the parent's
    # ``latest_reply`` (hypothetical mid-page race) would still
    # advance the cursor monotonically.
    assert parsed["threads"] == {f"C1:{parent_ts}": reply_ts}


def test_polling_phase_advances_threads_cursor_monotonically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple late replies → cursor advances to the highest ts.

    Defense in depth for the issue #339 monotonicity contract: the
    threads cursor must end at the maximum ``ts`` across the polling
    yields, even if the fetcher hands back a regressing order.
    """
    _patch_settings(monkeypatch, channels=["C1"])
    _patch_auth(monkeypatch)

    now_dt = now_utc()
    # ``late_a`` is newer than ``late_b`` even though the fetcher
    # stub yields them in reverse order — the connector must end at
    # ``late_a.ts`` regardless.
    ts_late_a = since_to_ts(now_dt - timedelta(hours=1))
    ts_late_b = since_to_ts(now_dt - timedelta(hours=2))
    ts_prior = since_to_ts(now_dt - timedelta(days=1))
    late_a = _raw_message(
        ts=ts_late_a,
        text="late-a",
        thread_ts="1700000010.000100",
    )
    late_b = _raw_message(
        ts=ts_late_b,  # ts < late_a, deliberately out of order
        text="late-b",
        thread_ts="1700000010.000100",
    )
    _patch_fetcher_with_thread_replies(
        monkeypatch,
        history_yields=[],
        thread_replies={("C1", "1700000010.000100"): [late_a, late_b]},
    )

    prior_cursor = _dump_state(
        {
            "team_id": None,
            "channels": {"C1": "1700000010.000100"},
            "backfill": {},
            "threads": {"C1:1700000010.000100": ts_prior},
        }
    )

    service = _RecordingSourceService()
    result = SlackConnector().sync(_context(service, cursor_value=prior_cursor))

    parsed = _load_state(result.new_cursor)
    # ``_max_ts`` keeps the cursor at the highest yielded ts.
    assert parsed["threads"] == {"C1:1700000010.000100": ts_late_a}


def test_polling_phase_skipped_for_thread_in_excluded_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A thread in an excluded channel does not consume polling budget.

    Mirrors the Phase 1 short-circuit: when a channel is in
    :class:`Excludes.channels`, every message there is already
    filtered out before observe. The polling phase reads the same
    excludes file so the budget-saving guard is uniform across
    phases.
    """
    _patch_settings(monkeypatch, channels=["C1"])
    _patch_auth(monkeypatch)

    # Build an excludes file that drops channel C1 (the only channel
    # in the test settings).
    from opshub.core.excludes import excludes_path as _excludes_path

    excludes_path_value = _excludes_path()
    excludes_path_value.parent.mkdir(parents=True, exist_ok=True)
    original_contents = excludes_path_value.read_text() if excludes_path_value.exists() else None
    excludes_path_value.write_text("channels:\n  - C1\nsenders: []\n")
    try:
        # Use a recent ts so the in-window guard alone is satisfied;
        # the excludes guard is what we're pinning here.
        recent_ts = since_to_ts(now_utc() - timedelta(days=1))
        prior_cursor = _dump_state(
            {
                "team_id": None,
                "channels": {"C1": "1700000010.000100"},
                "backfill": {},
                "threads": {"C1:1700000010.000100": recent_ts},
            }
        )

        _, polling_calls = _patch_fetcher_with_thread_replies(
            monkeypatch,
            history_yields=[],
            thread_replies={},
        )

        service = _RecordingSourceService()
        SlackConnector().sync(_context(service, cursor_value=prior_cursor))

        # No polling call for the excluded channel's thread.
        assert polling_calls == []
    finally:
        if original_contents is None:
            excludes_path_value.unlink(missing_ok=True)
        else:
            excludes_path_value.write_text(original_contents)


def test_thread_activity_window_overrides_default_via_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator-overridden window (7d) prunes earlier than the 30d default.

    A thread with a reply 14 days ago is in-window under the default
    30d window but out-of-window under an operator-set 7d override.
    Pinning that the connector reads the override from
    :class:`SlackConnectorSettings.thread_activity_window` rather
    than hard-coding the default.
    """
    _patch_settings(
        monkeypatch,
        channels=["C1"],
        thread_activity_window=timedelta(days=7),
    )
    _patch_auth(monkeypatch)

    fortnight_ts = since_to_ts(now_utc() - timedelta(days=14))
    prior_cursor = _dump_state(
        {
            "team_id": None,
            "channels": {"C1": "1700000010.000100"},
            "backfill": {},
            "threads": {"C1:1700000010.000100": fortnight_ts},
        }
    )

    _, polling_calls = _patch_fetcher_with_thread_replies(
        monkeypatch,
        history_yields=[],
        thread_replies={},
    )

    service = _RecordingSourceService()
    result = SlackConnector().sync(_context(service, cursor_value=prior_cursor))

    # 7d window → the 14-day-old thread is cold; no polling, pruned.
    assert polling_calls == []
    parsed = _load_state(result.new_cursor)
    assert parsed["threads"] == {}


def test_cli_flag_overrides_thread_activity_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``opshub slack sync --thread-activity-window=7d`` overrides the config default.

    The CLI flag sets ``OPSHUB_CONNECTORS__SLACK__THREAD_ACTIVITY_WINDOW``
    on the process environment before calling the shared sync driver,
    so :class:`OpsHubSettings` picks up the override on the next
    construction. Pinning the env-var path keeps the operator-facing
    flag a thin shim around the documented settings surface.
    """
    import os

    from typer.testing import CliRunner

    from opshub.cli.app import app

    # Short-circuit the shared driver so the test only exercises the
    # CLI's env-var setting code path, not the full sync stack. We
    # capture the env-var state at call time to confirm the CLI set
    # it before delegating to the driver.
    captured: dict[str, str | None] = {}

    def _fake_run(name: str) -> None:
        del name
        captured["env_value"] = os.environ.get("OPSHUB_CONNECTORS__SLACK__THREAD_ACTIVITY_WINDOW")

    monkeypatch.setattr("opshub.cli.slack.run_connector_sync", _fake_run, raising=False)
    # The CLI does ``from opshub.cli._connector_common import run_connector_sync``
    # inside the callback, so we need to patch the import target too.
    monkeypatch.setattr("opshub.cli._connector_common.run_connector_sync", _fake_run)

    runner = CliRunner()
    try:
        result = runner.invoke(app, ["slack", "sync", "--thread-activity-window=7d"])
        assert result.exit_code == 0, result.stdout
        assert captured["env_value"] == "7d"
    finally:
        # The CLI's ``os.environ[...] = "7d"`` is a documented design
        # choice (see :mod:`opshub.cli.slack`) but bypasses
        # ``monkeypatch.setenv``, so we explicitly drop the override
        # at test teardown to keep sibling tests hermetic.
        os.environ.pop("OPSHUB_CONNECTORS__SLACK__THREAD_ACTIVITY_WINDOW", None)


def test_cli_no_backfill_flag_sets_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``opshub slack sync --no-backfill`` sets the backfill-disable env var.

    Phase 22-D (ADR-0038): the flag is a thin process-local shim around
    ``OPSHUB_CONNECTORS__SLACK__BACKFILL_ON_FLOOR_LOWER`` (mirrors the
    ``--thread-activity-window`` env-var path), so the connector picks up
    ``backfill_on_floor_lower=false`` on the next ``OpsHubSettings``
    construction. Absent the flag, no env var is set (default ``True``).
    """
    import os

    from typer.testing import CliRunner

    from opshub.cli.app import app

    captured: dict[str, str | None] = {}

    def _fake_run(name: str) -> None:
        del name
        captured["env_value"] = os.environ.get("OPSHUB_CONNECTORS__SLACK__BACKFILL_ON_FLOOR_LOWER")

    monkeypatch.setattr("opshub.cli.slack.run_connector_sync", _fake_run, raising=False)
    monkeypatch.setattr("opshub.cli._connector_common.run_connector_sync", _fake_run)

    runner = CliRunner()
    try:
        # Without the flag → env var untouched (default behaviour).
        result = runner.invoke(app, ["slack", "sync"])
        assert result.exit_code == 0, result.stdout
        assert captured["env_value"] is None

        # With the flag → env var set to "false".
        result = runner.invoke(app, ["slack", "sync", "--no-backfill"])
        assert result.exit_code == 0, result.stdout
        assert captured["env_value"] == "false"
    finally:
        os.environ.pop("OPSHUB_CONNECTORS__SLACK__BACKFILL_ON_FLOOR_LOWER", None)


def test_polling_phase_treats_null_last_reply_as_in_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A threads cursor entry with ``None`` value gets one polling round.

    Defensive: a thread whose cursor was registered but has not
    observed a reply yet (``None`` value) is treated as in-window so
    the next sync's polling phase gets one chance to fetch replies.
    Pruning would otherwise lose the registration silently.
    """
    _patch_settings(monkeypatch, channels=["C1"])
    _patch_auth(monkeypatch)

    prior_cursor = _dump_state(
        {
            "team_id": None,
            "channels": {"C1": "1700000010.000100"},
            "backfill": {},
            "threads": {"C1:1700000010.000100": None},
        }
    )

    _, polling_calls = _patch_fetcher_with_thread_replies(
        monkeypatch,
        history_yields=[],
        thread_replies={},
    )

    service = _RecordingSourceService()
    SlackConnector().sync(_context(service, cursor_value=prior_cursor))

    # Polling fires once with ``oldest_reply_ts=None`` (cold fetch).
    assert polling_calls == [
        {
            "channel_id": "C1",
            "thread_ts": "1700000010.000100",
            "oldest_reply_ts": None,
        }
    ]


# -------------------------------------------------- "all" sentinel (Phase 20-E)
#
# Phase 20-E ([#478](https://github.com/ozzy-labs/opshub/issues/478)) added
# the ``thread_activity_window = "all"`` sentinel (case-insensitive) that
# coerces to ``None`` so the late-reply polling prune is disabled wholesale.
# ``docs/troubleshooting.md`` §3.12 and ``docs/upgrading.md`` §Phase 20 both
# promised this spelling ahead of the validator catching up; pre-Phase-20-E
# operators who set ``thread_activity_window = "all"`` per the doc were met
# with a startup-time ``ConfigError`` from the regex-based validator. These
# tests pin the recovery: the sentinel is accepted at every layer (config /
# CLI / connector pruning helper), prune is fully disabled, and every cold
# thread cursor is preserved across syncs.


@pytest.mark.parametrize("spelling", ["all", "All", "ALL", " all ", "aLl"])
def test_thread_activity_window_all_sentinel_coerces_to_none(
    spelling: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``thread_activity_window = "all"`` (any case-variant) coerces to ``None``.

    Pin the env-var path (the documented operator surface) end-to-end:
    pydantic settings → validator → resolved
    :class:`SlackConnectorSettings.thread_activity_window`. ``None`` is
    the connector-side sentinel for "disable prune"; the
    :func:`_prune_inactive_threads` / :func:`_window_cutoff_ts` helpers
    short-circuit on it.
    """
    monkeypatch.setenv(
        "OPSHUB_CONNECTORS__SLACK__THREAD_ACTIVITY_WINDOW",
        spelling,
    )
    settings = OpsHubSettings()
    assert settings.connectors.slack.thread_activity_window is None


def test_prune_inactive_threads_skips_everything_when_window_disabled() -> None:
    """``_prune_inactive_threads`` with ``cutoff_ts=None`` keeps every entry.

    The ``thread_activity_window = "all"`` path coerces the cutoff to
    ``None`` (via :func:`_window_cutoff_ts`); the prune helper must
    treat that as a no-op so even cold threads (last reply > 1 year ago
    relative to the default 30d window) stay in the cursor.
    """
    from opshub.connectors.slack.connector import (
        _prune_inactive_threads,  # pyright: ignore[reportPrivateUsage]
        _window_cutoff_ts,  # pyright: ignore[reportPrivateUsage]
    )

    ancient_ts = since_to_ts(now_utc() - timedelta(days=365))
    recent_ts = since_to_ts(now_utc() - timedelta(days=1))
    cursors: dict[str, str | None] = {
        "C1:thread-ancient": ancient_ts,
        "C1:thread-recent": recent_ts,
        "C1:thread-null": None,
    }
    expected = dict(cursors)

    _prune_inactive_threads(cursors, _window_cutoff_ts(None))

    # Every entry — even the 1-year-old cold thread — is preserved.
    assert cursors == expected


def test_polling_phase_preserves_all_threads_when_window_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``thread_activity_window = None`` (the ``"all"`` sentinel), prune is skipped.

    A cold thread (last reply 60 days ago, well outside the default
    30d window) would normally be skipped on the polling phase and
    pruned from the cursor. The ``None`` window (``"all"`` sentinel)
    flips that: the polling phase still fires for every thread, and
    no entry is dropped from the cursor.
    """
    # ``_patch_settings`` conflates ``thread_activity_window=None`` with
    # "use the default" via its ternary; we want the literal ``None``
    # (the connector-side spelling of the ``"all"`` sentinel), so we
    # call the helper first then override the attribute directly.
    _patch_settings(monkeypatch, channels=["C1"])
    from opshub.core.config import OpsHubSettings

    OpsHubSettings().connectors.slack.thread_activity_window = None
    _patch_auth(monkeypatch)

    cold_ts = since_to_ts(now_utc() - timedelta(days=60))
    cold_key = "C1:1700000010.000100"
    prior_cursor = _dump_state(
        {
            "team_id": None,
            "channels": {"C1": "1700000010.000100"},
            "backfill": {},
            "threads": {cold_key: cold_ts},
        }
    )

    _, polling_calls = _patch_fetcher_with_thread_replies(
        monkeypatch,
        history_yields=[],
        thread_replies={},
    )

    service = _RecordingSourceService()
    result = SlackConnector().sync(_context(service, cursor_value=prior_cursor))

    # Polling fires for the cold thread (window disabled).
    assert polling_calls == [
        {
            "channel_id": "C1",
            "thread_ts": "1700000010.000100",
            "oldest_reply_ts": cold_ts,
        }
    ]
    # Cursor entry is preserved (no prune).
    parsed = _load_state(result.new_cursor)
    assert parsed["threads"] == {cold_key: cold_ts}


def test_cli_flag_accepts_all_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``opshub slack sync --thread-activity-window all`` propagates verbatim.

    The CLI forwards the raw string to
    ``OPSHUB_CONNECTORS__SLACK__THREAD_ACTIVITY_WINDOW``; the validator
    then coerces ``"all"`` to ``None``. The flag itself only owns the
    typer surface — pin that the documented spelling reaches the env
    var unchanged (operator typo / case-folding only matters once
    pydantic picks it up).
    """
    import os

    from typer.testing import CliRunner

    from opshub.cli.app import app

    captured: dict[str, str | None] = {}

    def _fake_run(name: str) -> None:
        del name
        captured["env_value"] = os.environ.get("OPSHUB_CONNECTORS__SLACK__THREAD_ACTIVITY_WINDOW")

    monkeypatch.setattr("opshub.cli.slack.run_connector_sync", _fake_run, raising=False)
    monkeypatch.setattr("opshub.cli._connector_common.run_connector_sync", _fake_run)

    runner = CliRunner()
    try:
        result = runner.invoke(app, ["slack", "sync", "--thread-activity-window=all"])
        assert result.exit_code == 0, result.stdout
        assert captured["env_value"] == "all"
    finally:
        os.environ.pop("OPSHUB_CONNECTORS__SLACK__THREAD_ACTIVITY_WINDOW", None)
