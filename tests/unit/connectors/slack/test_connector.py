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
from datetime import timedelta
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
    SlackCursorState,
    _dump_cursors,  # pyright: ignore[reportPrivateUsage]
    _floor_to_ts,  # pyright: ignore[reportPrivateUsage]
    _load_cursors,  # pyright: ignore[reportPrivateUsage]
    _max_ts,  # pyright: ignore[reportPrivateUsage]
    _resolve_floors,  # pyright: ignore[reportPrivateUsage]
)
from opshub.connectors.slack.fetcher import RawSlackMessage
from opshub.core.config import SlackChannelSpec
from opshub.core.errors import ConfigError, ConnectorFailedError
from opshub.core.time import now_utc, parse_since, since_to_ts

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
    channels: list[str | Any],
    sync_since: str | None = None,
    thread_activity_window: timedelta | None = None,
    backfill_on_floor_lower: bool = True,
) -> None:
    """Patch :class:`OpsHubSettings` so ``_resolve_slack_settings`` returns ``channels``.

    The connector lazy-imports ``OpsHubSettings`` from
    ``opshub.core.config`` *inside* :meth:`_resolve_slack_settings`;
    patching a name on the connector module would silently fail (the
    import binds the name fresh inside the method). Patching the class on
    its defining module catches the lazy import via the usual
    monkeypatch lookup.

    Bare id strings in ``channels`` are coerced to
    :class:`SlackChannelSpec` (mirroring the pydantic before-validator)
    so call sites that only care about ids stay terse; pass a
    :class:`SlackChannelSpec` directly to exercise per-channel ``since``
    (Phase 20, ADR-0036). ``sync_since`` sets the connector-wide floor.
    ``thread_activity_window`` overrides the Phase 20-C late-reply
    polling window (default = the production default 30d).
    """
    from opshub.core.config import (
        SLACK_DEFAULT_THREAD_ACTIVITY_WINDOW,
        SlackChannelSpec,
    )

    specs = [c if isinstance(c, SlackChannelSpec) else SlackChannelSpec(id=c) for c in channels]
    fake_settings = MagicMock()
    fake_settings.connectors.slack.channels = specs
    fake_settings.connectors.slack.sync_since = sync_since
    fake_settings.connectors.slack.thread_activity_window = (
        thread_activity_window
        if thread_activity_window is not None
        else SLACK_DEFAULT_THREAD_ACTIVITY_WINDOW
    )
    # Phase 22-D: a real bool (not a MagicMock attribute, which is
    # truthy) so the gap-backfill toggle behaves deterministically.
    fake_settings.connectors.slack.backfill_on_floor_lower = backfill_on_floor_lower
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


def test_load_cursors_none_returns_empty_compound() -> None:
    """First-sync (``cursor_value=None``) → empty compound (both axes empty).

    Phase 20-B: ``_load_cursors`` always returns the compound shape
    with both ``channels`` and ``threads`` keys present. An empty
    compound tells the fetcher "no per-channel resume" (empty
    ``channels`` axis) and tells the late-reply polling path (Phase
    20-C) "no thread to poll" (empty ``threads`` axis).
    """
    state = _load_cursors(None)
    assert state == {"channels": {}, "backfill": {}, "threads": {}}
    # All three axes must be present and mutable for the sync path to
    # mutate them in-place without raising KeyError (Phase 22-B added the
    # ``backfill`` low-water axis, ADR-0038 §(a)).
    assert "channels" in state
    assert "backfill" in state
    assert "threads" in state


def test_load_cursors_round_trips_compound_schema() -> None:
    """``_dump → _load`` preserves the compound cursor exactly.

    The serialised form is the only thing the projection persists, so
    losing data on the round-trip would silently re-fetch history /
    skip late replies on every resume. Pinning the symmetry locks the
    Phase 20-B format.
    """
    original: SlackCursorState = {
        "channels": {
            "C1": "1700000001.000100",
            "C2": "1700000002.000200",
        },
        "backfill": {
            "C1": "1699990000.000000",
        },
        "threads": {
            "C1:1700000000.000000": "1700000003.000300",
        },
    }
    serialised = _dump_cursors(original)
    assert _load_cursors(serialised) == original


def test_load_cursors_accepts_empty_axes() -> None:
    """Either axis may be empty — first sync, threads not yet populated, etc.

    Pre-20-C the sync path always emits an empty ``threads`` axis, so
    the parser must accept it without complaint. Symmetrically a
    workspace whose first sync drained zero channels still emits an
    empty ``channels`` axis — also valid.
    """
    # Empty channels, populated threads (hypothetical mid-state). The
    # input omits ``backfill`` (pre-Phase-22 shape) — tolerated and
    # defaulted to empty (ADR-0038 §(g)).
    state_a = _load_cursors('{"channels":{}, "backfill": {},"threads":{"C1:t1":"ts-1"}}')
    assert state_a == {"channels": {}, "backfill": {}, "threads": {"C1:t1": "ts-1"}}

    # Populated channels, empty threads (the Phase 20-B steady state).
    state_b = _load_cursors('{"channels":{"C1":"ts-1"}, "backfill": {},"threads":{}}')
    assert state_b == {"channels": {"C1": "ts-1"}, "backfill": {}, "threads": {}}

    # Both empty (first sync, zero observed).
    state_c = _load_cursors('{"channels":{}, "backfill": {},"threads":{}}')
    assert state_c == {"channels": {}, "backfill": {}, "threads": {}}


def test_load_cursors_accepts_null_values_per_axis() -> None:
    """A channel / thread with a ``null`` value (never observed) round-trips.

    A first-sync that found no messages in channel ``C-empty`` leaves
    its entry as ``None`` so the next sync still passes the key to
    the fetcher. Same applies to threads on the late-reply polling
    path: a parent that has been registered but whose replies haven't
    been observed yet sits at ``None``.
    """
    original: SlackCursorState = {
        "channels": {"C1": "1700000001.000100", "C-empty": None},
        "backfill": {"C1": "1699990000.000000", "C-empty": None},
        "threads": {"C1:1700000000.000000": None},
    }
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


def test_load_cursors_rejects_legacy_flat_dict_schema() -> None:
    """Pre-20-B flat shape (``{channel_id: ts}``) → ConfigError with rebuild prompt.

    Phase 20-B is a hard schema flip ([epic #465](
    https://github.com/ozzy-labs/opshub/issues/465)): the pre-20-B
    flat ``{channel_id: ts}`` shape is rejected with a migration
    prompt pointing at ``opshub projections rebuild``. opshub is
    pre-userbase so we do not silently coerce — coercion would lose
    the operator-facing migration moment and would also be subtly
    wrong (the legacy ``ts`` was the per-channel max including
    thread replies *that were never observed*, so a 1:1 lift into the
    new ``channels`` axis breaks Phase 20-C's increment semantics).
    """
    legacy = '{"C1":"1700000001.000100","C2":"1700000002.000200"}'
    with pytest.raises(ConfigError, match="opshub projections rebuild"):
        _load_cursors(legacy)


def test_load_cursors_rejects_legacy_message_mentions_pre_phase_20b() -> None:
    """The rejection message names the schema flip so the operator can grep ADRs.

    Pinning the wording (substring match) keeps the error message
    discoverable from the ADR / epic side — an operator who reads
    ``docs/upgrading.md`` or the epic body looking for the rebuild
    prompt should find the same phrase here.
    """
    legacy = '{"C1":"ts-1"}'
    with pytest.raises(ConfigError, match="pre-Phase-20-B"):
        _load_cursors(legacy)


def test_load_cursors_legacy_message_matches_canonical_doc_text() -> None:
    """Reject text matches the canonical doc string (Phase 20-E audit).

    ``docs/troubleshooting.md`` §3.12 and ``docs/upgrading.md`` §Phase 20
    each render the error string verbatim as the "typical error" the
    operator will grep against. Phase 20-E
    ([#478](https://github.com/ozzy-labs/opshub/issues/478)) aligned the
    implementation to the documented spelling — both files are the SSOT
    for the message ("doc is canonical"); this test pins both the
    individual fragments and the underlying operator surface
    (``opshub projections rebuild``) so a future paraphrase has to
    update the docs first.
    """
    legacy = '{"C1":"ts-1"}'
    with pytest.raises(ConfigError) as excinfo:
        _load_cursors(legacy)
    message = str(excinfo.value)
    # Fragments lifted directly from the doc surface.
    assert "Slack cursor envelope is pre-Phase-20-B (flat dict)." in message
    assert "`opshub projections rebuild`" in message
    assert '{"channels": ..., "threads": ...} compound schema' in message
    assert "opshub is pre-userbase and ships no silent migration" in message
    # ADR cross-reference so operators can grep ADR-0030 from the error.
    assert "ADR-0030" in message


def test_load_cursors_rejects_missing_channels_axis() -> None:
    """Compound schema requires the ``channels`` axis — drop → ConfigError."""
    with pytest.raises(ConfigError, match="opshub projections rebuild"):
        _load_cursors('{"threads":{}}')


def test_load_cursors_rejects_missing_threads_axis() -> None:
    """Compound schema requires the ``threads`` axis — drop → ConfigError."""
    with pytest.raises(ConfigError, match="opshub projections rebuild"):
        _load_cursors('{"channels":{}}')


def test_load_cursors_rejects_non_object_axis() -> None:
    """Each axis must be a JSON object — list / scalar reject."""
    with pytest.raises(ConfigError, match="'channels' axis must be a JSON object"):
        _load_cursors('{"channels":["C1"],"threads":{}}')
    with pytest.raises(ConfigError, match="'threads' axis must be a JSON object"):
        _load_cursors('{"channels":{}, "backfill": {},"threads":"oops"}')


def test_load_cursors_rejects_non_string_values_per_axis() -> None:
    """Values must be ``str | None``; int / bool reject as hand-edit accident."""
    with pytest.raises(ConfigError, match="'channels' axis values must be"):
        _load_cursors('{"channels":{"C1":42}, "backfill": {},"threads":{}}')
    with pytest.raises(ConfigError, match="'threads' axis values must be"):
        _load_cursors('{"channels":{}, "backfill": {},"threads":{"C1:t1":true}}')


def test_load_cursors_tolerates_missing_backfill_axis() -> None:
    """Phase 22-B: a pre-Phase-22 cursor (no ``backfill`` axis) is accepted.

    The ``backfill`` low-water axis ([ADR-0038](
    https://github.com/ozzy-labs/opshub/issues/516) §(a)) is **additive**:
    a 2-axis cursor persisted before Phase 22 lacks it. Unlike the
    pre-20-B flat dict, a missing ``backfill`` key is unambiguous, and
    ``opshub projections rebuild`` does NOT reset the cursor (ADR-0038
    §Context), so a ConfigError migration prompt would be a dead-end.
    We default the absent axis to empty rather than raising.
    """
    state = _load_cursors('{"channels":{"C1":"ts-1"}, "backfill": {},"threads":{"C1:t1":"ts-r1"}}')
    assert state == {
        "channels": {"C1": "ts-1"},
        "backfill": {},
        "threads": {"C1:t1": "ts-r1"},
    }


def test_load_cursors_round_trips_backfill_axis() -> None:
    """A populated ``backfill`` axis survives ``_dump → _load`` (incl. null)."""
    original: SlackCursorState = {
        "channels": {"C1": "1700000001.000100"},
        "backfill": {"C1": "1699990000.000000", "C-cold": None},
        "threads": {},
    }
    assert _load_cursors(_dump_cursors(original)) == original


def test_load_cursors_rejects_non_object_backfill_axis() -> None:
    """The ``backfill`` axis, when present, must be a JSON object."""
    with pytest.raises(ConfigError, match="'backfill' axis must be a JSON object"):
        _load_cursors('{"channels":{},"threads":{},"backfill":["C1"]}')


def test_load_cursors_rejects_non_string_backfill_values() -> None:
    """``backfill`` axis values must be ``str | None`` (reject int / bool)."""
    with pytest.raises(ConfigError, match="'backfill' axis values must be"):
        _load_cursors('{"channels":{},"threads":{},"backfill":{"C1":42}}')


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
    """``sort_keys=True`` → identical compound inputs produce identical output strings.

    Determinism matters because the projection row's ``cursor_value``
    becomes a meaningful diff in operator dashboards — a non-stable
    ordering would mark every sync run as "changed" even when no new
    messages arrived. The Phase 20-B compound shape recurses into both
    axes, so this test pins both top-level (``channels`` before
    ``threads``) and per-axis (``C1`` before ``C2``) ordering.
    """
    a: SlackCursorState = {
        "channels": {"C2": "ts-2", "C1": "ts-1"},
        "backfill": {"C2": "ts-b2", "C1": "ts-b1"},
        "threads": {"C2:t2": "ts-r2", "C1:t1": "ts-r1"},
    }
    b: SlackCursorState = {
        "channels": {"C1": "ts-1", "C2": "ts-2"},
        "backfill": {"C1": "ts-b1", "C2": "ts-b2"},
        "threads": {"C1:t1": "ts-r1", "C2:t2": "ts-r2"},
    }
    assert _dump_cursors(a) == _dump_cursors(b)


def test_dump_cursors_emits_sorted_top_level_axes() -> None:
    """Phase 20-B: top-level keys are emitted alphabetically (channels, threads).

    Pins the exact serialised shape so a downstream consumer (operator
    dashboard, ``opshub projections inspect``) can diff JSON values
    textually without re-parsing. Drift on the top-level order would
    silently invalidate textual diffs across runs.
    """
    state: SlackCursorState = {
        "channels": {"C1": "ts-1"},
        "backfill": {"C1": "ts-b1"},
        "threads": {"C1:t1": "ts-r1"},
    }
    # ``sort_keys=True`` orders top-level axes alphabetically:
    # backfill < channels < threads.
    assert _dump_cursors(state) == (
        '{"backfill":{"C1":"ts-b1"},"channels":{"C1":"ts-1"},"threads":{"C1:t1":"ts-r1"}}'
    )


def test_dump_cursors_emits_empty_compound_envelope() -> None:
    """First-sync (no observations yet) still emits the schema marker.

    Phase 20-B writes the compound envelope even when both axes are
    empty so subsequent ``_load_cursors`` calls see a valid schema
    (and the ``connector_cursors`` row advances out of the legacy /
    NULL state on the first successful sync).
    """
    assert _dump_cursors({"channels": {}, "backfill": {}, "threads": {}}) == (
        '{"backfill":{},"channels":{},"threads":{}}'
    )


def test_max_ts_works_on_threads_axis_keys() -> None:
    """Phase 20-B: ``_max_ts`` is axis-agnostic — same helper for both axes.

    The threads axis keys are ``"channel_id:thread_ts"`` strings, but
    ``_max_ts`` compares the *values* (Slack ts strings) which are
    identical in shape to the channels axis values. This test pins
    the axis-agnostic contract so 20-C can reuse ``_max_ts`` for the
    per-thread ``conversations.replies(oldest=...)`` cursor advance.
    """
    # Both candidate and prior are Slack-format ts strings, the same
    # shape used on the channels axis.
    assert _max_ts("1700000001.000100", "1700000002.000200") == "1700000002.000200"
    assert _max_ts("1700000002.000200", "1700000001.000100") == "1700000002.000200"


# --------------------------------------------------- Phase 20: date floor resolution (ADR-0036)


def test_floor_to_ts_none_is_no_floor() -> None:
    """``since = None`` (inherit / unset) → no floor."""
    assert _floor_to_ts(None) is None


def test_floor_to_ts_all_sentinel_is_no_floor() -> None:
    """``since = "all"`` → full backfill (no floor), never fed to the date parser."""
    assert _floor_to_ts("all") is None


def test_floor_to_ts_absolute_date_returns_ts() -> None:
    """An ISO date floor renders to the same ``ts`` the cursor comparison uses."""
    assert _floor_to_ts("2026-01-01") == since_to_ts(parse_since("2026-01-01"))


def test_resolve_floors_precedence() -> None:
    """Channel ``since`` overrides global ``sync_since``; absent inherits; ``all`` opts out."""
    specs = [
        SlackChannelSpec(id="C_INHERIT"),  # inherits global
        SlackChannelSpec(id="C_OVERRIDE", since="2025-06-01"),  # own floor
        SlackChannelSpec(id="C_ALL", since="all"),  # explicit full backfill
    ]
    floors = _resolve_floors(specs, sync_since="2020-01-01")

    assert floors["C_INHERIT"] == since_to_ts(parse_since("2020-01-01"))
    assert floors["C_OVERRIDE"] == since_to_ts(parse_since("2025-06-01"))
    assert floors["C_ALL"] is None


def test_resolve_floors_no_global_no_channel_is_no_floor() -> None:
    """No ``sync_since`` and no per-channel ``since`` → no floor (legacy full backfill)."""
    floors = _resolve_floors([SlackChannelSpec(id="C1")], sync_since=None)
    assert floors == {"C1": None}


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

    # New cursor encodes the latest ts per channel under the compound schema.
    assert result.new_cursor is not None
    assert _load_cursors(result.new_cursor) == {
        "channels": {"C1": "1700000002.000200"},
        "backfill": {},
        "threads": {},
    }


def test_sync_resumes_from_persisted_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    """The persisted JSON cursor is parsed and the channels axis is handed to the fetcher.

    Without this, every sync would re-fetch the channel's full
    history. Pinning the kwargs argument shape prevents a regression
    that loses the resume value. Phase 20-B: the fetcher signature
    still takes ``cursor_per_channel=`` (a flat ``dict[str, str | None]``),
    so the connector forwards the ``channels`` axis directly without
    surfacing the compound envelope to the fetcher.
    """
    _patch_settings(monkeypatch, channels=["C1", "C2"])
    _patch_auth(monkeypatch)
    _, captured = _patch_fetcher(monkeypatch, yields=[])

    prior_cursor = _dump_cursors(
        {
            "channels": {"C1": "1700000001.000100", "C2": None},
            "backfill": {},
            "threads": {},
        }
    )
    service = _RecordingSourceService()
    result = SlackConnector().sync(_context(service, cursor_value=prior_cursor))

    # Fetcher receives the channels axis verbatim — not the compound
    # envelope. Pin Phase 20-A signature stability.
    assert captured["cursor_per_channel"] == {
        "C1": "1700000001.000100",
        "C2": None,
    }
    # No yields → observed_count=0; cursor stays at the prior value
    # (byte-identical because both axes are unchanged and
    # ``_dump_cursors`` is deterministic).
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
    # issue #339 inbox inflation. Wrapped in the Phase 20-B compound
    # envelope.
    assert result.new_cursor is not None
    assert _load_cursors(result.new_cursor) == {
        "channels": {"C1": "1700000002.000200"},
        "backfill": {},
        "threads": {},
    }


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
        "channels": {"C1": "ts-c1-new", "C2": "ts-c2-new"},
        "backfill": {},
        "threads": {},
    }


# --------------------------------------------------- sync: date floor (Phase 20, ADR-0036)


def test_sync_applies_global_floor_as_resume_bound_on_first_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First sync with ``sync_since`` set → fetcher resumes from the floor ts.

    With no prior cursor the floor becomes the ``oldest`` bound passed to
    ``conversations.history`` so the cold-start backfill is capped at the
    floor instead of walking the whole channel history (ADR-0036 §(g)).
    """
    _patch_settings(monkeypatch, channels=["C1"], sync_since="2020-01-01")
    _patch_auth(monkeypatch)
    _, captured = _patch_fetcher(monkeypatch, yields=[])

    SlackConnector().sync(_context(_RecordingSourceService(), cursor_value=None))

    assert captured["cursor_per_channel"] == {"C1": since_to_ts(parse_since("2020-01-01"))}


def test_sync_floor_is_inert_when_cursor_is_newer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An already-synced channel ignores a floor older than its cursor.

    ``_max_ts(cursor, floor)`` keeps the (newer) cursor, so enabling
    ``sync_since`` on an established install never rewinds the resume
    bound and never re-fetches history (ADR-0036 §(g) "cursor is
    authoritative").
    """
    _patch_settings(monkeypatch, channels=["C1"], sync_since="2000-01-01")
    _patch_auth(monkeypatch)
    _, captured = _patch_fetcher(monkeypatch, yields=[])

    prior = _dump_cursors(
        {"channels": {"C1": "1700000000.000000"}, "backfill": {}, "threads": {}}
    )  # year 2023 ≫ floor 2000
    SlackConnector().sync(_context(_RecordingSourceService(), cursor_value=prior))

    assert captured["cursor_per_channel"] == {"C1": "1700000000.000000"}


def test_sync_per_channel_all_opts_out_of_global_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``since = "all"`` keeps a channel on full backfill despite a global floor.

    The floor-bearing channel gets the floor ts; the ``"all"`` channel
    keeps the first-sync "absent key" resume shape (no ``oldest`` bound).
    """
    _patch_settings(
        monkeypatch,
        channels=[SlackChannelSpec(id="C_FLOOR"), SlackChannelSpec(id="C_ALL", since="all")],
        sync_since="2020-01-01",
    )
    _patch_auth(monkeypatch)
    _, captured = _patch_fetcher(monkeypatch, yields=[])

    SlackConnector().sync(_context(_RecordingSourceService(), cursor_value=None))

    # C_ALL is omitted (no floor, first sync); C_FLOOR carries the floor ts.
    assert captured["cursor_per_channel"] == {"C_FLOOR": since_to_ts(parse_since("2020-01-01"))}


def test_sync_relative_floor_is_evaluated_at_sync_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative ``sync_since`` ("30d") is resolved against ``now_utc`` at sync time.

    Pinning the ``now_utc`` monkeypatch proves the floor advances with
    each run rather than freezing at config-load time (ADR-0036 §(f)).
    """
    from datetime import UTC, datetime, timedelta

    fixed_now = datetime(2026, 6, 4, tzinfo=UTC)
    monkeypatch.setattr("opshub.core.time.now_utc", lambda: fixed_now)
    _patch_settings(monkeypatch, channels=["C1"], sync_since="30d")
    _patch_auth(monkeypatch)
    _, captured = _patch_fetcher(monkeypatch, yields=[])

    SlackConnector().sync(_context(_RecordingSourceService(), cursor_value=None))

    expected = since_to_ts(fixed_now - timedelta(days=30))
    assert captured["cursor_per_channel"] == {"C1": expected}


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
    assert _load_cursors(result.new_cursor) == {
        "channels": {"C1": "ts-1", "C-secret": "ts-2"},
        "backfill": {},
        "threads": {},
    }


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
    assert _load_cursors(result.new_cursor) == {
        "channels": {"C1": "ts-human"},
        "backfill": {},
        "threads": {},
    }


def test_sync_with_no_yields_preserves_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configured channels but zero new messages → cursor unchanged.

    Mirrors :func:`test_empty_sync_preserves_cursor` in the GitHub
    connector test suite — "no progress, no movement" is the
    documented contract.
    """
    _patch_settings(monkeypatch, channels=["C1"])
    _patch_auth(monkeypatch)
    _patch_fetcher(monkeypatch, yields=[])

    prior_cursor = _dump_cursors({"channels": {"C1": "ts-old"}, "backfill": {}, "threads": {}})
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
    # crash, wrapped in the Phase 20-B compound envelope (channels
    # axis advanced, threads axis preserved empty — pre-fix this
    # would have been missing entirely).
    assert _load_cursors(call["value"]) == {
        "channels": {"C1": "1700000002.000200"},
        "backfill": {},
        "threads": {},
    }


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
    assert _load_cursors(call["value"]) == {
        "channels": {"C-secret": "1700000001.000100"},
        "backfill": {},
        "threads": {},
    }


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
    # The success-path return value still encodes the new cursor
    # under the Phase 20-B compound envelope.
    assert result.observed_count == 1
    assert result.new_cursor is not None
    assert _load_cursors(result.new_cursor) == {
        "channels": {"C1": "1700000001.000100"},
        "backfill": {},
        "threads": {},
    }


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
    assert _load_cursors(call["value"]) == {
        "channels": {"C1": "1700000001.000100"},
        "backfill": {},
        "threads": {},
    }


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

    prior_cursor = _dump_cursors(
        {"channels": {"A": "ts-a-prior", "B": None}, "backfill": {}, "threads": {}}
    )
    service = _RecordingSourceService()
    with pytest.raises(ConnectorFailedError):
        SlackConnector().sync(_context(service, cursor_value=prior_cursor))

    # The checkpoint persists both channels' state — A's prior
    # cursor untouched, B's partial cursor advanced. Compound
    # envelope so the next sync's _load_cursors sees a valid Phase
    # 20-B schema.
    assert len(service.cursor_set_calls) == 1
    call = service.cursor_set_calls[0]
    assert _load_cursors(call["value"]) == {
        "channels": {"A": "ts-a-prior", "B": "ts-b-new"},
        "backfill": {},
        "threads": {},
    }


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


# ----- sync: gap backfill (Phase 22-D, ADR-0038) ------------------------


def _patch_fetcher_with_gap(
    monkeypatch: pytest.MonkeyPatch,
    *,
    forward_yields: list[tuple[str, RawSlackMessage, str | None]] | None = None,
    gap_yields: Iterator[tuple[str, RawSlackMessage, str | None]]
    | list[tuple[str, RawSlackMessage, str | None]]
    | None = None,
) -> tuple[MagicMock, dict[str, dict[str, Any] | None]]:
    """Patch :class:`SlackFetcher` routing forward vs gap ``fetch_messages`` calls.

    The connector (Phase 22-D) constructs two fetchers — the forward one
    (no ``latest_per_channel``) and the gap one (``latest_per_channel``
    set). ``MagicMock`` hands back the same ``return_value`` for both, so
    we route inside the ``fetch_messages`` side-effect on the presence of
    ``latest_per_channel``: a bounded call is the gap pass, an unbounded
    call is the forward pass. Each returns a **fresh** iterator (the gap
    pass drains before the forward pass, so a single shared iterator would
    starve the second consumer). ``fetch_thread_replies`` is a no-op so the
    Phase 2 polling path doesn't interfere with these backfill-focused
    assertions.
    """
    forward = list(forward_yields or [])
    captured: dict[str, dict[str, Any] | None] = {"forward": None, "gap": None}
    fake_cls = MagicMock()

    def _fetch_messages(
        *,
        cursor_per_channel: dict[str, str | None],
        latest_per_channel: dict[str, str | None] | None = None,
        max_per_channel: int = 100,
        excludes: Any = None,
    ) -> Iterator[tuple[str, RawSlackMessage, str | None]]:
        del max_per_channel, excludes
        if latest_per_channel is not None:
            captured["gap"] = {
                "cursor_per_channel": dict(cursor_per_channel),
                "latest_per_channel": dict(latest_per_channel),
            }
            # ``gap_yields`` may be a generator (for the crash test) — pass
            # it through verbatim so its side effects (raising) fire as the
            # connector iterates.
            if gap_yields is None:
                return iter(())
            if isinstance(gap_yields, list):
                return iter(list(gap_yields))
            return gap_yields
        captured["forward"] = {"cursor_per_channel": dict(cursor_per_channel)}
        return iter(list(forward))

    def _no_replies(**_kw: Any) -> Iterator[RawSlackMessage]:
        # Phase 2 polling is a no-op for these backfill-focused tests.
        return iter(())

    fake_cls.return_value.fetch_messages.side_effect = _fetch_messages
    fake_cls.return_value.fetch_thread_replies.side_effect = _no_replies
    monkeypatch.setattr("opshub.connectors.slack.connector.SlackFetcher", fake_cls)
    return fake_cls, captured


def test_sync_lowered_floor_triggers_gap_backfill(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lowering an absolute floor below the recorded low-water → gap backfill.

    A post-feature channel (``channels`` + ``backfill`` both recorded)
    whose ``sync_since`` is lowered fetches only the newly-uncovered
    window ``(floor_new, low_water]`` — disjoint from the forward set —
    and advances the low-water mark down to the new floor (ADR-0038 §(d)).
    """
    _patch_settings(monkeypatch, channels=["C1"], sync_since="2026-01-01")
    _patch_auth(monkeypatch)

    floor_ts = since_to_ts(parse_since("2026-01-01"))
    gap_ts = since_to_ts(parse_since("2026-02-01"))
    low_water = since_to_ts(parse_since("2026-03-01"))
    high_water = since_to_ts(parse_since("2026-06-01"))

    gap_msg = _raw_message(channel_id="C1", ts=gap_ts, text="gap-msg")
    _, captured = _patch_fetcher_with_gap(
        monkeypatch,
        forward_yields=[],  # no new messages at the head this run
        gap_yields=[("C1", gap_msg, gap_ts)],
    )

    prior = _dump_cursors(
        {
            "channels": {"C1": high_water},
            "backfill": {"C1": low_water},
            "threads": {},
        }
    )
    service = _RecordingSourceService()
    result = SlackConnector().sync(_context(service, cursor_value=prior))

    # The gap pass fetched exactly the (floor_new, low_water] window.
    assert captured["gap"] == {
        "cursor_per_channel": {"C1": floor_ts},
        "latest_per_channel": {"C1": low_water},
    }
    # The gap message was observed (and nothing else — the forward set is
    # not re-fetched, so no inbox inflation on the already-covered region).
    assert [c["external_id"] for c in service.calls] == [f"C1:{gap_ts}"]
    parsed = _load_cursors(result.new_cursor)
    # Low-water advanced down to the new floor; high-water unchanged (the
    # older gap ts never rewinds the forward cursor).
    assert parsed["backfill"] == {"C1": floor_ts}
    assert parsed["channels"]["C1"] == high_water


def test_sync_relative_floor_does_not_trigger_gap_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative floor walks forward, so it never falls below the low-water.

    ``sync_since = "90d"`` evaluated at sync time is *newer* than a
    low-water mark recorded a year ago, so ``target_low >= low_water`` and
    no gap fires — the load-bearing guard against a relative floor
    spuriously re-fetching history on every run (ADR-0038 §(d)).
    """
    _patch_settings(monkeypatch, channels=["C1"], sync_since="90d")
    _patch_auth(monkeypatch)

    low_water = since_to_ts(now_utc() - timedelta(days=365))
    high_water = since_to_ts(now_utc() - timedelta(days=1))
    _, captured = _patch_fetcher_with_gap(monkeypatch, forward_yields=[])

    prior = _dump_cursors(
        {
            "channels": {"C1": high_water},
            "backfill": {"C1": low_water},
            "threads": {},
        }
    )
    service = _RecordingSourceService()
    result = SlackConnector().sync(_context(service, cursor_value=prior))

    # No gap pass at all.
    assert captured["gap"] is None
    assert service.calls == []
    # Low-water unchanged.
    parsed = _load_cursors(result.new_cursor)
    assert parsed["backfill"] == {"C1": low_water}


def test_sync_gap_backfill_suppressed_when_toggle_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``backfill_on_floor_lower=False`` suppresses the gap pass.

    The lowered floor still bounds the forward fetch, but the past is not
    re-fetched (ADR-0038 §透明性 / `--no-backfill`).
    """
    _patch_settings(
        monkeypatch,
        channels=["C1"],
        sync_since="2026-01-01",
        backfill_on_floor_lower=False,
    )
    _patch_auth(monkeypatch)

    low_water = since_to_ts(parse_since("2026-03-01"))
    high_water = since_to_ts(parse_since("2026-06-01"))
    _, captured = _patch_fetcher_with_gap(monkeypatch, forward_yields=[])

    prior = _dump_cursors(
        {
            "channels": {"C1": high_water},
            "backfill": {"C1": low_water},
            "threads": {},
        }
    )
    service = _RecordingSourceService()
    result = SlackConnector().sync(_context(service, cursor_value=prior))

    assert captured["gap"] is None
    assert service.calls == []
    # Low-water is left where it was (no backfill performed).
    parsed = _load_cursors(result.new_cursor)
    assert parsed["backfill"] == {"C1": low_water}


def test_sync_pre_feature_channel_does_not_auto_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A channel synced before Phase 22 (no ``backfill`` entry) never auto-backfills.

    Its historical floor is unrecoverable, so even with a floor set the
    connector leaves the ``backfill`` axis absent and fires no gap pass
    (ADR-0038 §(e)). The operator uses ``opshub slack cursor backfill``.
    """
    _patch_settings(monkeypatch, channels=["C1"], sync_since="2026-01-01")
    _patch_auth(monkeypatch)
    _, captured = _patch_fetcher_with_gap(monkeypatch, forward_yields=[])

    # Pre-feature cursor: ``channels`` present, ``backfill`` axis absent
    # entirely (the connector tolerates the missing axis, Phase 22-B).
    prior = '{"channels":{"C1":"1800000000.000000"},"threads":{}}'
    service = _RecordingSourceService()
    result = SlackConnector().sync(_context(service, cursor_value=prior))

    assert captured["gap"] is None
    assert service.calls == []
    # The axis stays absent (≡ epoch low-water, no auto-backfill).
    parsed = _load_cursors(result.new_cursor)
    assert parsed["backfill"] == {}


def test_sync_floored_cold_start_records_low_water(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A floored cold-start records ``backfill[ch] = floor`` (no gap pass).

    The forward pass resumes from ``oldest = floor`` so the channel is
    covered down to the floor; recording the low-water lets a *later*
    floor reduction detect the gap (ADR-0038 §(d)).
    """
    _patch_settings(monkeypatch, channels=["C1"], sync_since="2026-01-01")
    _patch_auth(monkeypatch)

    floor_ts = since_to_ts(parse_since("2026-01-01"))
    head_msg = _raw_message(channel_id="C1", ts=since_to_ts(parse_since("2026-05-01")))
    _, captured = _patch_fetcher_with_gap(
        monkeypatch,
        forward_yields=[("C1", head_msg, head_msg.ts)],
    )

    service = _RecordingSourceService()
    result = SlackConnector().sync(_context(service, cursor_value=None))

    # Cold-start → no gap pass, but the low-water is recorded at the floor.
    assert captured["gap"] is None
    parsed = _load_cursors(result.new_cursor)
    assert parsed["backfill"] == {"C1": floor_ts}
    assert parsed["channels"]["C1"] == head_msg.ts


def test_sync_no_floor_cold_start_leaves_backfill_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-floor cold-start leaves the ``backfill`` axis empty (≡ epoch).

    Nothing older than the channel head exists to backfill, so we do not
    materialise a spurious epoch entry (ADR-0038 §(d)).
    """
    _patch_settings(monkeypatch, channels=["C1"])  # no sync_since
    _patch_auth(monkeypatch)
    head_msg = _raw_message(channel_id="C1", ts="1700000005.000000")
    _, captured = _patch_fetcher_with_gap(
        monkeypatch,
        forward_yields=[("C1", head_msg, head_msg.ts)],
    )

    service = _RecordingSourceService()
    result = SlackConnector().sync(_context(service, cursor_value=None))

    assert captured["gap"] is None
    parsed = _load_cursors(result.new_cursor)
    assert parsed["backfill"] == {}


def test_sync_gap_parent_seeds_threads_axis(monkeypatch: pytest.MonkeyPatch) -> None:
    """A parent fetched in the gap pass seeds the ``threads`` axis.

    Gap parents' replies were never observed (the parent was below the
    forward window), so registering the thread lets the Phase 2 polling
    path pick up their replies on subsequent syncs (ADR-0038 §thread).
    The threads-axis bookkeeping is shared with the forward path via
    ``_ingest_yield``.
    """
    _patch_settings(monkeypatch, channels=["C1"], sync_since="2026-01-01")
    _patch_auth(monkeypatch)

    low_water = since_to_ts(parse_since("2026-03-01"))
    high_water = since_to_ts(parse_since("2026-06-01"))
    # A gap parent (thread_ts == ts) with a *recent* latest_reply so the
    # Phase 2 prune keeps it in-window.
    parent_ts = since_to_ts(parse_since("2026-02-01"))
    latest_reply_ts = since_to_ts(now_utc() - timedelta(days=1))
    gap_parent = RawSlackMessage(
        channel_id="C1",
        channel_name="general",
        ts=parent_ts,
        text="gap-parent",
        user_id="U1",
        user_display_name="alice",
        permalink="https://acme.slack.com/archives/C1/p1",
        raw={"latest_reply": latest_reply_ts},
        thread_ts=parent_ts,
    )
    _patch_fetcher_with_gap(
        monkeypatch,
        forward_yields=[],
        gap_yields=[("C1", gap_parent, parent_ts)],
    )

    prior = _dump_cursors(
        {"channels": {"C1": high_water}, "backfill": {"C1": low_water}, "threads": {}}
    )
    service = _RecordingSourceService()
    result = SlackConnector().sync(_context(service, cursor_value=prior))

    parsed = _load_cursors(result.new_cursor)
    # The gap parent registered its thread at ``latest_reply``.
    assert parsed["threads"] == {f"C1:{parent_ts}": latest_reply_ts}


def test_sync_mid_gap_crash_does_not_advance_low_water(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash mid-gap leaves the low-water unchanged (whole window re-attempted).

    The low-water mark advances to the new floor only **after** the gap
    pass drains fully. A mid-gap exception keeps ``backfill`` at the prior
    low-water, so the next sync re-attempts the whole ``(floor_new,
    low_water]`` window (resume-safe; the bounded re-observe is healed on
    inbox by #522). Here the gap yields a thread *reply* first so the
    threads axis advances — which makes the issue #339 partial-progress
    checkpoint fire, letting us assert that it persists the threads
    progress while explicitly **not** advancing the low-water (ADR-0038
    §(d)).
    """
    _patch_settings(monkeypatch, channels=["C1"], sync_since="2026-01-01")
    _patch_auth(monkeypatch)

    low_water = since_to_ts(parse_since("2026-03-01"))
    high_water = since_to_ts(parse_since("2026-06-01"))
    parent_ts = since_to_ts(parse_since("2026-02-01"))
    reply_ts = since_to_ts(parse_since("2026-02-15"))
    # A gap *reply* (thread_ts != ts): its yield advances the threads axis
    # (so the checkpoint fires) but never the channels high-water (the
    # reply's cursor anchor is the parent ts, below the forward cursor).
    gap_reply = RawSlackMessage(
        channel_id="C1",
        channel_name="general",
        ts=reply_ts,
        text="gap-reply",
        user_id="U1",
        user_display_name="alice",
        permalink="https://acme.slack.com/archives/C1/p2",
        raw={},
        thread_ts=parent_ts,
    )

    def _crashing_gap() -> Iterator[tuple[str, RawSlackMessage, str | None]]:
        # Reply yields under the parent's cursor anchor (Phase 20-A
        # semantics), then the fetcher raises mid-window.
        yield ("C1", gap_reply, parent_ts)
        raise ConnectorFailedError("Slack fetch failed for channel C1: rate_limited")

    _patch_fetcher_with_gap(monkeypatch, forward_yields=[], gap_yields=_crashing_gap())

    prior = _dump_cursors(
        {"channels": {"C1": high_water}, "backfill": {"C1": low_water}, "threads": {}}
    )
    service = _RecordingSourceService()
    with pytest.raises(ConnectorFailedError):
        SlackConnector().sync(_context(service, cursor_value=prior))

    # The gap reply was observed before the crash.
    assert [c["external_id"] for c in service.calls] == [f"C1:{reply_ts}"]
    # The partial-progress checkpoint fired (threads axis advanced).
    assert len(service.cursor_set_calls) == 1
    checkpoint = service.cursor_set_calls[0]
    assert checkpoint["sync_started"] is True
    parsed = _load_cursors(checkpoint["value"])
    # Threads progress is checkpointed...
    assert parsed["threads"] == {f"C1:{parent_ts}": reply_ts}
    # ...but the low-water is NOT advanced (the gap did not fully drain) —
    # the next sync re-attempts (floor_new, low_water].
    assert parsed["backfill"] == {"C1": low_water}
