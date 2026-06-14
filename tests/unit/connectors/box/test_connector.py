"""Tests for :class:`opshub.connectors.box.connector.BoxConnector`.

Focused on the Phase 10 audit Cluster 3 §A2 contract: the shared
``excludes.yaml`` ``senders`` selector (Box actor id) and ``paths``
selector (Box item path) cause the connector to skip the matched
event before it reaches the source service. Box's
``next_stream_position`` advances once per page (not per event), so
the cursor advances regardless of skip / observe — the connector's
"last seen position" tracker remains the page-level position.

The ``[connectors-box]`` extras are gated with :func:`pytest.importorskip`
to mirror the rest of the Box test suite.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip(
    "boxsdk",
    reason="Box connector tests require the 'connectors-box' extras",
)

from opshub.connectors.box.connector import BoxConnector
from opshub.connectors.box.fetcher import RawBoxEvent
from opshub.connectors.context import ConnectorContext

# ---------------------------------------------------------------------- helpers


class _RecordingSourceService:
    """Test double for :class:`SourceService`.

    Records ``observe`` calls with the full Phase 10 keyword set so a
    regression that drops ``body`` / ``provenance_origin`` /
    ``provenance_trust`` from the connector forwarder trips immediately.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

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
        author_handle: str | None = None,
        author_display: str | None = None,
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
                "author_handle": author_handle,
                "author_display": author_display,
            }
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


def _raw_event(
    *,
    event_id: str = "ev-1",
    event_type: str = "ITEM_CREATE",
    item_id: str = "f-1",
    item_name: str = "report.pdf",
    item_path: str = "/Documents/report.pdf",
    actor_id: str = "u-1",
    actor_name: str = "Alice",
    web_url: str | None = "https://app.box.com/file/f-1",
) -> RawBoxEvent:
    return RawBoxEvent(
        event_id=event_id,
        event_type=event_type,
        item_id=item_id,
        item_type="file",
        item_name=item_name,
        item_path=item_path,
        created_iso="2026-05-17T10:00:00Z",
        actor_id=actor_id,
        actor_name=actor_name,
        web_url=web_url,
        raw={"event_id": event_id},
    )


def _patch_excludes_yaml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, body: str) -> None:
    """Write an ``excludes.yaml`` and redirect :func:`default_config_dir`.

    Mirrors the slack / github / ms365 excludes-test pattern: the
    connector calls :func:`load_excludes` with **no arguments** so it
    resolves through the module-level :func:`default_config_dir`
    import on :mod:`opshub.core.excludes` — patching that name is the
    documented way to redirect resolution for tests (audit Cluster 3
    rationale).
    """
    cfg_dir = tmp_path / "opshub-config"
    cfg_dir.mkdir()
    (cfg_dir / "excludes.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setattr("opshub.core.excludes.default_config_dir", lambda: cfg_dir)


def _connector_with_events(
    events: list[tuple[RawBoxEvent, str]],
) -> BoxConnector:
    """Build a :class:`BoxConnector` whose fetcher yields ``events``.

    Routes through the ``fetcher_factory`` constructor seam so no real
    Box SDK / keyring touch happens. The stub mirrors the actual
    fetcher's iterator contract (``(RawBoxEvent, stream_position)``
    pairs) so the connector loop reads it identically.
    """

    class _StubFetcher:
        def fetch_events(self, *, stream_position: str | None) -> Iterator[tuple[RawBoxEvent, str]]:
            del stream_position
            yield from events

    fetcher = _StubFetcher()
    return BoxConnector(fetcher_factory=lambda: fetcher)  # type: ignore[arg-type,return-value]


# ---------------------------------------------------------------------- name


def test_connector_name_is_box() -> None:
    """The registry / CLI dispatch key must be exactly ``"box"``."""
    assert BoxConnector.name == "box"
    assert BoxConnector().name == "box"


# ---------------------------------------------------------------------- happy path


def test_sync_observes_each_event_and_advances_cursor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Baseline contract: every event reaches the source service.

    With an empty excludes file every yielded event flows through to
    :meth:`SourceService.observe`. Pins the per-event pipeline so the
    exclude-path tests below have a known-good reference.
    """
    _patch_excludes_yaml(monkeypatch, tmp_path, body="")
    events: list[tuple[RawBoxEvent, str]] = [
        (_raw_event(event_id="ev-1"), "pos-1"),
        (_raw_event(event_id="ev-2"), "pos-2"),
    ]
    connector = _connector_with_events(events)

    service = _RecordingSourceService()
    result = connector.sync(_context(service))

    assert result.observed_count == 2
    assert [c["external_id"] for c in service.calls] == ["ev-1", "ev-2"]
    # ADR-0020 §(e): provenance threads through even on the happy path.
    assert all(c["provenance_origin"] == "external" for c in service.calls)
    assert all(c["provenance_trust"] == "untrusted" for c in service.calls)
    assert result.new_cursor == "pos-2"


# ---------------------------------------------------------------------- excludes


def test_sync_skips_event_from_excluded_actor_but_advances_cursor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ADR-0020 §(b): an event from an excluded actor is never observed.

    Box's ``next_stream_position`` is page-wide, so it advances
    regardless of skip / observe — the connector's "last position"
    tracker reflects that. Pinning explicitly so a regression that
    early-``continue``\\s without updating ``new_cursor`` does not slip
    past review.
    """
    _patch_excludes_yaml(monkeypatch, tmp_path, body="senders:\n  - u-bot\n")
    events: list[tuple[RawBoxEvent, str]] = [
        (_raw_event(event_id="ev-bot", actor_id="u-bot"), "pos-1"),
        (_raw_event(event_id="ev-human", actor_id="u-1"), "pos-2"),
    ]
    connector = _connector_with_events(events)

    service = _RecordingSourceService()
    result = connector.sync(_context(service))

    assert result.observed_count == 1
    assert [c["external_id"] for c in service.calls] == ["ev-human"]
    # Cursor still advances to the last seen position despite the skip.
    assert result.new_cursor == "pos-2"


def test_sync_skips_event_under_excluded_path_but_advances_cursor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ADR-0020 §(b): an event under an excluded path glob is dropped.

    Uses the same gitignore-style ``**/secrets/**`` pattern as
    ``box_drive`` so a single shared rule covers both connector
    flavours uniformly.
    """
    _patch_excludes_yaml(monkeypatch, tmp_path, body="paths:\n  - '**/secrets/**'\n")
    events: list[tuple[RawBoxEvent, str]] = [
        (
            _raw_event(event_id="ev-secret", item_path="/Documents/secrets/api-key.txt"),
            "pos-1",
        ),
        (
            _raw_event(event_id="ev-safe", item_path="/Documents/Reports/Q3.pdf"),
            "pos-2",
        ),
    ]
    connector = _connector_with_events(events)

    service = _RecordingSourceService()
    result = connector.sync(_context(service))

    assert result.observed_count == 1
    assert [c["external_id"] for c in service.calls] == ["ev-safe"]
    assert result.new_cursor == "pos-2"
