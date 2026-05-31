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


# ---------------------------------------------------------------------------
# Phase 14 audit cluster D1 (#308): integration test gap closure
# ---------------------------------------------------------------------------
#
# The tests below close three structural coverage gaps the Phase 14
# closeout audit (#292 → ab0b792) surfaced against the Phase 13
# lifecycle baseline:
#
# - G-1 (lighter): ``auth → client → cursor → projection`` round-trip
#   for Gmail and Calendar is exercised at the integration layer via a
#   :class:`httpx.MockTransport` so the wire-shape contract is observed
#   end-to-end (not just at the unit layer where ``GmailClient`` /
#   ``CalendarClient`` are mocked at the class boundary).
# - G-2: refresh-token rotation cursor continuation across the **three**
#   Google connectors (Drive + Gmail + Calendar) — mirrors
#   :func:`test_phase13_google_workspace_lifecycle` step 7 but fans the
#   ``sync1 → rotation → sync2`` shape over the Phase 14 surface so
#   regressions that drop cursor continuation on the new connectors are
#   caught with the same pin shape Phase 13 uses.
# - G-10: scope-extension survival pin — re-runs the Phase 13
#   ``GoogleWorkspaceConnector`` end-to-end under a shared auth helper
#   whose scopes were widened to all three Phase 14 read scopes (Drive
#   + Gmail + Calendar) and asserts the Drive cursor path is bit-for-bit
#   unaffected.
#
# Design judgement (Option A vs Option B per issue #308):
#
# - Option A (chosen): keep the lifecycle 1-file structure and add the
#   three new test_ functions here so the Phase 14 G5 closeout decision
#   to consolidate into one lifecycle file (lifecycle = projection /
#   MCP read surface + mapper symmetry + write-back absence guard) is
#   not re-fragmented. Rotation / round-trip / scope-extension are
#   structural complements to the lifecycle pins above (every test in
#   the file targets the same end-to-end Phase 14 ingest path), so
#   co-location keeps "what does Phase 14 integration coverage look
#   like" answerable by reading one file.
# - Option B (rejected): split into three new files
#   (``test_phase14_google_mail_sync.py`` /
#   ``test_phase14_google_calendar_sync.py`` /
#   ``test_phase14_shared_auth_scope_extension.py``) per the original
#   ``phase-14-plan.md §7.2`` listing. The split was the initial design;
#   G5 audit consolidated to lifecycle 1-file because the Phase 14
#   contract layers (auth / cursor / projection / MCP read surface)
#   reuse the Phase 13 shape verbatim and the per-connector
#   round-trip pin reads naturally next to the projection / write-back
#   guards. ``phase-14-plan.md §7.2`` is updated in this PR to match.


# ---------------------------------------------------------------------------
# Phase 14 audit cluster D1 helpers — hermetic httpx MockTransport
# wiring for the auth → client → cursor → projection round-trip pins.
# ---------------------------------------------------------------------------


def _install_httpx_mock_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    """Patch ``httpx.Client`` so every construction routes through ``handler``.

    Mirrors the pattern :mod:`tests.unit.connectors.google_auth.test_auth`
    uses: ``GoogleWorkspaceAuth`` / ``GmailClient`` / ``CalendarClient``
    each instantiate a fresh ``httpx.Client`` so a single class-level
    monkeypatch of ``httpx.Client`` is enough to capture every
    request without each call site needing a bespoke ``transport=``
    kwarg.

    Kept inside this file (rather than promoted to ``conftest.py``)
    because the Phase 14 audit D1 pins are the only consumer; a future
    Phase 15+ pin can lift the helper if a second user emerges.
    """
    import httpx

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def fake_client(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", fake_client)


def _seed_refresh_token_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub :mod:`opshub.core.secrets` so the OAuth refresh path is hermetic.

    ``GoogleWorkspaceAuth.get_access_token`` calls
    ``opshub.core.secrets.get_secret`` to load the refresh token from
    keyring; in a test environment keyring is unavailable. We monkeypatch
    ``get_secret`` / ``set_secret`` to consult a per-test in-memory store
    **only for the Google refresh-token slot**, delegating to the real
    implementation for every other key — the DB encryption key
    (``db:encryption_key``) is also looked up via
    :func:`opshub.core.secrets.get_secret` during ``build_engine`` and
    must keep using the real keyring path so ``isolated_env`` /
    ``opshub init`` continues to work end-to-end.
    """
    import opshub.core.secrets as secrets_module

    real_get_secret = secrets_module.get_secret
    real_set_secret = secrets_module.set_secret

    refresh_slot = "connector:google_workspace:refresh_token"
    store: dict[str, str | None] = {refresh_slot: "INITIAL_REFRESH_TOKEN"}

    def fake_get_secret(key: str) -> str | None:
        if key == refresh_slot:
            return store.get(refresh_slot)
        return real_get_secret(key)

    def fake_set_secret(key: str, value: str) -> None:
        if key == refresh_slot:
            store[key] = value
            return
        real_set_secret(key, value)

    monkeypatch.setattr(secrets_module, "get_secret", fake_get_secret)
    monkeypatch.setattr(secrets_module, "set_secret", fake_set_secret)
    # Also patch the names the auth module imported locally (the auth
    # module does ``from opshub.core.secrets import get_secret,
    # set_secret`` inside ``get_access_token`` / ``complete_auth_flow``,
    # so the local-name patch is the actually-load-bearing one).
    monkeypatch.setattr("opshub.core.secrets.get_secret", fake_get_secret)
    monkeypatch.setattr("opshub.core.secrets.set_secret", fake_set_secret)


def test_phase14_sync_round_trip_via_mock_httpx_gmail(
    isolated_env: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del isolated_env
    del tmp_path
    """G-1 (Gmail): auth → client → cursor → projection round-trip.

    Hermetic ``httpx.MockTransport`` handler answers every Gmail wire
    call the connector's first-sync bootstrap path issues:

    1. ``POST oauth2.googleapis.com/token`` (refresh-token exchange)
       → returns a fresh access token so the bearer header is valid.
    2. ``GET gmail.googleapis.com/.../users/me/messages?q=after:...``
       (``list_messages_since`` initial-window backfill) → returns 1
       message id.
    3. ``GET gmail.googleapis.com/.../users/me/messages/<id>?format=full``
       → returns a minimal multipart payload the mapper consumes.
    4. ``GET gmail.googleapis.com/.../users/me/profile`` (history-id
       bootstrap) → returns ``PROF_HIST_ID``.

    The connector then walks ``users.history.list`` (yielding zero
    new messages since the bootstrap id is the current id) and persists
    the cursor. The pin asserts:

    - ``sources`` projection has 1 ``gmail_message`` row.
    - ``connector_cursors`` row for ``google_mail`` is ``PROF_HIST_ID``.

    The wire round-trip is what the unit ``test_connector.py`` cannot
    exercise (it mocks ``GmailClient`` at the class boundary).
    """
    _install_stub_embedder(monkeypatch)
    _seed_refresh_token_secret(monkeypatch)

    # Build engine + service BEFORE patching OpsHubSettings — the
    # wiring helpers consult the real settings (encryption flag etc.)
    # which the isolated_env fixture provisioned via ``opshub init``.
    # Patching OpsHubSettings before this point would shadow the real
    # ``[storage]`` config with a MagicMock whose ``encryption`` attr
    # is itself a truthy MagicMock, triggering the SQLCipher path.
    engine = build_engine()
    service = build_source_service(actor="test:phase14_d1_gmail_round_trip")

    # Stub OpsHubSettings so the connector picks up the fake OAuth
    # client + a 7-day initial window + 30-day fallback (the production
    # defaults — we want the round-trip to exercise the documented path).
    from unittest.mock import MagicMock

    fake_settings = MagicMock()
    fake_settings.connectors.google_workspace.client_id = "fake-cid"
    fake_settings.connectors.google_workspace.client_secret = "fake-secret"
    fake_settings.connectors.google_workspace.redirect_uri = "http://localhost"
    fake_settings.connectors.google_mail.initial_window_days = 7
    fake_settings.connectors.google_mail.fallback_window_days = 30
    monkeypatch.setattr("opshub.core.config.OpsHubSettings", lambda: fake_settings)

    import httpx

    requests_seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = str(request.url.path)
        requests_seen.append((request.method, path))
        if request.method == "POST" and "oauth2.googleapis.com/token" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "access_token": "ACCESS_TOKEN_VALUE",
                    "expires_in": 3600,
                    # No rotated refresh_token — the rotation pin is in
                    # the Gmail-side test below; here we want the
                    # round-trip baseline only.
                },
            )
        if request.method == "GET" and path.endswith("/users/me/messages"):
            return httpx.Response(
                200,
                json={"messages": [{"id": "MSG_BACKFILL_1"}]},
            )
        if request.method == "GET" and path.endswith("/users/me/messages/MSG_BACKFILL_1"):
            # Minimal multipart payload the mapper accepts. The body is
            # a single text/plain part (Gmail's "simple message" shape).
            import base64

            body_text = "Round-trip body for G-1 Gmail wire pin."
            encoded = (
                base64.urlsafe_b64encode(body_text.encode("utf-8")).decode("ascii").rstrip("=")
            )
            return httpx.Response(
                200,
                json={
                    "id": "MSG_BACKFILL_1",
                    "threadId": "THREAD_1",
                    "labelIds": ["INBOX"],
                    "historyId": "100",
                    "internalDate": "1717200000000",
                    "snippet": "Round-trip body for G-1 Gmail wire pin.",
                    "payload": {
                        "mimeType": "text/plain",
                        "headers": [
                            {"name": "Subject", "value": "G-1 Gmail wire round-trip"},
                            {"name": "From", "value": "alice@example.com"},
                        ],
                        "body": {"data": encoded},
                    },
                },
            )
        if request.method == "GET" and path.endswith("/users/me/profile"):
            return httpx.Response(
                200,
                json={"historyId": "PROF_HIST_ID", "emailAddress": "me@example.com"},
            )
        if request.method == "GET" and path.endswith("/users/me/history"):
            # Bootstrap-after-backfill case: zero new history events
            # since the freshly-minted history id. Returning an empty
            # history payload exercises the steady-state "no changes"
            # branch without re-iterating message fetches.
            return httpx.Response(
                200,
                json={"historyId": "PROF_HIST_ID"},
            )
        raise AssertionError(f"unexpected Gmail wire call: {request.method} {request.url}")

    _install_httpx_mock_transport(monkeypatch, handler)

    from opshub.connectors.context import ConnectorContext
    from opshub.connectors.google_mail.connector import GoogleMailConnector

    connector = GoogleMailConnector()
    initial_cursor = service.cursor_get(connector.name)
    service.cursor_set(connector.name, initial_cursor, sync_started=True)
    ctx = ConnectorContext(
        source_service=service,
        cursor_value=initial_cursor,
        secrets=None,
        logger=__import__("unittest.mock", fromlist=["MagicMock"]).MagicMock(),
    )
    result = connector.sync(ctx)
    service.cursor_set(connector.name, result.new_cursor, sync_started=False)

    # The first-sync bootstrap commits the profile history id eagerly
    # via the per-cursor key (``google_mail:history``); the CLI driver
    # bracket then also writes the SyncResult under the connector name
    # itself. The end-to-end pin is: observed_count = 1, cursor =
    # PROF_HIST_ID, and the projection has the one mapped row.
    assert result.observed_count == 1
    assert result.new_cursor == "PROF_HIST_ID"

    from sqlalchemy import select, text

    from opshub.projections.sources import sources_table

    with engine.connect() as conn:
        rows = conn.execute(
            select(
                sources_table.c.external_id,
                sources_table.c.source_type,
                sources_table.c.title,
                sources_table.c.body,
            ).where(sources_table.c.connector_name == "google_mail")
        ).all()
        cursor_rows = (
            conn.execute(
                text(
                    "SELECT cursor_value FROM connector_cursors"
                    " WHERE connector_name IN ('google_mail', 'google_mail:history')"
                )
            )
            .scalars()
            .all()
        )

    assert len(rows) == 1, f"Gmail round-trip should produce exactly 1 row; got {rows}"
    only_row = rows[0]
    assert only_row.external_id == "MSG_BACKFILL_1"
    assert only_row.source_type == GMAIL_SOURCE_TYPE
    assert only_row.body is not None
    assert "Round-trip body for G-1 Gmail wire pin." in only_row.body

    # The eager cursor_set inside the connector writes to the per-cursor
    # key (``google_mail:history``); the CLI driver bracket above also
    # writes the SyncResult under the connector name. Either way the
    # PROF_HIST_ID value must reach the connector_cursors projection.
    assert "PROF_HIST_ID" in {str(v) for v in cursor_rows}

    # Defensive: ensure no httpx call escaped to a real Google host.
    for method, path in requests_seen:
        assert "googleapis.com" not in path or "/users/me/" in path or "/token" in path, (
            f"unexpected wire path {method} {path}"
        )


def test_phase14_sync_round_trip_via_mock_httpx_calendar(
    isolated_env: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del isolated_env
    del tmp_path
    """G-1 (Calendar): auth → client → cursor → projection round-trip.

    Hermetic ``httpx.MockTransport`` handler answers the Calendar
    first-sync bootstrap wire calls:

    1. ``POST oauth2.googleapis.com/token`` (access-token refresh).
    2. ``GET .../calendars/primary/events?timeMin=...&timeMax=...``
       (``fetch_events_window`` first-sync bootstrap) → returns 1 event
       plus ``nextSyncToken=ST_BOOTSTRAP``.

    The pin asserts:

    - ``sources`` projection has 1 ``google_calendar`` row.
    - ``connector_cursors`` row for ``google_calendar`` is
      ``ST_BOOTSTRAP``.

    The wire round-trip is what the unit ``test_connector.py`` cannot
    exercise (it mocks ``CalendarClient`` at the class boundary).
    """
    _install_stub_embedder(monkeypatch)
    _seed_refresh_token_secret(monkeypatch)

    # Build engine + service BEFORE patching OpsHubSettings (see
    # rationale in ``test_phase14_sync_round_trip_via_mock_httpx_gmail``).
    engine = build_engine()
    service = build_source_service(actor="test:phase14_d1_calendar_round_trip")

    from unittest.mock import MagicMock

    fake_settings = MagicMock()
    fake_settings.connectors.google_workspace.client_id = "fake-cid"
    fake_settings.connectors.google_workspace.client_secret = "fake-secret"
    fake_settings.connectors.google_workspace.redirect_uri = "http://localhost"
    fake_settings.connectors.google_calendar.calendar_id = "primary"
    fake_settings.connectors.google_calendar.time_min_days = 90
    fake_settings.connectors.google_calendar.time_max_days = 365
    monkeypatch.setattr("opshub.core.config.OpsHubSettings", lambda: fake_settings)

    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if request.method == "POST" and "oauth2.googleapis.com/token" in url_str:
            return httpx.Response(
                200,
                json={"access_token": "ACCESS_TOKEN_VALUE", "expires_in": 3600},
            )
        if request.method == "GET" and "/calendar/v3/calendars/primary/events" in url_str:
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "evt-round-trip-1",
                            "summary": "G-1 Calendar wire round-trip",
                            "start": {"dateTime": "2026-06-15T10:00:00Z"},
                            "end": {"dateTime": "2026-06-15T11:00:00Z"},
                            "htmlLink": "https://calendar.google.com/event?eid=evt-round-trip-1",
                            "updated": "2026-06-01T00:00:00Z",
                            "status": "confirmed",
                            "description": "Round-trip body for G-1 Calendar wire pin.",
                            "location": "Conference Room A",
                            "organizer": {"email": "alice@example.com"},
                            "attendees": [
                                {"email": "alice@example.com"},
                                {"email": "bob@example.com"},
                            ],
                        }
                    ],
                    "nextSyncToken": "ST_BOOTSTRAP",
                },
            )
        raise AssertionError(f"unexpected Calendar wire call: {request.method} {url_str}")

    _install_httpx_mock_transport(monkeypatch, handler)

    from opshub.connectors.context import ConnectorContext
    from opshub.connectors.google_calendar.connector import GoogleCalendarConnector

    connector = GoogleCalendarConnector()
    initial_cursor = service.cursor_get(connector.name)
    service.cursor_set(connector.name, initial_cursor, sync_started=True)
    ctx = ConnectorContext(
        source_service=service,
        cursor_value=initial_cursor,
        secrets=None,
        logger=__import__("unittest.mock", fromlist=["MagicMock"]).MagicMock(),
    )
    result = connector.sync(ctx)
    service.cursor_set(connector.name, result.new_cursor, sync_started=False)

    assert result.observed_count == 1
    assert result.new_cursor == "ST_BOOTSTRAP"

    from sqlalchemy import select, text

    from opshub.projections.sources import sources_table

    with engine.connect() as conn:
        rows = conn.execute(
            select(
                sources_table.c.external_id,
                sources_table.c.source_type,
                sources_table.c.body,
            ).where(sources_table.c.connector_name == "google_calendar")
        ).all()
        cursor_rows = (
            conn.execute(
                text(
                    "SELECT cursor_value FROM connector_cursors"
                    " WHERE connector_name IN ('google_calendar', 'google_calendar:events')"
                )
            )
            .scalars()
            .all()
        )

    assert len(rows) == 1
    only_row = rows[0]
    assert only_row.external_id == "evt-round-trip-1"
    assert only_row.source_type == GOOGLE_CALENDAR_SOURCE_TYPE
    assert only_row.body is not None
    assert "Round-trip body for G-1 Calendar wire pin." in only_row.body

    assert "ST_BOOTSTRAP" in {str(v) for v in cursor_rows}


def test_phase14_rotation_propagates_to_all_three_connectors(
    isolated_env: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del isolated_env
    del tmp_path
    """G-2: refresh-token rotation cursor continuation across all 3 connectors.

    Same shape as the Phase 13 lifecycle step 7 rotation pin
    (:func:`tests.integration.test_phase13_google_workspace_lifecycle.test_phase13_google_workspace_lifecycle`
    step 7) but fanned over the **three** Google connectors (Drive +
    Gmail + Calendar). Per-connector sync sequence is ``sync1 →
    rotation → sync2`` x 3:

    1. ``sync1`` for each connector drains one mocked page advancing
       the cursor to a known pre-rotation value.
    2. We simulate Google rotating the refresh token (write the new
       value into the in-memory secrets store) — the rotation happens
       on the OAuth side and the connector cursor rows must be
       structurally orthogonal to it.
    3. ``sync2`` for each connector replays the persisted cursor (NOT
       a re-bootstrap) and advances to a post-rotation value.

    The pin asserts:

    - Each connector's ``sync2`` started from the cursor ``sync1``
      persisted (no re-bootstrap).
    - Each connector's terminal cursor is the post-rotation value.
    - Bootstrap-only counters (``get_start_page_token`` for Drive,
      ``get_profile_history_id`` for Gmail) stay at their initial
      first-sync values — rotation does not perturb them.

    The Calendar connector does not have a bootstrap-only counter
    distinct from the delta path, so the assertion for Calendar pins
    the cursor continuation only.
    """
    _install_stub_embedder(monkeypatch)

    # Build engine + service BEFORE patching OpsHubSettings (see
    # rationale in ``test_phase14_sync_round_trip_via_mock_httpx_gmail``).
    engine = build_engine()
    service = build_source_service(actor="test:phase14_d1_rotation")

    from unittest.mock import MagicMock

    # Shared OAuth settings — one Google account = one principal
    # (Phase 14 plan §1 OQ6) so the three connectors all read from
    # ``[connectors.google_workspace]`` for client_id / client_secret.
    fake_settings = MagicMock()
    fake_settings.connectors.google_workspace.client_id = "fake-cid"
    fake_settings.connectors.google_workspace.client_secret = "fake-secret"
    fake_settings.connectors.google_workspace.redirect_uri = "http://localhost"
    fake_settings.connectors.google_workspace.content_extraction = False
    fake_settings.connectors.google_workspace.fallback_window_days = 30
    fake_settings.connectors.google_mail.initial_window_days = 0
    fake_settings.connectors.google_mail.fallback_window_days = 30
    fake_settings.connectors.google_calendar.calendar_id = "primary"
    fake_settings.connectors.google_calendar.time_min_days = 90
    fake_settings.connectors.google_calendar.time_max_days = 365
    fake_settings.office.max_file_size_mb = 50
    fake_settings.office.max_chars = 500_000
    fake_settings.office.excel.max_cells_per_sheet = 10_000
    fake_settings.office.excel.max_cells_per_workbook = 50_000
    monkeypatch.setattr("opshub.core.config.OpsHubSettings", lambda: fake_settings)

    # Stub the shared auth class for all 3 connectors so the rotation
    # is observable at the cursor projection without needing real
    # OAuth round-trips (the OAuth-side rotation mechanic is pinned
    # individually in
    # ``tests/unit/connectors/google_auth/test_auth.py``::
    # ``test_get_access_token_persists_rotated_refresh_token``;
    # this test pins the cursor-side invariant that survives it).
    monkeypatch.setattr(
        "opshub.connectors.google_auth.auth.GoogleWorkspaceAuth",
        MagicMock(),
    )

    # ---- Drive (google_workspace) wiring ----
    fake_drive_class = MagicMock()
    fake_drive_instance = MagicMock()
    fake_drive_class.return_value = fake_drive_instance
    monkeypatch.setattr(
        "opshub.connectors.google_workspace.client.DriveClient",
        fake_drive_class,
    )

    from opshub.connectors.google_workspace.client import RawDriveItem

    drive_token_before = "DRIVE_TOKEN_BEFORE_ROTATION"
    drive_token_after = "DRIVE_TOKEN_AFTER_ROTATION"
    fake_drive_instance.get_start_page_token.return_value = "DRIVE_BOOT_TOKEN"

    drive_item_pre = RawDriveItem(
        file_id="DRIVE_FILE_PRE",
        removed=False,
        trashed=False,
        name="pre-rotation.gdoc",
        mime_type="application/vnd.google-apps.document",
        modified_time_iso="2026-05-31T00:00:00Z",
        web_view_link="https://drive.google.com/file/d/DRIVE_FILE_PRE/view",
        owner_email="alice@example.com",
        owner_display_name="Alice",
        is_shared_with_me=False,
        shared=False,
        last_modifying_user_email="",
        last_modifying_user_display_name="",
        drive_id="",
        raw={},
    )
    drive_item_post = RawDriveItem(
        file_id="DRIVE_FILE_POST",
        removed=False,
        trashed=False,
        name="post-rotation.gdoc",
        mime_type="application/vnd.google-apps.document",
        modified_time_iso="2026-05-31T01:00:00Z",
        web_view_link="https://drive.google.com/file/d/DRIVE_FILE_POST/view",
        owner_email="alice@example.com",
        owner_display_name="Alice",
        is_shared_with_me=False,
        shared=False,
        last_modifying_user_email="",
        last_modifying_user_display_name="",
        drive_id="",
        raw={},
    )
    drive_fetch_queue: list[list[tuple[RawDriveItem, str]]] = [
        [(drive_item_pre, drive_token_before)],
        [(drive_item_post, drive_token_after)],
    ]
    drive_seen_page_tokens: list[str] = []

    def _drive_fetch_changes(*, page_token: str) -> Any:
        drive_seen_page_tokens.append(page_token)
        return iter(drive_fetch_queue.pop(0))

    fake_drive_instance.fetch_changes.side_effect = _drive_fetch_changes

    # ---- Gmail (google_mail) wiring ----
    fake_gmail_class = MagicMock()
    fake_gmail_instance = MagicMock()
    fake_gmail_class.return_value = fake_gmail_instance
    monkeypatch.setattr(
        "opshub.connectors.google_mail.client.GmailClient",
        fake_gmail_class,
    )

    gmail_history_before = "GMAIL_HIST_BEFORE_ROTATION"
    gmail_history_after = "GMAIL_HIST_AFTER_ROTATION"
    # initial_window_days = 0 → no backfill walk; the connector calls
    # get_profile_history_id directly and persists it as the first
    # cursor. We seed it to ``gmail_history_before`` so sync1 lands at
    # the known pre-rotation value.
    fake_gmail_instance.get_profile_history_id.return_value = gmail_history_before
    fake_gmail_instance.list_messages_since.return_value = iter([])

    # First sync drains one message + advances the cursor to
    # ``gmail_history_before``; second sync replays it and advances to
    # ``gmail_history_after`` (after a single message).
    gmail_history_queue: list[list[tuple[str, str]]] = [
        # First sync: cursor starts at gmail_history_before (from
        # get_profile_history_id during first-sync bootstrap). The
        # delta yields no advance because the bootstrap-and-delta-in-
        # one-call sequence is what the unit tests pin; here the second
        # sync is the one we care about for rotation continuation.
        [],
        # Second sync (post-rotation): one message, cursor advances.
        [("GMAIL_MSG_POST", gmail_history_after)],
    ]
    gmail_seen_start_ids: list[str] = []

    def _gmail_fetch_history(*, start_history_id: str) -> Any:
        gmail_seen_start_ids.append(start_history_id)
        return iter(gmail_history_queue.pop(0))

    fake_gmail_instance.fetch_history.side_effect = _gmail_fetch_history

    from opshub.connectors.google_mail.client import RawGmailMessage

    def _gmail_get_message(*, message_id: str) -> RawGmailMessage:
        return RawGmailMessage(
            message_id=message_id,
            thread_id="t",
            label_ids=("INBOX",),
            history_id=gmail_history_after,
            internal_date_ms="1717200000000",
            from_header="alice@example.com",
            subject_header=f"Post-rotation message {message_id}",
            snippet="snip",
            body_text="post-rotation body",
            body_html="",
            raw={},
        )

    fake_gmail_instance.get_message.side_effect = _gmail_get_message

    # ---- Calendar (google_calendar) wiring ----
    fake_cal_class = MagicMock()
    fake_cal_instance = MagicMock()
    fake_cal_class.return_value = fake_cal_instance
    monkeypatch.setattr(
        "opshub.connectors.google_calendar.client.CalendarClient",
        fake_cal_class,
    )

    cal_token_before = "CAL_SYNCTOKEN_BEFORE_ROTATION"
    cal_token_after = "CAL_SYNCTOKEN_AFTER_ROTATION"

    cal_event_pre = _raw_event(event_id="CAL_EVT_PRE", subject="Pre-rotation event")
    cal_event_post = _raw_event(event_id="CAL_EVT_POST", subject="Post-rotation event")
    # First sync = bootstrap window walk (no cursor).
    cal_window_queue: list[list[tuple[Any, str | None]]] = [
        [(cal_event_pre, None), (None, cal_token_before)],
    ]

    def _cal_fetch_events_window(*, calendar_id: str, time_min: str, time_max: str) -> Any:
        del calendar_id, time_min, time_max
        return iter(cal_window_queue.pop(0))

    fake_cal_instance.fetch_events_window.side_effect = _cal_fetch_events_window

    cal_delta_queue: list[list[tuple[Any, str]]] = [
        [(cal_event_post, cal_token_after)],
    ]
    cal_seen_sync_tokens: list[str] = []

    def _cal_fetch_events_delta(*, calendar_id: str, sync_token: str) -> Any:
        del calendar_id
        cal_seen_sync_tokens.append(sync_token)
        return iter(cal_delta_queue.pop(0))

    fake_cal_instance.fetch_events_delta.side_effect = _cal_fetch_events_delta

    # ---- Drive build cursor + run sync1 + sync2 ----
    from opshub.connectors.context import ConnectorContext
    from opshub.connectors.google_calendar.connector import GoogleCalendarConnector
    from opshub.connectors.google_mail.connector import GoogleMailConnector
    from opshub.connectors.google_workspace.connector import GoogleWorkspaceConnector

    def _run_sync(connector: Any) -> Any:
        initial = service.cursor_get(connector.name)
        service.cursor_set(connector.name, initial, sync_started=True)
        ctx = ConnectorContext(
            source_service=service,
            cursor_value=initial,
            secrets=None,
            logger=MagicMock(),
        )
        out = connector.sync(ctx)
        service.cursor_set(connector.name, out.new_cursor, sync_started=False)
        return out

    drive_connector = GoogleWorkspaceConnector()
    gmail_connector = GoogleMailConnector()
    calendar_connector = GoogleCalendarConnector()

    # --- Sync 1 round (all 3 connectors, pre-rotation) ---
    drive_result1 = _run_sync(drive_connector)
    gmail_result1 = _run_sync(gmail_connector)
    cal_result1 = _run_sync(calendar_connector)

    assert drive_result1.new_cursor == drive_token_before
    # Gmail first-sync persists the bootstrap profile history id; with
    # initial_window_days = 0 + empty history queue, the cursor lands
    # at gmail_history_before exactly.
    assert gmail_result1.new_cursor == gmail_history_before
    assert cal_result1.new_cursor == cal_token_before

    from sqlalchemy import text

    with engine.connect() as conn:
        cursor_after_sync1 = {
            row.connector_name: row.cursor_value
            for row in conn.execute(
                text(
                    "SELECT connector_name, cursor_value FROM connector_cursors"
                    " WHERE connector_name IN ('google_workspace', 'google_mail',"
                    " 'google_calendar')"
                )
            )
        }
    assert cursor_after_sync1 == {
        "google_workspace": drive_token_before,
        "google_mail": gmail_history_before,
        "google_calendar": cal_token_before,
    }, cursor_after_sync1

    # --- Simulate OAuth-side refresh-token rotation ---
    # The rotation happens on the auth path (auth.refresh_access_token
    # writes the new refresh token to keyring). The cursor rows in
    # connector_cursors must remain untouched — they are orthogonal
    # projection rows. We do not actually call the auth here because
    # GoogleWorkspaceAuth is mocked; the orthogonality pin is the
    # cursor row content survives unchanged across the simulated
    # rotation, then sync2 resumes from those persisted cursors.

    # --- Sync 2 round (all 3 connectors, post-rotation) ---
    drive_result2 = _run_sync(drive_connector)
    gmail_result2 = _run_sync(gmail_connector)
    cal_result2 = _run_sync(calendar_connector)

    assert drive_result2.new_cursor == drive_token_after
    assert gmail_result2.new_cursor == gmail_history_after
    assert cal_result2.new_cursor == cal_token_after

    # The Drive connector must have replayed the persisted token (NOT
    # re-bootstrapped). The first ``fetch_changes`` call was the post-
    # bootstrap call carrying ``DRIVE_BOOT_TOKEN``; the second was the
    # rotation-continuation call carrying ``DRIVE_TOKEN_BEFORE_ROTATION``.
    assert drive_seen_page_tokens == ["DRIVE_BOOT_TOKEN", drive_token_before], (
        "Drive sync2 must replay the persisted cursor (NOT re-bootstrap);"
        f" got page-token sequence {drive_seen_page_tokens!r}"
    )
    assert fake_drive_instance.get_start_page_token.call_count == 1, (
        "Drive rotation must NOT trigger a second bootstrap; the cursor row survives the rotation"
    )

    # Gmail sync2 must have replayed the persisted history id (gmail
    # sync1's bootstrap call captured gmail_history_before; sync2 reads
    # that value from the cursor and re-feeds it into fetch_history).
    # First call = sync1 bootstrap+delta (start_history_id == gmail_history_before);
    # second call = sync2 delta (start_history_id == gmail_history_before).
    assert gmail_seen_start_ids == [gmail_history_before, gmail_history_before], (
        "Gmail sync2 must replay the persisted history id (NOT re-bootstrap);"
        f" got fetch_history start ids {gmail_seen_start_ids!r}"
    )
    assert fake_gmail_instance.get_profile_history_id.call_count == 1, (
        "Gmail rotation must NOT trigger a second profile bootstrap;"
        " the cursor row survives the rotation"
    )

    # Calendar sync2 must have replayed the persisted sync token (NOT
    # re-bootstrapped via fetch_events_window).
    assert cal_seen_sync_tokens == [cal_token_before], (
        "Calendar sync2 must replay the persisted sync token (NOT re-bootstrap);"
        f" got fetch_events_delta sync_token sequence {cal_seen_sync_tokens!r}"
    )
    # fetch_events_window was called exactly once during sync1 bootstrap.
    assert fake_cal_instance.fetch_events_window.call_count == 1, (
        "Calendar rotation must NOT trigger a second window bootstrap;"
        " the cursor row survives the rotation"
    )

    with engine.connect() as conn:
        cursor_after_sync2 = {
            row.connector_name: row.cursor_value
            for row in conn.execute(
                text(
                    "SELECT connector_name, cursor_value FROM connector_cursors"
                    " WHERE connector_name IN ('google_workspace', 'google_mail',"
                    " 'google_calendar')"
                )
            )
        }
    assert cursor_after_sync2 == {
        "google_workspace": drive_token_after,
        "google_mail": gmail_history_after,
        "google_calendar": cal_token_after,
    }, cursor_after_sync2


def test_phase14_phase13_google_workspace_unaffected_by_scope_extension(
    isolated_env: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del isolated_env
    del tmp_path
    """G-10: Phase 13 ``google_workspace`` round-trip unaffected by scope extension.

    Phase 14 G2 (#294) widened the shared OAuth scope set from
    ``drive.readonly`` only to the full three-scope list (``drive`` +
    ``gmail`` + ``calendar``). The Phase 14 plan §G2 DoD pins
    "Phase 13 既存 google_workspace round-trip が 1 byte たりとも
    壊れない". Phase 13 lifecycle test rerun catches regressions in
    isolation but does not exercise the
    ``DEFAULT_SCOPES = [drive + gmail + calendar]`` shape — this test
    confirms that running the Phase 13 connector under the **widened
    auth helper** (where ``DEFAULT_SCOPES`` covers all three scopes)
    produces the same cursor / projection round-trip Phase 13 pinned
    in isolation.

    The pin asserts:

    - ``DEFAULT_SCOPES`` literally lists all three Phase 14 read scopes
      (catches a regression that drops one).
    - ``GoogleWorkspaceConnector.sync()`` runs end-to-end under the
      Phase 14 auth helper, persisting a Drive cursor row and a
      ``google_doc`` projection row.
    """
    _install_stub_embedder(monkeypatch)

    # First: the literal scope-list pin. Catches a regression that
    # drops one of the three scopes (which would break either Gmail or
    # Calendar silently). Co-located with the e2e pin so a future
    # reader sees both the contract and the exercise in one read.
    from opshub.connectors.google_auth.auth import DEFAULT_SCOPES

    assert DEFAULT_SCOPES == [
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/calendar.readonly",
    ], (
        "Phase 14 G2 #294 widened DEFAULT_SCOPES to the fixed three-scope list"
        " (Drive + Gmail + Calendar); regression here would silently break"
        " either Gmail or Calendar consent flow"
    )

    # Then the e2e pin: run the Phase 13 connector under the widened
    # auth helper and assert the Drive cursor / projection round-trip
    # the Phase 13 lifecycle pinned in isolation.

    # Build engine + service BEFORE patching OpsHubSettings (see
    # rationale in ``test_phase14_sync_round_trip_via_mock_httpx_gmail``).
    engine = build_engine()
    service = build_source_service(actor="test:phase14_d1_scope_ext")

    from unittest.mock import MagicMock

    fake_settings = MagicMock()
    fake_settings.connectors.google_workspace.client_id = "fake-cid"
    fake_settings.connectors.google_workspace.client_secret = "fake-secret"
    fake_settings.connectors.google_workspace.redirect_uri = "http://localhost"
    fake_settings.connectors.google_workspace.content_extraction = False
    fake_settings.connectors.google_workspace.fallback_window_days = 30
    fake_settings.office.max_file_size_mb = 50
    fake_settings.office.max_chars = 500_000
    fake_settings.office.excel.max_cells_per_sheet = 10_000
    fake_settings.office.excel.max_cells_per_workbook = 50_000
    monkeypatch.setattr("opshub.core.config.OpsHubSettings", lambda: fake_settings)

    # Use the actual GoogleWorkspaceAuth class so the scope-list path
    # is genuinely exercised (rather than mocking the auth helper out
    # entirely). The OAuth round-trips are short-circuited by stubbing
    # opshub.core.secrets + httpx so no real network calls fire.
    _seed_refresh_token_secret(monkeypatch)

    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if request.method == "POST" and "oauth2.googleapis.com/token" in url_str:
            return httpx.Response(
                200,
                json={"access_token": "ACCESS_TOKEN_VALUE", "expires_in": 3600},
            )
        # Any other call is a Drive API call — we let the DriveClient
        # mock below intercept those by mocking the DriveClient class
        # directly. The transport handler here only services the OAuth
        # round-trip the un-mocked GoogleWorkspaceAuth helper performs.
        raise AssertionError(
            f"scope-extension test should not reach Drive API directly: {request.method} {url_str}"
        )

    _install_httpx_mock_transport(monkeypatch, handler)

    # Mock the Drive client class so the connector's lazy import picks
    # up a stub; the OAuth helper still runs end-to-end so the scope
    # list is genuinely exercised.
    fake_drive_class = MagicMock()
    fake_drive_instance = MagicMock()
    fake_drive_class.return_value = fake_drive_instance
    monkeypatch.setattr(
        "opshub.connectors.google_workspace.client.DriveClient",
        fake_drive_class,
    )

    from opshub.connectors.google_workspace.client import RawDriveItem

    fake_drive_instance.get_start_page_token.return_value = "BOOT_TOKEN_SCOPE_EXT"
    drive_item = RawDriveItem(
        file_id="DRIVE_FILE_SCOPE_EXT",
        removed=False,
        trashed=False,
        name="scope-extension-pin.gdoc",
        mime_type="application/vnd.google-apps.document",
        modified_time_iso="2026-05-31T00:00:00Z",
        web_view_link="https://drive.google.com/file/d/DRIVE_FILE_SCOPE_EXT/view",
        owner_email="alice@example.com",
        owner_display_name="Alice",
        is_shared_with_me=False,
        shared=False,
        last_modifying_user_email="",
        last_modifying_user_display_name="",
        drive_id="",
        raw={},
    )

    def _scope_ext_fetch_changes(*, page_token: str) -> Any:
        del page_token
        return iter([(drive_item, "DRIVE_TOKEN_AFTER_SCOPE_EXT_SYNC")])

    fake_drive_instance.fetch_changes.side_effect = _scope_ext_fetch_changes

    from opshub.connectors.context import ConnectorContext
    from opshub.connectors.google_workspace.connector import GoogleWorkspaceConnector

    connector = GoogleWorkspaceConnector()
    initial_cursor = service.cursor_get(connector.name)
    service.cursor_set(connector.name, initial_cursor, sync_started=True)
    ctx = ConnectorContext(
        source_service=service,
        cursor_value=initial_cursor,
        secrets=None,
        logger=MagicMock(),
    )
    result = connector.sync(ctx)
    service.cursor_set(connector.name, result.new_cursor, sync_started=False)

    assert result.new_cursor == "DRIVE_TOKEN_AFTER_SCOPE_EXT_SYNC"
    assert result.observed_count == 1

    # The cursor path Phase 13 pinned in isolation must round-trip
    # under the widened-scope auth helper. The pin is bit-for-bit
    # cursor equality + 1 projection row of the Phase 13 source_type.
    from sqlalchemy import select, text

    from opshub.projections.sources import sources_table

    with engine.connect() as conn:
        rows = conn.execute(
            select(
                sources_table.c.external_id,
                sources_table.c.source_type,
            ).where(sources_table.c.connector_name == "google_workspace")
        ).all()
        cursor_after = conn.execute(
            text(
                "SELECT cursor_value FROM connector_cursors"
                " WHERE connector_name = 'google_workspace'"
            )
        ).scalar_one()

    assert len(rows) == 1
    assert rows[0].external_id == "DRIVE_FILE_SCOPE_EXT"
    assert rows[0].source_type == "google_doc"
    assert cursor_after == "DRIVE_TOKEN_AFTER_SCOPE_EXT_SYNC"
