"""Tests for ``opshub.connectors.box.mapper`` (Phase 7 step C3).

The mapper turns a :class:`RawBoxEvent` into a :class:`SourceObserved`
event ready for append. The tests here pin the contract documented in
the module docstring:

* ``external_id`` / ``source_type`` / ``title`` field assembly.
* :data:`SUMMARY_MAX_CHARS` truncation (ADR-0005 External Content
  Minimisation).
* ``web_url=None`` is forwarded as-is (no fabricated Box-canonical
  URL) because :class:`SourceObserved.url` is already nullable.
* ``occurred_at`` parsing produces a tz-aware UTC :class:`datetime`.

The ``pytest.importorskip("boxsdk", ...)`` guard mirrors
``tests/unit/connectors/box/test_fetcher.py`` — the mapper module
itself only imports :class:`RawBoxEvent` under ``TYPE_CHECKING``, but
constructing a real :class:`RawBoxEvent` in the test requires the
fetcher module, which lazy-imports ``boxsdk`` on first instantiation
of :class:`BoxFetcher`. Importing :class:`RawBoxEvent` directly (a
:func:`dataclasses.dataclass`) does *not* touch ``boxsdk`` at runtime,
but the skip keeps the test suite consistent with the Phase 7 plan
expectation that ``connectors-box`` extras gate every Box test file.
"""

from __future__ import annotations

from datetime import UTC

import pytest

pytest.importorskip(
    "boxsdk",
    reason="Box connector tests require the 'connectors-box' extras",
)

from opshub.connectors.box.fetcher import RawBoxEvent
from opshub.connectors.box.mapper import (
    SOURCE_TYPE,
    SUMMARY_MAX_CHARS,
    map_event,
)


def _raw_event(
    *,
    event_id: str = "ev-1",
    event_type: str = "ITEM_CREATE",
    item_id: str = "f-1",
    item_type: str = "file",
    item_name: str = "report.pdf",
    item_path: str = "/Documents/Reports/report.pdf",
    created_iso: str = "2026-05-17T10:00:00Z",
    actor_id: str = "u-1",
    actor_name: str = "Alice",
    web_url: str | None = "https://app.box.com/file/f-1",
) -> RawBoxEvent:
    """Construct a :class:`RawBoxEvent` for mapper tests.

    Defaults match the fetcher's representative happy-path event so
    each test overrides only the field it cares about. The frozen
    dataclass shape mirrors what the fetcher actually yields, so the
    mapper tests exercise the same value-object the real connector
    consumes at runtime.
    """
    return RawBoxEvent(
        event_id=event_id,
        event_type=event_type,
        item_id=item_id,
        item_type=item_type,
        item_name=item_name,
        item_path=item_path,
        created_iso=created_iso,
        actor_id=actor_id,
        actor_name=actor_name,
        web_url=web_url,
        raw={"event_id": event_id, "event_type": event_type},
    )


def test_map_event_basic() -> None:
    """``RawBoxEvent`` → ``SourceObserved`` with the canonical field assembly.

    Pins the documented contract: title combines event_type with the
    item name; source_type is the singleton :data:`SOURCE_TYPE`;
    external_id matches the Box event id; url and summary pass through
    intact (within the 200-char summary cap).
    """
    raw = _raw_event(event_id="evt-42", event_type="ITEM_CREATE", item_name="foo.pdf")

    observed = map_event(raw)

    assert observed.connector_name == "box"
    assert observed.source_type == SOURCE_TYPE == "box_event"
    assert observed.external_id == "evt-42"
    assert observed.title == "ITEM_CREATE: foo.pdf"
    assert observed.summary == "path: /Documents/Reports/report.pdf"
    assert observed.url == "https://app.box.com/file/f-1"
    assert observed.actor == "connector:box"


def test_map_event_truncates_long_path() -> None:
    """A summary that would exceed :data:`SUMMARY_MAX_CHARS` is truncated.

    ADR-0005 caps :attr:`SourceObserved.summary` at 200 chars; the
    mapper appends a single ``"…"`` so the truncation is visible to
    operators reading recall output. The final string length must be
    *exactly* :data:`SUMMARY_MAX_CHARS` — the ellipsis replaces the
    last character of the otherwise-overflowing prefix.
    """
    # ``deep/`` x 50 puts the rendered ``path: /.../file.pdf`` well past 200 chars.
    long_path = "/" + "/".join(["deep"] * 50) + "/file.pdf"
    raw = _raw_event(item_path=long_path)

    observed = map_event(raw)

    assert observed.summary is not None
    assert len(observed.summary) == SUMMARY_MAX_CHARS
    assert observed.summary.endswith("…")
    # The 200th character is the ellipsis itself; ensure the prefix
    # before it is a faithful slice of ``path: <long_path>`` rather
    # than a different sentinel.
    assert observed.summary.startswith("path: /deep/")


def test_map_event_handles_none_web_url() -> None:
    """``web_url=None`` is forwarded verbatim — no canonical URL fabrication.

    Box returns ``None`` for the deep link on some event types (e.g.
    ``ITEM_TRASH`` on a purged item). :class:`SourceObserved.url` is
    nullable, so the mapper preserves the ``None`` rather than invent
    a Box-canonical URL that would 404 when followed.
    """
    raw = _raw_event(event_type="ITEM_TRASH", web_url=None)

    observed = map_event(raw)

    assert observed.url is None


def test_map_event_observed_at_is_utc_aware() -> None:
    """``raw.created_iso`` → tz-aware UTC :class:`datetime` on the event.

    The fetcher keeps timestamps as raw ISO strings so the
    string-vs-datetime conversion happens once, here, at the
    projection boundary. The base :class:`DomainEvent` enforces
    tz-aware UTC via its :func:`to_utc` validator, so a regression in
    the parser would either raise at construction time or land a
    naive datetime — both of which the assertions below cover.

    The ``+09:00`` test case is included to prove the mapper does not
    assume Box always sends UTC; the validator normalises a Tokyo
    offset back to UTC at the event boundary.
    """
    raw_utc = _raw_event(created_iso="2026-05-17T10:00:00Z")
    raw_jst = _raw_event(created_iso="2026-05-17T19:00:00+09:00")

    observed_utc = map_event(raw_utc)
    observed_jst = map_event(raw_jst)

    assert observed_utc.occurred_at.tzinfo is not None
    assert observed_utc.occurred_at.utcoffset() == UTC.utcoffset(observed_utc.occurred_at)
    # The two events describe the same wall-clock instant — the
    # mapper's UTC normalisation must agree.
    assert observed_utc.occurred_at == observed_jst.occurred_at


def test_map_event_uses_custom_actor() -> None:
    """The ``actor`` kwarg overrides the ``connector:box`` default.

    The override exists so unit tests / future direct-construction
    paths can stamp a different provenance without monkeypatching the
    module-level default.
    """
    raw = _raw_event()

    observed = map_event(raw, actor="cli:test-suite")

    assert observed.actor == "cli:test-suite"


def test_map_event_body_equals_summary_provenance_tagged() -> None:
    """epic #470 / issue #481: metadata-only Box events emit ``body = summary``.

    ADR-0020: Box *events* describe file activity, not content. The
    mapper has no body to retain (the event payload is metadata-only),
    so the metadata-only rule (ADR-0010 §不変条件) applies: the
    composed ``"path: <item_path>"`` summary is reused as the body to
    satisfy the :class:`SourceObserved.body` ``min_length=1``
    invariant. The observation is still external in origin, so the
    provenance tags are stamped (external / untrusted) for
    cross-connector consistency with the SaaS family.
    """
    observed = map_event(_raw_event())

    assert observed.body == observed.summary, (
        "metadata-only path must reuse summary as body (epic #470 / #481)"
    )
    assert observed.body is not None and observed.body.strip()
    assert observed.provenance_origin == "external"
    assert observed.provenance_trust == "untrusted"
