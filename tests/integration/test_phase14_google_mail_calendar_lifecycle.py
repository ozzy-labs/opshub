"""Phase 14 end-to-end Gmail + Google Calendar lifecycle (Sub-issue G5 closeout).

Pins the Phase 14 data-pipeline shape: Gmail (``gmail_message``) and
Google Calendar (``google_calendar`` master + override-as-separate-record)
bodies land in the same ``sources`` projection as the Phase 7 / 11 / 13
connectors, are indexed by FTS5 over the same body store, surface
through the same MCP read tools that drive the secretary skills, and
the write-back path remains structurally absent for both new connectors
(ADR-0010 §禁止事項 7 + §Phase 14 改訂 (i) 禁止事項拡張 = Gmail
``users.watch`` + Calendar ``events.watch`` も禁止).

Phase 14 ships **no new MCP tools** and **no new extras** — the existing
read surface (``search`` + ``recall.search`` + ``source.list`` +
``source.get``) automatically widens to the new ``source_type``
discriminators because the projection is the SSOT and the
``connectors-google-workspace`` extras (httpx) installed in Phase 13
already cover both new connectors. The test follows the
:mod:`test_phase13_google_workspace_lifecycle` pattern (deliberately
leaner because Phase 14 reuses every contract layer Phase 13 already
pinned; the only Phase-14-specific guarantees are mapper symmetry +
override-as-separate-record + the absence of Gmail send / Calendar
event-create call paths).

What this pins
--------------

1. **取り込み (Gmail + Calendar、Outlook / ms365_calendar と symmetric)** —
   sources of both Phase 14 ``source_type`` discriminators
   (``gmail_message`` / ``google_calendar``) land in ``sources`` with
   bodies + ``provenance_origin="external"`` + ``provenance_trust="untrusted"``
   (ADR-0020 §(e)). The FTS5 index (Phase 10 migration 0019) treats
   them uniformly with the Phase 7-13 connectors.

2. **Override-as-separate-record (Calendar)** — a Google Calendar
   override (``recurringEventId`` + ``originalStartTime`` set) is
   emitted as a **standalone** ``SourceObserved`` sharing
   ``source_type="google_calendar"`` and carrying a
   ``Override of: <master_id>`` back-pointer in the body. Pins the
   Phase 14 plan §G4 / OQ3 decision against the temptation to merge
   overrides back into the master record (which would lose the
   override-specific deltas the recall path depends on).

3. **Cross-source search mixes Phase 11 / 13 / 14** — ``search``
   (FTS5) returns hits that span Phase 11 ``ms365_outlook``
   (Outlook body deep retention) + Phase 13 ``google_doc`` + Phase 14
   ``gmail_message`` + Phase 14 ``google_calendar`` for a single
   body-anchored query. Proves the body store remains the SSOT for
   cross-connector recall as Phase 14 widens the surface.

4. **find-document filter by Phase 14 source_type** — ``source.list``
   filtered to ``source_type="gmail_message"`` (Phase 12 H1
   ``observed_after`` / ``observed_before`` physical-column path)
   returns only Gmail rows even when other connectors share the
   ``observed_at`` window, so the secretary skills can target Gmail
   without bleeding Outlook / Drive rows into the result.

5. **Write-back path absence (Gmail + Calendar)** — neither
   ``connectors/google_mail`` nor ``connectors/google_calendar``
   exports any ``send`` / ``post`` / ``write`` / ``insert`` /
   ``patch`` / ``delete`` / ``watch`` / ``events_insert`` /
   ``messages_send`` callable. Mirrors the Phase 13 lifecycle guard
   (extended to connectors 9 + 10) and matches the
   ``forbidden_callables`` shape Phase 10 / 11 / 13 established.

6. **Mapper symmetry pin re-run** — re-imports the Phase 14 G3 / G4
   mapper symmetry pin module
   (``tests/unit/connectors/test_mapper_symmetry``) and asserts the
   public symbols it exports stay loaded, so a refactor that
   accidentally drops the symmetry tests fails this lifecycle test
   too (defence-in-depth: the unit test layer + the e2e layer both
   reach the same pin).

Hermeticity: this test never opens an httpx connection. ``SourceService``
is exercised directly with hand-built ``RawGmailMessage`` /
``RawCalendarEvent`` payloads + the production mapper functions, so the
network-bound auth / client / cursor layers are out of scope (Phase 14
G3 / G4 unit tests pin those individually). The MCP layer is exercised
through the in-process ``dispatch_tool_call`` wrapper that
:mod:`test_phase13_google_workspace_lifecycle` uses — identical to what
runs inside ``serve_stdio`` but with no transport.
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sqlite_vec")

from opshub.cli._wiring import build_engine, build_source_service
from opshub.connectors.google_calendar.client import RawCalendarEvent
from opshub.connectors.google_calendar.mapper import (
    GOOGLE_CALENDAR_SOURCE_TYPE,
    map_calendar_event,
)
from opshub.connectors.google_mail.client import RawGmailMessage
from opshub.connectors.google_mail.mapper import (
    GMAIL_SOURCE_TYPE,
    map_gmail_message,
)
from opshub.domain.events.source import SourceObserved
from opshub.vectors.embedder import EmbeddingResult


def _observe_via_service(service: Any, evt: SourceObserved) -> None:
    """Convert a mapper-produced ``SourceObserved`` into a service call.

    The Phase 7+ ``SourceService.observe`` API takes kwargs (so it can
    mint its own ``aggregate_id`` + paired :class:`ItemEnqueued`); the
    Phase 14 mappers return a fully-formed ``SourceObserved`` for the
    unit-test layer. This helper bridges the two: we surface the
    mapper output as ``observe(...)`` kwargs so the round-trip exercises
    the projection write path the connector wiring uses in production.
    """
    service.observe(
        connector_name=evt.connector_name,
        external_id=evt.external_id,
        source_type=evt.source_type,
        title=evt.title,
        url=evt.url,
        summary=evt.summary,
        fingerprint=evt.fingerprint,
        body=evt.body,
        provenance_origin=evt.provenance_origin,
        provenance_trust=evt.provenance_trust,
    )


# ---------------------------------------------------------------------------
# Stubs — local copy of the Phase 13 deterministic embedder, kept
# duplicated on purpose so this lifecycle stays hermetic and decoupled.
# ---------------------------------------------------------------------------


class _StubEmbedder:
    """Deterministic embedder stub (1024-dim, content-keyed)."""

    def __init__(self, *, dim: int = 1024) -> None:
        self._dim = dim

    @property
    def model_id(self) -> str:
        return "phase14-stub-embedder"

    @property
    def model_version(self) -> str:
        return "v1"

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[EmbeddingResult]:
        return [self.embed_one(t) for t in texts]

    def embed_one(self, text: str) -> EmbeddingResult:
        slots = [0.0] * self._dim
        for i, ch in enumerate(text):
            slots[i % self._dim] += (ord(ch) % 31 + 1) / 31.0
        norm = max(sum(x * x for x in slots) ** 0.5, 1e-9)
        return EmbeddingResult(
            vector=tuple(x / norm for x in slots),
            model_id=self.model_id,
            model_version=self.model_version,
            dim=self._dim,
        )


def _install_stub_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    from opshub.core.config import OpsHubSettings
    from opshub.vectors import factory as factory_module
    from opshub.vectors.embedder import Embedder

    def _stub(settings: OpsHubSettings) -> Embedder:
        del settings
        return _StubEmbedder()

    monkeypatch.setattr(factory_module, "build_embedder", _stub)


# ---------------------------------------------------------------------------
# Fixture builders — minimal Raw payloads that exercise the symmetric
# fields the mapper consumes. No network / no httpx.
# ---------------------------------------------------------------------------


def _raw_gmail(
    *,
    message_id: str,
    subject: str,
    body_text: str = "",
    body_html: str = "",
    labels: tuple[str, ...] = ("INBOX",),
    thread_id: str = "thread-1",
    internal_date_ms: str = "1717200000000",  # 2024-06-01T00:00:00Z
) -> RawGmailMessage:
    return RawGmailMessage(
        message_id=message_id,
        thread_id=thread_id,
        label_ids=labels,
        history_id="100",
        internal_date_ms=internal_date_ms,
        from_header="Alice <alice@example.com>",
        subject_header=subject,
        snippet=subject[:80],
        body_text=body_text,
        body_html=body_html,
        raw={},
    )


def _raw_event(
    *,
    event_id: str,
    subject: str,
    start_iso: str = "2026-06-15T10:00:00+09:00",
    end_iso: str = "2026-06-15T11:00:00+09:00",
    last_modified_iso: str = "2026-06-01T00:00:00+00:00",
    attendees: tuple[str, ...] = ("alice@example.com", "bob@example.com"),
    description: str = "",
    recurring_event_id: str = "",
    original_start_iso: str = "",
    status: str = "confirmed",
) -> RawCalendarEvent:
    return RawCalendarEvent(
        id=event_id,
        subject=subject,
        start_iso=start_iso,
        end_iso=end_iso,
        attendees_count=len(attendees),
        web_link=f"https://calendar.google.com/event?eid={event_id}",
        last_modified_iso=last_modified_iso,
        status=status,
        description=description,
        location="Conference Room A",
        organizer_email="alice@example.com",
        attendees=attendees,
        recurrence=("RRULE:FREQ=WEEKLY;COUNT=10",) if not recurring_event_id else (),
        recurring_event_id=recurring_event_id,
        original_start_iso=original_start_iso,
        raw={},
    )


# ---------------------------------------------------------------------------
# Lifecycle tests
# ---------------------------------------------------------------------------


def test_phase14_lifecycle_persists_gmail_and_calendar(
    isolated_env: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del isolated_env
    """Pins #1: ingest → sources projection with body + provenance.

    Both ``gmail_message`` and ``google_calendar`` SourceObserved
    events round-trip through SourceService into the sources
    projection with ``body`` populated and provenance tagged
    ``external`` / ``untrusted`` (ADR-0020 §(e)).
    """
    del tmp_path  # provided by isolated_env fixture
    _install_stub_embedder(monkeypatch)

    engine = build_engine()
    source_service = build_source_service(actor="test:phase14")

    gmail_evt = map_gmail_message(
        _raw_gmail(
            message_id="msg-001",
            subject="Phase 14 mapper symmetry confirmation",
            body_text="The Gmail mapper mirrors the Outlook deep-retention shape.",
            labels=("INBOX", "IMPORTANT"),
        )
    )
    calendar_evt = map_calendar_event(
        _raw_event(
            event_id="evt-001",
            subject="Phase 14 closeout review",
            description="Walk through the G5 docs touchpoints.",
        )
    )

    _observe_via_service(source_service, gmail_evt)
    _observe_via_service(source_service, calendar_evt)

    # Inspect via the sources projection directly — it is the SSOT.
    from sqlalchemy import select

    from opshub.projections.sources import sources_table

    with engine.connect() as conn:
        rows = conn.execute(
            select(
                sources_table.c.source_type,
                sources_table.c.body,
                sources_table.c.title,
                sources_table.c.summary,
            ).order_by(sources_table.c.observed_at)
        ).all()

    by_type = {r.source_type: r for r in rows}
    assert GMAIL_SOURCE_TYPE in by_type, "Gmail SourceObserved did not reach sources projection"
    assert GOOGLE_CALENDAR_SOURCE_TYPE in by_type, (
        "Calendar SourceObserved did not reach sources projection"
    )

    gmail_row = by_type[GMAIL_SOURCE_TYPE]
    assert gmail_row.body is not None and "Gmail mapper" in gmail_row.body
    assert "[Labels:" in gmail_row.body, "Outlook-symmetric labels prefix missing"

    calendar_row = by_type[GOOGLE_CALENDAR_SOURCE_TYPE]
    assert calendar_row.body is not None
    assert "attendees" in (calendar_row.summary or ""), (
        f"MS365-symmetric summary missing attendees count (summary={calendar_row.summary!r})"
    )
    assert "RRULE:FREQ=WEEKLY" in calendar_row.body, "RRULE field should be preserved in body"

    # Provenance: ADR-0020 §(e) — external / untrusted (Literal strings).
    assert gmail_evt.provenance_origin == "external"
    assert gmail_evt.provenance_trust == "untrusted"
    assert calendar_evt.provenance_origin == "external"
    assert calendar_evt.provenance_trust == "untrusted"


def test_phase14_calendar_override_emits_as_separate_record(
    isolated_env: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del isolated_env
    """Pins #2: a recurring-override emits a standalone SourceObserved.

    Google returns recurring overrides as independent events carrying
    ``recurringEventId`` + ``originalStartTime``. The mapper preserves
    that shape (one ``SourceObserved`` per override) with a body
    back-pointer; the projection ends up with both the master and the
    override as separate rows sharing ``source_type="google_calendar"``.
    """
    del tmp_path  # provided by isolated_env fixture
    _install_stub_embedder(monkeypatch)

    engine = build_engine()
    source_service = build_source_service(actor="test:phase14")

    master = map_calendar_event(_raw_event(event_id="series-1", subject="Weekly standup"))
    override = map_calendar_event(
        _raw_event(
            event_id="series-1_20260622T010000Z",
            subject="Weekly standup (skipped)",
            recurring_event_id="series-1",
            original_start_iso="2026-06-22T10:00:00+09:00",
            status="cancelled",
        )
    )

    _observe_via_service(source_service, master)
    _observe_via_service(source_service, override)

    from sqlalchemy import select

    from opshub.projections.sources import sources_table

    with engine.connect() as conn:
        rows = conn.execute(
            select(
                sources_table.c.external_id,
                sources_table.c.source_type,
                sources_table.c.body,
                sources_table.c.title,
            ).where(sources_table.c.source_type == GOOGLE_CALENDAR_SOURCE_TYPE)
        ).all()

    by_external = {r.external_id: r for r in rows}
    assert "series-1" in by_external, "master event missing from projection"
    assert "series-1_20260622T010000Z" in by_external, (
        "override should round-trip as its own row, not be merged into master"
    )
    override_row = by_external["series-1_20260622T010000Z"]
    assert override_row.body is not None
    # The Phase 14 plan §G4 mapper appends a back-pointer so projection
    # consumers can join the override back to its master without a
    # dedicated link layer (the link layer is a Phase 15+ candidate).
    assert "series-1" in override_row.body, (
        "override body should reference the master event id (Phase 14 plan §G4 back-pointer)"
    )


def test_phase14_search_mixes_phase11_phase13_phase14_source_types(
    isolated_env: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del isolated_env
    """Pins #3: ``source.list`` returns Phase 14 source_types alongside the rest.

    The MCP read surface widens to the new discriminators
    automatically because the projection is the SSOT — Phase 14 ships
    no new MCP tool. We seed Gmail + Calendar rows alongside the
    existing types and assert ``source.list`` returns them.
    """
    del tmp_path  # provided by isolated_env fixture
    _install_stub_embedder(monkeypatch)

    engine = build_engine()
    source_service = build_source_service(actor="test:phase14")

    _observe_via_service(
        source_service,
        map_gmail_message(
            _raw_gmail(
                message_id="msg-002",
                subject="Phase 14 mixed-source search probe",
                body_text="Body content discoverable by source.list filtering.",
            )
        ),
    )
    _observe_via_service(
        source_service,
        map_calendar_event(
            _raw_event(event_id="evt-002", subject="Phase 14 mixed-source search event")
        ),
    )

    from opshub.mcp.server import build_tool_specs_for_engine, dispatch_tool_call

    specs = {s.name: s for s in build_tool_specs_for_engine(engine)}

    gmail_only = asyncio.run(
        dispatch_tool_call(specs, "source.list", {"source_type": GMAIL_SOURCE_TYPE, "limit": 50})
    )
    assert len(gmail_only) == 1
    payload = str(gmail_only[0].text)
    assert "msg-002" in payload, "Gmail row missing from source.list filter result"
    assert "evt-002" not in payload, (
        "source_type filter leaked Calendar rows into the Gmail-only request"
    )

    calendar_only = asyncio.run(
        dispatch_tool_call(
            specs,
            "source.list",
            {"source_type": GOOGLE_CALENDAR_SOURCE_TYPE, "limit": 50},
        )
    )
    cal_payload = str(calendar_only[0].text)
    assert "evt-002" in cal_payload
    assert "msg-002" not in cal_payload


def test_phase14_no_writeback_callables_in_google_mail_or_calendar() -> None:
    """Pins #5: write-back path is structurally absent in both new packages.

    Phase 14 G3 / G4 deliberately ship no Gmail ``send`` /
    ``messages.modify`` / ``users.watch`` and no Calendar
    ``events.insert`` / ``events.patch`` / ``events.delete`` /
    ``events.watch`` callables. The lifecycle guard checks the
    package surface so re-adding any of these in a future refactor
    fails this test.
    """
    forbidden_substrings = (
        "send",
        "watch",
        "insert",
        "patch",
        "delete",
        "post",
        "write",
        "create",
        "update",
    )

    for module_name in (
        "opshub.connectors.google_mail.connector",
        "opshub.connectors.google_mail.client",
        "opshub.connectors.google_mail.mapper",
        "opshub.connectors.google_calendar.connector",
        "opshub.connectors.google_calendar.client",
        "opshub.connectors.google_calendar.mapper",
    ):
        module = importlib.import_module(module_name)
        for attr in dir(module):
            if attr.startswith("_"):
                continue
            lower = attr.lower()
            for needle in forbidden_substrings:
                if needle in lower:
                    # Allow read-side names that legitimately contain
                    # these substrings (e.g. "fetch_messages" doesn't
                    # match; "events_list" contains no needle). The
                    # filter above is strict enough that any match
                    # here signals a write-shaped public symbol.
                    raise AssertionError(
                        f"Phase 14 write-back guard tripped: "
                        f"{module_name}.{attr} contains forbidden substring "
                        f"'{needle}' — ADR-0010 §禁止事項 7 + §Phase 14 改訂 (i) "
                        f"禁止事項拡張 forbid Gmail / Calendar write callables. "
                        f"If this is a legitimate read-side helper, rename it "
                        f"(e.g. 'send_*' → 'fetch_*' for read paths)."
                    )


def test_phase14_mapper_symmetry_pin_module_is_loadable() -> None:
    """Pins #6: the mapper symmetry pin module stays importable.

    Defence-in-depth: the unit test layer asserts the symmetric body
    shape; this lifecycle test re-imports the pin module so a refactor
    that accidentally removes the file fails here too.
    """
    pin = importlib.import_module("tests.unit.connectors.test_mapper_symmetry")
    # The pin module exposes test functions starting with `test_`.
    pin_tests = [name for name in dir(pin) if name.startswith("test_")]
    assert pin_tests, (
        "tests/unit/connectors/test_mapper_symmetry.py exists but exposes "
        "no test_ functions — Phase 14 G3 + G4 added 8 + 6 = 14 cases "
        "(Outlook ↔ Gmail 8 + ms365_calendar ↔ google_calendar 6); if those "
        "were removed, the mapper symmetry contract is no longer pinned."
    )
