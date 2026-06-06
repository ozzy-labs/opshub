"""Tests for :class:`opshub.connectors.slack.connector.SlackConnector`.

These tests pin the connector-level contract independently of the
end-to-end CLI lifecycle (covered by the integration suite):

1. ``name`` matches the registry key the CLI dispatches on.
2. Cursor round-trip via JSON is symmetric — what the connector
   writes back on one sync, the next sync parses without loss.
3. Empty cursor / first-sync path treats ``context.cursor_value =
   None`` as "no resume" and yields the fetcher an empty dict.
4. Empty channel list is a structured warning + no-op (matches the
   GitHub "empty sync preserves prior cursor" contract).
5. Mapper output is forwarded to :meth:`SourceService.observe`
   verbatim — no field is dropped or rewritten by the connector.

The :mod:`slack_sdk` extras are gated with ``pytest.importorskip``
because :class:`RawSlackMessage` lives in the same package as the SDK
import path (even though the connector itself only uses the SDK
indirectly via the fetcher).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
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
    _max_ts,  # pyright: ignore[reportPrivateUsage]
)
from opshub.connectors.slack.fetcher import RawSlackMessage
from opshub.core.errors import ConfigError

# ---------------------------------------------------------------------- helpers


class _RecordingSourceService:
    """Test double for :class:`SourceService` that records ``observe`` calls.

    Mirrors the keyword-only signature used by the real service so a
    drift on argument names trips a TypeError immediately.

    The partial-progress checkpoint added for issue #339 Bug 2 calls
    :meth:`cursor_set` mid-sync; we record those calls separately so
    tests can assert "the connector wrote a partial cursor before
    re-raising" without spinning up the full SQLite-backed service.
    """

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
            }
        )

    def cursor_set(
        self,
        connector_name: str,
        value: str | None,
        *,
        sync_started: bool = False,
    ) -> None:
        """Record a ``cursor_set`` call without persisting anything.

        Mirrors the real :meth:`SourceService.cursor_set` signature so
        a connector-side argument drift trips a ``TypeError`` here
        rather than silently no-op'ing.
        """
        self.cursor_set_calls.append(
            {
                "connector_name": connector_name,
                "value": value,
                "sync_started": sync_started,
            }
        )


def _raw_message(
    *,
    channel_id: str = "C1",
    channel_name: str = "general",
    ts: str = "1700000001.000100",
    text: str = "hello",
    user_id: str = "U1",
    user_display_name: str = "alice",
    permalink: str = "https://acme.slack.com/archives/C1/p1700000001000100",
) -> RawSlackMessage:
    return RawSlackMessage(
        channel_id=channel_id,
        channel_name=channel_name,
        ts=ts,
        text=text,
        user_id=user_id,
        user_display_name=user_display_name,
        permalink=permalink,
        raw={},
    )


def _context(
    service: _RecordingSourceService, *, cursor_value: str | None = None
) -> ConnectorContext:
    return ConnectorContext(
        source_service=service,
        cursor_value=cursor_value,
        secrets=None,
        logger=MagicMock(),  # warning() needs to be callable on the empty-channel path
    )


def _patch_settings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    channels: list[str],
) -> None:
    """Patch :class:`OpsHubSettings` so ``_resolve_channels`` returns ``channels``.

    The connector lazy-imports ``OpsHubSettings`` from
    ``opshub.core.config`` *inside* :meth:`_resolve_channels`; patching
    a name on the connector module would silently fail (the import
    binds the name fresh inside the method). Patching the class on
    its defining module catches the lazy import via the usual
    monkeypatch lookup.
    """
    fake_settings = MagicMock()
    fake_settings.connectors.slack.channels = channels
    monkeypatch.setattr(
        "opshub.core.config.OpsHubSettings",
        lambda: fake_settings,
    )


def _patch_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch :class:`SlackAuth` so construction never reads the keyring.

    The connector instantiates :class:`SlackAuth` inside :meth:`sync`;
    we replace it with a no-arg double that returns a token-bearing
    fake. The fetcher (also patched) doesn't actually use the token
    — it's just plumbing.
    """
    fake_auth_cls = MagicMock()
    fake_auth_cls.return_value.token = "xoxb-fake"
    monkeypatch.setattr(
        "opshub.connectors.slack.connector.SlackAuth",
        fake_auth_cls,
    )


def _patch_fetcher(
    monkeypatch: pytest.MonkeyPatch,
    *,
    yields: list[tuple[str, RawSlackMessage, str | None]],
) -> tuple[MagicMock, dict[str, dict[str, str | None]]]:
    """Patch :class:`SlackFetcher` so :meth:`fetch_messages` yields ``yields``.

    Returns ``(fetcher_cls_mock, captured_kwargs_dict)``. The captured
    dict has a single ``"cursor_per_channel"`` key whose value is a
    **snapshot** of the dict passed to :meth:`fetch_messages` at call
    time. The connector advances the cursor dict in-place as it
    yields messages, so :class:`MagicMock` ``call_args`` would
    otherwise reflect the post-iteration state — losing the
    information the test wants to assert (the *initial* resume
    state). A snapshot taken inside the side_effect preserves it.
    """
    fake_fetcher_cls = MagicMock()
    captured: dict[str, dict[str, str | None]] = {}

    def _fetch_messages(
        *,
        cursor_per_channel: dict[str, str | None],
        max_per_channel: int = 100,
        excludes: Any = None,
    ) -> Iterator[tuple[str, RawSlackMessage, str | None]]:
        # ADR-0030 (#466): the connector forwards the resolved
        # ``ExcludeRules`` filter so the real fetcher can short-circuit
        # ``conversations.replies`` calls for excluded parents. The
        # unit-test mock accepts and ignores it — the connector's
        # per-yield ``excludes`` arm still runs on the yielded
        # ``RawSlackMessage`` rows, so the contract observed by these
        # tests is unchanged.
        del max_per_channel, excludes
        captured["cursor_per_channel"] = dict(cursor_per_channel)
        return iter(yields)

    fake_fetcher_cls.return_value.fetch_messages.side_effect = _fetch_messages
    monkeypatch.setattr(
        "opshub.connectors.slack.connector.SlackFetcher",
        fake_fetcher_cls,
    )
    return fake_fetcher_cls, captured


# ---------------------------------------------------------------------- name


def test_connector_name_is_slack() -> None:
    """The registry / CLI dispatch key must be exactly ``"slack"``."""
    assert SlackConnector.name == "slack"
    assert SlackConnector().name == "slack"


# ---------------------------------------------------------------------- cursor helpers


def test_load_cursors_none_returns_empty_dict() -> None:
    """First-sync (``cursor_value=None``) → empty resume dict.

    Empty dict tells the fetcher "no per-channel resume" and it pulls
    the most recent N messages per channel.
    """
    assert _load_cursors(None) == {}


def test_load_cursors_round_trips_dict() -> None:
    """``_dump → _load`` preserves the cursor dict exactly.

    The serialised form is the only thing the projection persists, so
    losing data on the round-trip would silently re-fetch history on
    every resume. Pinning the symmetry locks the format.
    """
    original: dict[str, str | None] = {
        "C1": "1700000001.000100",
        "C2": "1700000002.000200",
    }
    serialised = _dump_cursors(original)
    assert _load_cursors(serialised) == original


def test_load_cursors_accepts_null_values() -> None:
    """A channel with a ``null`` value (never observed) round-trips.

    A first-sync that found no messages in channel ``C-empty`` leaves
    its entry as ``None`` so the next sync still passes the key to
    the fetcher (helps a future operator-facing diff between
    "channel missing from cursor" vs. "channel observed nothing").
    """
    original: dict[str, str | None] = {"C1": "1700000001.000100", "C-empty": None}
    assert _load_cursors(_dump_cursors(original)) == original


def test_load_cursors_rejects_malformed_json() -> None:
    """Hand-edited / corrupt cursor → :class:`ConfigError`, not silent re-fetch.

    A silently-truncated history (the only alternative) would loudly
    mislead the operator. A hard error tells them to rebuild the
    projection.
    """
    with pytest.raises(ConfigError, match="not valid JSON"):
        _load_cursors("not-json")


def test_load_cursors_rejects_non_dict_root() -> None:
    """Cursor must be a JSON **object** — list / string root rejected."""
    with pytest.raises(ConfigError, match="must be a JSON object"):
        _load_cursors('["C1"]')


def test_load_cursors_rejects_non_string_values() -> None:
    """Values must be ``str | None``; int / bool reject as hand-edit accident."""
    with pytest.raises(ConfigError, match="must be strings or null"):
        _load_cursors('{"C1": 42}')


def test_max_ts_returns_candidate_when_prior_is_none() -> None:
    """First observation for a channel → ``_max_ts(None, ts) == ts``.

    Defense-in-depth helper introduced for issue #339; the load-bearing
    invariant is that a freshly-observed message overwrites a
    placeholder ``None`` (channel previously had no cursor entry).
    """
    assert _max_ts(None, "1700000001.000100") == "1700000001.000100"


def test_max_ts_keeps_prior_when_candidate_is_older() -> None:
    """Out-of-order yield (older candidate) → prior wins; cursor never rewinds.

    The pre-#339 connector wrote ``cursors[ch] = new_cursor``
    unconditionally and so a fetcher that yielded an older ts after
    a newer one would silently rewind the persisted cursor. The
    ``_max_ts`` guard is the connector-level defense against that
    regression class.
    """
    assert _max_ts("1700000002.000200", "1700000001.000100") == "1700000002.000200"


def test_max_ts_picks_candidate_when_newer() -> None:
    """In-order yield (newer candidate) → candidate wins; cursor advances."""
    assert _max_ts("1700000001.000100", "1700000002.000200") == "1700000002.000200"


def test_max_ts_returns_prior_when_candidate_is_none() -> None:
    """``None`` candidate (channel drained nothing this run) → prior preserved."""
    assert _max_ts("1700000001.000100", None) == "1700000001.000100"


def test_max_ts_returns_candidate_when_both_sides_non_numeric() -> None:
    """Defensive fallback: both operands non-numeric → ``float()`` raises and
    the ``except (TypeError, ValueError)`` arm returns ``candidate``.

    The fetcher's ``_ts_key`` + ``if not ts: continue`` skip-arm
    normally drops malformed ``ts`` strings before they reach the
    connector, so this branch is purely defensive — but it is
    load-bearing for forensic visibility: if the fetcher contract
    ever drifts and starts yielding non-numeric values, we want the
    connector to record *some* cursor (the candidate) rather than
    silently lose progress to the prior. Pinning the fallback also
    means a future hardening that raises here instead of returning
    will fail this test loudly, prompting an ADR-grade conversation
    rather than a silent semantic flip.

    Audit followup for #345 (PR 1 of #339) — see
    ``connector.py:281-282`` for the source-of-truth comment.
    """
    # Both sides parse-fail → fallback returns candidate.
    assert _max_ts("abc", "xyz") == "xyz"


def test_max_ts_returns_candidate_when_prior_non_numeric_but_candidate_numeric() -> None:
    """Defensive fallback: prior is non-numeric → comparison raises and
    candidate is returned regardless of whether candidate parses.

    The ``try`` block does ``float(candidate) >= float(prior)``; if
    ``prior`` is unparseable the whole comparison raises
    :class:`ValueError` and the except-arm returns the candidate.
    Pins the behaviour so a future refactor that re-orders the
    operands (or short-circuits) is caught.
    """
    assert _max_ts("abc", "1700000001.000100") == "1700000001.000100"


def test_max_ts_returns_candidate_when_candidate_non_numeric_but_prior_numeric() -> None:
    """Defensive fallback: candidate is non-numeric → fallback still returns
    candidate (not prior).

    This is the asymmetric arm: the ``except`` arm unconditionally
    yields ``candidate`` rather than picking the parseable operand.
    Documented behaviour (see connector docstring): "the connector
    still records some progress rather than silently dropping the
    new value". Pinning prevents a "fix" that quietly switches the
    fallback to prior — which would mean a malformed-ts regression
    would stall the cursor indefinitely.
    """
    assert _max_ts("1700000001.000100", "abc") == "abc"


def test_dump_cursors_is_deterministic() -> None:
    """``sort_keys=True`` → identical input dicts produce identical output strings.

    Determinism matters because the projection row's ``cursor_value``
    becomes a meaningful diff in operator dashboards — a non-stable
    ordering would mark every sync run as "changed" even when no new
    messages arrived.
    """
    a: dict[str, str | None] = {"C2": "ts-2", "C1": "ts-1"}
    b: dict[str, str | None] = {"C1": "ts-1", "C2": "ts-2"}
    assert _dump_cursors(a) == _dump_cursors(b)


# ---------------------------------------------------------------------- sync: happy path


def test_sync_observes_each_yielded_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: fetcher yields → mapper → observe → cursor advances per yield.

    Pins the three-step pipeline so a regression that drops the
    ``observe(**kwargs)`` splat or forgets to advance the cursor
    surfaces immediately.
    """
    _patch_settings(monkeypatch, channels=["C1"])
    _patch_auth(monkeypatch)
    msg_a = _raw_message(ts="1700000001.000100", text="first")
    msg_b = _raw_message(ts="1700000002.000200", text="second")
    fetcher_cls, captured = _patch_fetcher(
        monkeypatch,
        yields=[
            ("C1", msg_a, "1700000001.000100"),
            ("C1", msg_b, "1700000002.000200"),
        ],
    )

    service = _RecordingSourceService()
    result = SlackConnector().sync(_context(service, cursor_value=None))

    # The fetcher was built with the configured channel list and
    # called with an empty resume dict (first-sync semantics).
    fetcher_cls.assert_called_once()
    assert fetcher_cls.call_args.kwargs == {"channels": ["C1"]}
    # ``captured`` holds the cursor dict snapshot taken at fetch_messages
    # entry, before the connector mutates it in-place per yield.
    assert captured["cursor_per_channel"] == {}

    # Both messages reached the service; mapped fields match the
    # mapper contract.
    assert result.observed_count == 2
    assert [c["external_id"] for c in service.calls] == [
        "C1:1700000001.000100",
        "C1:1700000002.000200",
    ]
    assert [c["summary"] for c in service.calls] == ["first", "second"]
    assert all(c["connector_name"] == "slack" for c in service.calls)
    assert all(c["source_type"] == "slack_message" for c in service.calls)

    # New cursor encodes the latest ts per channel.
    assert result.new_cursor is not None
    assert _load_cursors(result.new_cursor) == {"C1": "1700000002.000200"}


def test_sync_resumes_from_persisted_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    """The persisted JSON cursor is parsed and handed to the fetcher.

    Without this, every sync would re-fetch the channel's full
    history. Pinning the kwargs argument shape prevents a regression
    that loses the resume value.
    """
    _patch_settings(monkeypatch, channels=["C1", "C2"])
    _patch_auth(monkeypatch)
    _, captured = _patch_fetcher(monkeypatch, yields=[])

    prior_cursor = _dump_cursors({"C1": "1700000001.000100", "C2": None})
    service = _RecordingSourceService()
    result = SlackConnector().sync(_context(service, cursor_value=prior_cursor))

    assert captured["cursor_per_channel"] == {
        "C1": "1700000001.000100",
        "C2": None,
    }
    # No yields → observed_count=0; cursor stays at the prior value.
    assert result.observed_count == 0
    assert result.new_cursor == prior_cursor


def test_sync_persists_max_ts_when_yield_order_regresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense-in-depth: a fetcher that yields out-of-order does not rewind the cursor.

    Regression guard for the cursor-rewind half of issue #339. The
    fetcher (post-#339 fix) yields ts-ascending across pages, but
    the connector additionally guards the persisted cursor with
    ``cursors[ch] = _max_ts(prior, yielded)`` so a future fetcher
    bug that yields an older ``ts`` after a newer one cannot
    overwrite the projection cursor with a stale value (which would
    re-trigger the issue #339 inbox-inflation cascade on the next
    sync).

    The mock fetcher here deliberately yields the **newest** message
    first, then the **older** one — the exact failure mode the
    pre-#339 ``cursors[ch] = new_cursor`` line exhibited. With the
    ``_max_ts`` guard the persisted cursor stays at the newer ts.
    """
    _patch_settings(monkeypatch, channels=["C1"])
    _patch_auth(monkeypatch)
    msg_new = _raw_message(channel_id="C1", ts="1700000002.000200", text="newer")
    msg_old = _raw_message(channel_id="C1", ts="1700000001.000100", text="older")
    _patch_fetcher(
        monkeypatch,
        yields=[
            # Deliberately out-of-order: newer first, older second.
            ("C1", msg_new, "1700000002.000200"),
            ("C1", msg_old, "1700000001.000100"),
        ],
    )

    service = _RecordingSourceService()
    result = SlackConnector().sync(_context(service, cursor_value=None))

    # Both messages still reach the service (the connector does not
    # itself drop out-of-order yields — that's the fetcher's
    # responsibility post-#339).
    assert result.observed_count == 2
    # The persisted cursor is the maximum ts despite the regression
    # in yield order — this is the load-bearing invariant against
    # issue #339 inbox inflation.
    assert result.new_cursor is not None
    assert _load_cursors(result.new_cursor) == {"C1": "1700000002.000200"}


def test_sync_advances_cursor_per_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two channels with new messages → both cursors advance independently."""
    _patch_settings(monkeypatch, channels=["C1", "C2"])
    _patch_auth(monkeypatch)
    _patch_fetcher(
        monkeypatch,
        yields=[
            ("C1", _raw_message(channel_id="C1", ts="ts-c1-new"), "ts-c1-new"),
            ("C2", _raw_message(channel_id="C2", ts="ts-c2-new"), "ts-c2-new"),
        ],
    )

    service = _RecordingSourceService()
    result = SlackConnector().sync(_context(service, cursor_value=None))

    assert result.observed_count == 2
    assert result.new_cursor is not None
    assert _load_cursors(result.new_cursor) == {
        "C1": "ts-c1-new",
        "C2": "ts-c2-new",
    }


# ---------------------------------------------------------------------- sync: empty channels


def test_sync_with_empty_channels_warns_and_returns_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty channel config → structured warning + cursor preserved.

    The connector must not crash here — an operator who hasn't
    populated ``channels`` yet should see a CLI-level warning, not a
    stack trace. The prior cursor is preserved so flipping
    ``channels = ["C1"]`` and re-running picks up exactly where the
    last sync left off.
    """
    _patch_settings(monkeypatch, channels=[])
    # No fetcher patch — the connector must short-circuit before
    # touching it. A pollution check confirms the short-circuit:
    fetcher_cls = MagicMock(side_effect=AssertionError("fetcher must not be constructed"))
    monkeypatch.setattr("opshub.connectors.slack.connector.SlackFetcher", fetcher_cls)
    auth_cls = MagicMock(side_effect=AssertionError("auth must not be constructed"))
    monkeypatch.setattr("opshub.connectors.slack.connector.SlackAuth", auth_cls)

    service = _RecordingSourceService()
    ctx = _context(service, cursor_value="{}")
    result = SlackConnector().sync(ctx)

    assert result.observed_count == 0
    assert result.new_cursor == "{}"
    assert service.calls == []
    # Warning was emitted via the structured logger.
    ctx.logger.warning.assert_called_once()


# ---------------------------------------------------------------------- sync: no yields


def _patch_excludes_yaml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, body: str) -> None:
    """Write an ``excludes.yaml`` and point ``default_config_dir`` at it.

    ``load_excludes()`` resolves the file through
    :func:`opshub.core.config.default_config_dir` (imported into
    ``opshub.core.excludes`` at module top); monkeypatching that name
    on the excludes module is the documented way to redirect resolution
    for tests (see also :func:`tests.unit.core.test_excludes` for the
    ``config_dir=`` kwarg pattern; here we exercise the no-arg call
    site the Phase 10 audit Cluster 3 mandates).
    """
    cfg_dir = tmp_path / "opshub-config"
    cfg_dir.mkdir()
    (cfg_dir / "excludes.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setattr("opshub.core.excludes.default_config_dir", lambda: cfg_dir)


def test_sync_skips_excluded_channel_but_advances_cursor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ADR-0020 §(b): a message in an excluded channel is never observed.

    The cursor still advances past the message so the connector does
    not re-fetch it forever. Pins the "skip but advance" semantics
    explicitly — a regression that early-``continue``\\s without
    updating the cursor dict would re-fetch the excluded channel on
    every sync.
    """
    _patch_excludes_yaml(monkeypatch, tmp_path, body="channels:\n  - C-secret\n")
    _patch_settings(monkeypatch, channels=["C1", "C-secret"])
    _patch_auth(monkeypatch)
    public_msg = _raw_message(channel_id="C1", ts="ts-1", text="public")
    secret_msg = _raw_message(channel_id="C-secret", ts="ts-2", text="leaked")
    _patch_fetcher(
        monkeypatch,
        yields=[
            ("C1", public_msg, "ts-1"),
            ("C-secret", secret_msg, "ts-2"),
        ],
    )

    service = _RecordingSourceService()
    result = SlackConnector().sync(_context(service, cursor_value=None))

    # Only the public channel's message reaches the source service.
    assert result.observed_count == 1
    assert len(service.calls) == 1
    assert service.calls[0]["external_id"] == "C1:ts-1"
    # Both channels' cursors advance (skip-but-advance contract).
    assert result.new_cursor is not None
    assert _load_cursors(result.new_cursor) == {"C1": "ts-1", "C-secret": "ts-2"}


def test_sync_skips_excluded_sender_but_advances_cursor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ADR-0020 §(b): a message from an excluded sender is never observed.

    Same "skip but advance" semantics as the channel-exclude path.
    """
    _patch_excludes_yaml(monkeypatch, tmp_path, body="senders:\n  - U-bot\n")
    _patch_settings(monkeypatch, channels=["C1"])
    _patch_auth(monkeypatch)
    bot_msg = _raw_message(channel_id="C1", ts="ts-bot", user_id="U-bot", text="noise")
    human_msg = _raw_message(channel_id="C1", ts="ts-human", user_id="U1", text="real")
    _patch_fetcher(
        monkeypatch,
        yields=[
            ("C1", bot_msg, "ts-bot"),
            ("C1", human_msg, "ts-human"),
        ],
    )

    service = _RecordingSourceService()
    result = SlackConnector().sync(_context(service, cursor_value=None))

    assert result.observed_count == 1
    assert service.calls[0]["external_id"] == "C1:ts-human"
    assert result.new_cursor is not None
    assert _load_cursors(result.new_cursor) == {"C1": "ts-human"}


def test_sync_with_no_yields_preserves_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configured channels but zero new messages → cursor unchanged.

    Mirrors :func:`test_empty_sync_preserves_cursor` in the GitHub
    connector test suite — "no progress, no movement" is the
    documented contract.
    """
    _patch_settings(monkeypatch, channels=["C1"])
    _patch_auth(monkeypatch)
    _patch_fetcher(monkeypatch, yields=[])

    prior_cursor = _dump_cursors({"C1": "ts-old"})
    service = _RecordingSourceService()
    result = SlackConnector().sync(_context(service, cursor_value=prior_cursor))

    assert result.observed_count == 0
    assert result.new_cursor == prior_cursor
    assert service.calls == []


# --------------------------------------------- sync: partial-progress checkpoint (issue #339 Bug 2)


def _patch_fetcher_with_mid_iteration_error(
    monkeypatch: pytest.MonkeyPatch,
    *,
    yields_before_error: list[tuple[str, RawSlackMessage, str | None]],
    error: BaseException,
) -> dict[str, dict[str, str | None]]:
    """Patch :class:`SlackFetcher` so ``fetch_messages`` yields N tuples then raises.

    The yields happen first (the connector advances ``cursors`` for
    each), then the generator raises ``error``. Mirrors the realistic
    failure shape: the fetcher made it part-way through a paginated
    history before Slack returned (e.g.) ``rate_limited`` with the
    retry budget exhausted, or a manual ``KeyboardInterrupt`` arrived.

    Returns the snapshot of the initial ``cursor_per_channel`` so
    tests can assert the connector was wired correctly.
    """
    fake_fetcher_cls = MagicMock()
    captured: dict[str, dict[str, str | None]] = {}

    def _fetch_messages(
        *,
        cursor_per_channel: dict[str, str | None],
        max_per_channel: int = 100,
        excludes: Any = None,
    ) -> Iterator[tuple[str, RawSlackMessage, str | None]]:
        # See the sibling ``_patch_fetcher`` mock for the ``excludes``
        # kwarg rationale (ADR-0030 / #466).
        del max_per_channel, excludes
        captured["cursor_per_channel"] = dict(cursor_per_channel)
        yield from yields_before_error
        raise error

    fake_fetcher_cls.return_value.fetch_messages.side_effect = _fetch_messages
    monkeypatch.setattr(
        "opshub.connectors.slack.connector.SlackFetcher",
        fake_fetcher_cls,
    )
    return captured


def test_sync_checkpoints_partial_cursor_on_mid_iteration_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #339 Bug 2: mid-iteration exception → partial cursor persists.

    Pre-fix the connector returned only on the success path, so a
    fetcher exception left the CLI driver without a ``new_cursor``
    to write — the projection stayed at the prior-run value while
    ``observe`` had already committed the N successful messages.
    On retry the same N messages were re-fetched / re-enqueued,
    inflating ``inbox_items`` per aborted-then-retried sync.

    Post-fix the ``try/finally`` arm in :meth:`SlackConnector.sync`
    writes the accumulated ``cursors`` dict via
    ``cursor_set(sync_started=True)`` before the exception
    propagates, so the projection cursor reflects the
    actually-committed messages. The exception itself still
    propagates verbatim (the CLI driver maps it to
    :class:`ConnectorSyncFailed`); we only insert the cursor
    write, we do not swallow.
    """
    from opshub.core.errors import ConnectorFailedError

    _patch_settings(monkeypatch, channels=["C1"])
    _patch_auth(monkeypatch)
    msg_a = _raw_message(channel_id="C1", ts="1700000001.000100", text="first")
    msg_b = _raw_message(channel_id="C1", ts="1700000002.000200", text="second")
    _patch_fetcher_with_mid_iteration_error(
        monkeypatch,
        yields_before_error=[
            ("C1", msg_a, "1700000001.000100"),
            ("C1", msg_b, "1700000002.000200"),
        ],
        error=ConnectorFailedError("Slack fetch failed for channel C1: rate_limited"),
    )

    service = _RecordingSourceService()
    with pytest.raises(ConnectorFailedError):
        SlackConnector().sync(_context(service, cursor_value=None))

    # Both messages reached observe before the crash.
    assert [c["external_id"] for c in service.calls] == [
        "C1:1700000001.000100",
        "C1:1700000002.000200",
    ]
    # Exactly one ``cursor_set`` call, fired by the connector's
    # finally arm with the partial-progress cursor encoded as JSON.
    # ``sync_started=True`` so the projection's started-event
    # reducer upserts ``cursor_value`` (see
    # :meth:`ConnectorCursorsProjection._apply_started`).
    assert len(service.cursor_set_calls) == 1
    call = service.cursor_set_calls[0]
    assert call["connector_name"] == "slack"
    assert call["sync_started"] is True
    assert call["value"] is not None
    # The persisted JSON contains the latest ts observed before the
    # crash — pre-fix this would have been missing entirely.
    assert _load_cursors(call["value"]) == {"C1": "1700000002.000200"}


def test_sync_no_checkpoint_when_failure_yields_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure with zero observed messages → no checkpoint event noise.

    The ``finally`` arm guards on ``cursors != cursors_at_entry``
    so a fetcher that raises before yielding anything (e.g.
    ``invalid_auth`` on the first ``conversations.history`` call)
    does not emit a redundant ``ConnectorSyncStarted`` event with a
    no-op cursor value. The CLI driver's ``record_sync_failure``
    arm is still responsible for the audit trail; we just stay out
    of its way.
    """
    from opshub.core.errors import ConnectorFailedError

    _patch_settings(monkeypatch, channels=["C1"])
    _patch_auth(monkeypatch)
    _patch_fetcher_with_mid_iteration_error(
        monkeypatch,
        yields_before_error=[],
        error=ConnectorFailedError("Slack fetch failed for channel C1: invalid_auth"),
    )

    service = _RecordingSourceService()
    with pytest.raises(ConnectorFailedError):
        SlackConnector().sync(_context(service, cursor_value=None))

    # No observes (fetcher raised on its first call) and no
    # cursor_set noise — the connector cleanly delegates the failure
    # to the CLI driver.
    assert service.calls == []
    assert service.cursor_set_calls == []


def test_sync_no_checkpoint_when_failure_yields_only_excluded_messages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Excluded messages still advance the cursor — so a mid-iteration crash
    after only-excluded yields *does* trigger a checkpoint.

    The skip-but-advance contract (pinned by
    :func:`test_sync_skips_excluded_channel_but_advances_cursor`)
    means even excluded yields mutate ``cursors``. The
    ``cursors != cursors_at_entry`` guard therefore treats them as
    progress for the checkpoint purpose: on retry we want to skip
    re-fetching them, same as observed messages.

    This is the symmetric counterpart to
    :func:`test_sync_no_checkpoint_when_failure_yields_nothing`:
    "no yields at all → no checkpoint" vs "yields happened (even
    if filtered) → checkpoint".
    """
    from opshub.core.errors import ConnectorFailedError

    _patch_excludes_yaml(monkeypatch, tmp_path, body="channels:\n  - C-secret\n")
    _patch_settings(monkeypatch, channels=["C-secret", "C1"])
    _patch_auth(monkeypatch)
    excluded_msg = _raw_message(channel_id="C-secret", ts="1700000001.000100", text="leaked")
    _patch_fetcher_with_mid_iteration_error(
        monkeypatch,
        yields_before_error=[
            ("C-secret", excluded_msg, "1700000001.000100"),
        ],
        error=ConnectorFailedError("Slack fetch failed for channel C1: rate_limited"),
    )

    service = _RecordingSourceService()
    with pytest.raises(ConnectorFailedError):
        SlackConnector().sync(_context(service, cursor_value=None))

    # Excluded message never reached observe.
    assert service.calls == []
    # But the cursor did advance, so the checkpoint fires — the
    # next sync skips re-fetching the excluded message.
    assert len(service.cursor_set_calls) == 1
    call = service.cursor_set_calls[0]
    assert call["sync_started"] is True
    assert _load_cursors(call["value"]) == {"C-secret": "1700000001.000100"}


def test_sync_no_checkpoint_on_normal_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: ``finally`` arm is a no-op when the loop completes normally.

    The CLI driver writes the terminal ``ConnectorSyncCompleted``
    event with the same cursor value the partial checkpoint would
    have written — so firing the checkpoint here would just emit a
    redundant ``ConnectorSyncStarted`` event for no operator
    benefit. The connector guards on ``completed_normally`` (set
    just before the ``try`` arm exits) to keep the happy-path
    event log clean.
    """
    _patch_settings(monkeypatch, channels=["C1"])
    _patch_auth(monkeypatch)
    msg_a = _raw_message(channel_id="C1", ts="1700000001.000100", text="first")
    _patch_fetcher(
        monkeypatch,
        yields=[("C1", msg_a, "1700000001.000100")],
    )

    service = _RecordingSourceService()
    result = SlackConnector().sync(_context(service, cursor_value=None))

    # observe fired exactly once; cursor_set did NOT fire (the CLI
    # driver will write the terminal completed event).
    assert len(service.calls) == 1
    assert service.cursor_set_calls == []
    # The success-path return value still encodes the new cursor.
    assert result.observed_count == 1
    assert result.new_cursor is not None
    assert _load_cursors(result.new_cursor) == {"C1": "1700000001.000100"}


def test_sync_checkpoints_partial_progress_on_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``KeyboardInterrupt`` mid-iteration → partial cursor still persists.

    ``KeyboardInterrupt`` is a :class:`BaseException` (not
    :class:`Exception`), so a naive ``try / except Exception`` would
    miss it. The ``try / finally`` shape used in the fix covers
    both branches uniformly — pinning the ``BaseException`` path
    here prevents a future refactor from narrowing the catch
    clause and re-opening the manual-abort half of the cascade.
    """
    _patch_settings(monkeypatch, channels=["C1"])
    _patch_auth(monkeypatch)
    msg_a = _raw_message(channel_id="C1", ts="1700000001.000100", text="first")
    _patch_fetcher_with_mid_iteration_error(
        monkeypatch,
        yields_before_error=[("C1", msg_a, "1700000001.000100")],
        error=KeyboardInterrupt(),
    )

    service = _RecordingSourceService()
    with pytest.raises(KeyboardInterrupt):
        SlackConnector().sync(_context(service, cursor_value=None))

    # The partial-progress checkpoint fired despite the
    # BaseException-derived interrupt.
    assert len(service.cursor_set_calls) == 1
    call = service.cursor_set_calls[0]
    assert _load_cursors(call["value"]) == {"C1": "1700000001.000100"}


def test_sync_checkpoint_preserves_prior_cursor_for_unprocessed_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mid-iteration crash → partial cursor merges prior + new progress.

    A two-channel sync where channel A had a prior cursor and
    channel B drained partially before crashing must persist the
    union: A's prior cursor (untouched) + B's partial cursor (new).
    Pre-fix B's progress was lost; channel A's was preserved only
    by accident because the cursor map starts as a copy of the
    resume state and the CLI's failure arm does not touch the
    cursor projection.

    Pinning the union here means a future refactor that, e.g.,
    starts ``cursors = {}`` instead of ``cursors = _load_cursors(...)``
    immediately fails this test — which would otherwise be a
    silent data-loss regression for the "channel A had no new
    messages this run" case.
    """
    from opshub.core.errors import ConnectorFailedError

    _patch_settings(monkeypatch, channels=["A", "B"])
    _patch_auth(monkeypatch)
    msg_b = _raw_message(channel_id="B", ts="ts-b-new", text="B's first")
    _patch_fetcher_with_mid_iteration_error(
        monkeypatch,
        yields_before_error=[("B", msg_b, "ts-b-new")],
        error=ConnectorFailedError("Slack fetch failed for channel B: rate_limited"),
    )

    prior_cursor = _dump_cursors({"A": "ts-a-prior", "B": None})
    service = _RecordingSourceService()
    with pytest.raises(ConnectorFailedError):
        SlackConnector().sync(_context(service, cursor_value=prior_cursor))

    # The checkpoint persists both channels' state — A's prior
    # cursor untouched, B's partial cursor advanced.
    assert len(service.cursor_set_calls) == 1
    call = service.cursor_set_calls[0]
    assert _load_cursors(call["value"]) == {"A": "ts-a-prior", "B": "ts-b-new"}


# ---------------------------------------------------------------------- registry


def test_slack_subpackage_registers_connector() -> None:
    """Importing :mod:`opshub.connectors.slack` registers the connector.

    Mirrors the Phase 3 GitHub precedent — the CLI driver discovers
    connectors purely through the registry, so the import side
    effect is the contract that makes ``opshub slack sync``
    resolve.

    We :func:`importlib.reload` the package after a registry reset so
    the test is robust against earlier tests that may have called
    :func:`unregister_all` (see ``tests/unit/cli/test_connector.py``
    autouse fixture). Reload re-runs the module body and re-fires
    the ``register_connector(SlackConnector())`` side effect at the
    bottom of the module — that's the contract under test.
    """
    import importlib

    import opshub.connectors.slack
    from opshub.connectors import discover_connectors, unregister_all

    unregister_all()
    importlib.reload(opshub.connectors.slack)

    names = {c.name for c in discover_connectors()}
    assert "slack" in names
