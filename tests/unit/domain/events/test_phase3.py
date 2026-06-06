"""Tests for the Phase 3 domain events.

Covers all 5 new event classes plus the :data:`Phase3Event` and the
extended :data:`AllEvent` discriminated unions. The shape mirrors
``test_phase2.py`` so the conventions stay obvious to future readers:

- happy-path construction
- field validation (length bounds, ``Literal`` enums)
- ``frozen=True`` and ``extra="forbid"`` invariants
- round-trip through each union's ``TypeAdapter``
- the extended ``AllEvent`` still dispatches to Phase 1 + Phase 2 events
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from opshub.core.ids import new_ulid
from opshub.domain.events import (
    AllEvent,
    ConnectorSyncCompleted,
    ConnectorSyncFailed,
    ConnectorSyncStarted,
    FileIngested,
    ItemEnqueued,
    Phase3Event,
    SourceObserved,
    SourceReferenced,
    TaskCreated,
)

# Module-level singletons so each test pays the schema-build cost once.
_Phase3Adapter: TypeAdapter[Phase3Event] = TypeAdapter(Phase3Event)  # pyright: ignore[reportCallIssue]
_AllEventAdapter: TypeAdapter[AllEvent] = TypeAdapter(AllEvent)  # pyright: ignore[reportCallIssue]


def _agg() -> str:
    return new_ulid()


# ---- SourceObserved --------------------------------------------------------


def test_source_observed_minimal_fields() -> None:
    event = SourceObserved(
        aggregate_id=_agg(),
        actor="connector:github",
        connector_name="github",
        external_id="owner/repo#42",
        source_type="issue",
        title="something is broken",
        # epic #470 / issue #481: ``body`` is required + non-empty.
        body="full issue body",
    )
    assert event.event_type == "source.observed"
    assert event.schema_version == 1
    assert event.url is None
    assert event.summary is None
    assert event.body == "full issue body"


def test_source_observed_full_fields() -> None:
    event = SourceObserved(
        aggregate_id=_agg(),
        actor="connector:github",
        connector_name="github",
        external_id="owner/repo#42",
        source_type="pull_request",
        title="fix(foo): bar",
        url="https://github.com/owner/repo/pull/42",
        summary="rolls back the regression introduced in #41",
        body="PR description body",
    )
    assert event.url == "https://github.com/owner/repo/pull/42"
    assert event.summary == "rolls back the regression introduced in #41"
    assert event.body == "PR description body"


def test_source_observed_requires_non_empty_body() -> None:
    """epic #470 / issue #481 pins ``body`` to ``str = Field(min_length=1)``.

    A missing ``body`` (the Phase 10 backward-compat shim) and an
    empty / whitespace-only ``body`` both raise
    :class:`PydanticValidationError` so connectors that forget to
    substitute ``summary`` for ``body`` on metadata-only paths fail
    fast at construction time.
    """
    base_payload: dict[str, Any] = {
        "aggregate_id": _agg(),
        "actor": "connector:github",
        "connector_name": "github",
        "external_id": "owner/repo#1",
        "source_type": "issue",
        "title": "ok",
    }
    with pytest.raises(PydanticValidationError):
        SourceObserved(**base_payload)
    with pytest.raises(PydanticValidationError):
        SourceObserved(**base_payload, body="")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("connector_name", ""),
        ("connector_name", "x" * 51),
        ("external_id", ""),
        ("external_id", "x" * 201),
        ("source_type", ""),
        ("source_type", "x" * 51),
        ("title", ""),
        ("title", "x" * 501),
        # ADR-0005 + ADR-0010 Phase 7 Validation §3: every connector
        # caps the persisted summary at 200 unicode characters. The
        # schema-side cap fails fast if a connector ever forgets to
        # truncate, complementing the per-connector
        # ``SUMMARY_MAX_CHARS = 200`` constants (Slack / MS365 / Box /
        # GitHub mappers).
        ("summary", "x" * 201),
    ],
)
def test_source_observed_rejects_out_of_range_strings(field: str, value: str) -> None:
    payload: dict[str, Any] = {
        "aggregate_id": _agg(),
        "actor": "connector:github",
        "connector_name": "github",
        "external_id": "owner/repo#1",
        "source_type": "issue",
        "title": "ok",
        # epic #470 / issue #481: ``body`` is required + non-empty;
        # supply a placeholder so the test asserts the OTHER field's
        # validation, not body's.
        "body": "ok body",
    }
    payload[field] = value
    with pytest.raises(PydanticValidationError):
        SourceObserved(**payload)


def test_source_observed_accepts_max_length_strings() -> None:
    event = SourceObserved(
        aggregate_id=_agg(),
        actor="connector:github",
        connector_name="x" * 50,
        external_id="x" * 200,
        source_type="x" * 50,
        title="x" * 500,
        summary="x" * 200,
        body="x" * 500,
    )
    assert len(event.connector_name) == 50
    assert len(event.external_id) == 200
    assert len(event.source_type) == 50
    assert len(event.title) == 500
    assert event.summary is not None
    assert len(event.summary) == 200


# ---- SourceReferenced ------------------------------------------------------


@pytest.mark.parametrize("entity_type", ["task", "decision", "inbox_item"])
def test_source_referenced_accepts_allowed_entity_types(entity_type: str) -> None:
    event = SourceReferenced(
        aggregate_id=_agg(),
        actor="cli:triage",
        entity_type=entity_type,  # type: ignore[arg-type]
        entity_id=_agg(),
    )
    assert event.event_type == "source.referenced"
    assert event.entity_type == entity_type


def test_source_referenced_rejects_unknown_entity_type() -> None:
    with pytest.raises(PydanticValidationError):
        SourceReferenced(
            aggregate_id=_agg(),
            actor="cli:triage",
            entity_type="project",  # type: ignore[arg-type]
            entity_id=_agg(),
        )


def test_source_referenced_requires_entity_id() -> None:
    with pytest.raises(PydanticValidationError):
        SourceReferenced.model_validate(
            {
                "aggregate_id": _agg(),
                "actor": "cli:triage",
                "entity_type": "task",
            }
        )


def test_source_referenced_docstring_mentions_phase8_promotion() -> None:
    """Pin ADR-0017 closeout note on the docstring (Phase 8 step B1).

    Phase 3 introduced :class:`SourceReferenced` as a placeholder but
    no projector consumed it for the Phase 3-7 window. Phase 8
    (Knowledge graph, ADR-0017 §決定 (c)) closes that gap via the
    ``LinksProjector``. The docstring carries the first-class
    promotion note so future readers grepping for the event type land
    on the closeout context; this test asserts the note is present so
    a future refactor that drops it gets caught in CI.
    """
    doc = SourceReferenced.__doc__ or ""
    assert "Phase 8" in doc, "SourceReferenced docstring must mention Phase 8 closeout"
    assert "ADR-0017" in doc, "SourceReferenced docstring must reference ADR-0017"
    assert "LinksProjector" in doc, (
        "SourceReferenced docstring must mention the consumer (LinksProjector)"
    )


# ---- ConnectorSyncStarted --------------------------------------------------


def test_connector_sync_started_minimal_fields() -> None:
    event = ConnectorSyncStarted(
        aggregate_id=_agg(),
        actor="connector:github",
        connector_name="github",
    )
    assert event.event_type == "connector.sync_started"
    assert event.cursor_value is None


def test_connector_sync_started_with_cursor() -> None:
    event = ConnectorSyncStarted(
        aggregate_id=_agg(),
        actor="connector:github",
        connector_name="github",
        cursor_value="2026-05-16T10:00:00Z",
    )
    assert event.cursor_value == "2026-05-16T10:00:00Z"


@pytest.mark.parametrize("connector_name", ["", "x" * 51])
def test_connector_sync_started_rejects_out_of_range_name(connector_name: str) -> None:
    with pytest.raises(PydanticValidationError):
        ConnectorSyncStarted(
            aggregate_id=_agg(),
            actor="connector:github",
            connector_name=connector_name,
        )


# ---- ConnectorSyncCompleted ------------------------------------------------


def test_connector_sync_completed_minimal_fields() -> None:
    event = ConnectorSyncCompleted(
        aggregate_id=_agg(),
        actor="connector:github",
        connector_name="github",
        observed_count=0,
    )
    assert event.event_type == "connector.sync_completed"
    assert event.cursor_value is None
    assert event.observed_count == 0


def test_connector_sync_completed_with_cursor_and_count() -> None:
    event = ConnectorSyncCompleted(
        aggregate_id=_agg(),
        actor="connector:github",
        connector_name="github",
        cursor_value="2026-05-17T00:00:00Z",
        observed_count=17,
    )
    assert event.cursor_value == "2026-05-17T00:00:00Z"
    assert event.observed_count == 17


def test_connector_sync_completed_rejects_negative_count() -> None:
    with pytest.raises(PydanticValidationError):
        ConnectorSyncCompleted(
            aggregate_id=_agg(),
            actor="connector:github",
            connector_name="github",
            observed_count=-1,
        )


# ---- ConnectorSyncFailed ---------------------------------------------------


def test_connector_sync_failed_minimal_fields() -> None:
    event = ConnectorSyncFailed(
        aggregate_id=_agg(),
        actor="connector:github",
        connector_name="github",
        error_message="rate limited (HTTP 429)",
    )
    assert event.event_type == "connector.sync_failed"
    assert event.error_message == "rate limited (HTTP 429)"


def test_connector_sync_failed_rejects_empty_message() -> None:
    with pytest.raises(PydanticValidationError):
        ConnectorSyncFailed(
            aggregate_id=_agg(),
            actor="connector:github",
            connector_name="github",
            error_message="",
        )


def test_connector_sync_failed_rejects_overlong_message() -> None:
    with pytest.raises(PydanticValidationError):
        ConnectorSyncFailed(
            aggregate_id=_agg(),
            actor="connector:github",
            connector_name="github",
            error_message="x" * 2001,
        )


def test_connector_sync_failed_accepts_max_message() -> None:
    event = ConnectorSyncFailed(
        aggregate_id=_agg(),
        actor="connector:github",
        connector_name="github",
        error_message="x" * 2000,
    )
    assert len(event.error_message) == 2000


# ---- FileIngested ----------------------------------------------------------


_VALID_HASH = "a" * 64  # 64-char SHA-256 hex stand-in.


def test_file_ingested_minimal_fields() -> None:
    event = FileIngested(
        aggregate_id=_VALID_HASH,
        actor="cli:workspace_ingest",
        file_path="workspace/inbox/note.md",
        content_hash=_VALID_HASH,
        inbox_item_id=_agg(),
    )
    assert event.event_type == "workspace.file_ingested"
    assert event.schema_version == 1
    assert event.file_path == "workspace/inbox/note.md"
    assert event.content_hash == _VALID_HASH


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("file_path", ""),
        ("file_path", "x" * 2001),
        ("content_hash", ""),
        ("content_hash", "a" * 63),
        ("content_hash", "a" * 65),
    ],
)
def test_file_ingested_rejects_out_of_range_strings(field: str, value: str) -> None:
    payload: dict[str, Any] = {
        "aggregate_id": _VALID_HASH,
        "actor": "cli:workspace_ingest",
        "file_path": "workspace/inbox/note.md",
        "content_hash": _VALID_HASH,
        "inbox_item_id": _agg(),
    }
    payload[field] = value
    with pytest.raises(PydanticValidationError):
        FileIngested(**payload)


@pytest.mark.parametrize(
    "inbox_item_id",
    ["", "x" * 25, "x" * 27],
)
def test_file_ingested_rejects_non_ulid_inbox_item_id(inbox_item_id: str) -> None:
    with pytest.raises(PydanticValidationError):
        FileIngested(
            aggregate_id=_VALID_HASH,
            actor="cli:workspace_ingest",
            file_path="workspace/inbox/note.md",
            content_hash=_VALID_HASH,
            inbox_item_id=inbox_item_id,
        )


def test_file_ingested_accepts_max_length_file_path() -> None:
    event = FileIngested(
        aggregate_id=_VALID_HASH,
        actor="cli:workspace_ingest",
        file_path="x" * 2000,
        content_hash=_VALID_HASH,
        inbox_item_id=_agg(),
    )
    assert len(event.file_path) == 2000


# ---- frozen / extra=forbid -------------------------------------------------


def test_phase3_event_is_frozen() -> None:
    event = SourceObserved(
        aggregate_id=_agg(),
        actor="connector:github",
        connector_name="github",
        external_id="owner/repo#1",
        source_type="issue",
        title="t",
        body="body",
    )
    with pytest.raises(PydanticValidationError):
        event.title = "y"


def test_phase3_event_forbids_extra_fields() -> None:
    with pytest.raises(PydanticValidationError):
        SourceObserved.model_validate(
            {
                "aggregate_id": _agg(),
                "actor": "connector:github",
                "connector_name": "github",
                "external_id": "owner/repo#1",
                "source_type": "issue",
                "title": "t",
                "body": "body",
                "unexpected": "boom",
            }
        )


def test_phase3_event_rejects_wrong_event_type_literal() -> None:
    """The ``event_type`` Literal cannot be overridden to an arbitrary value."""
    with pytest.raises(PydanticValidationError):
        SourceObserved.model_validate(
            {
                "event_type": "source.invented",
                "aggregate_id": _agg(),
                "actor": "connector:github",
                "connector_name": "github",
                "external_id": "owner/repo#1",
                "source_type": "issue",
                "title": "t",
                "body": "body",
            }
        )


# ---- Phase3Event discriminated union ---------------------------------------


_PHASE3_FACTORIES: list[tuple[str, Any]] = [
    (
        "source.observed",
        lambda: SourceObserved(
            aggregate_id=_agg(),
            actor="connector:github",
            connector_name="github",
            external_id="owner/repo#1",
            source_type="issue",
            title="t",
            body="b",
        ),
    ),
    (
        "source.referenced",
        lambda: SourceReferenced(
            aggregate_id=_agg(),
            actor="cli:triage",
            entity_type="task",
            entity_id=_agg(),
        ),
    ),
    (
        "connector.sync_started",
        lambda: ConnectorSyncStarted(
            aggregate_id=_agg(),
            actor="connector:github",
            connector_name="github",
        ),
    ),
    (
        "connector.sync_completed",
        lambda: ConnectorSyncCompleted(
            aggregate_id=_agg(),
            actor="connector:github",
            connector_name="github",
            observed_count=3,
        ),
    ),
    (
        "connector.sync_failed",
        lambda: ConnectorSyncFailed(
            aggregate_id=_agg(),
            actor="connector:github",
            connector_name="github",
            error_message="boom",
        ),
    ),
    (
        "workspace.file_ingested",
        lambda: FileIngested(
            aggregate_id=_VALID_HASH,
            actor="cli:workspace_ingest",
            file_path="workspace/inbox/note.md",
            content_hash=_VALID_HASH,
            inbox_item_id=_agg(),
        ),
    ),
]


@pytest.mark.parametrize(
    ("event_type", "factory"),
    _PHASE3_FACTORIES,
    ids=[event_type for event_type, _ in _PHASE3_FACTORIES],
)
def test_phase3_event_roundtrip_via_model_dump(event_type: str, factory: Any) -> None:
    event = factory()
    assert event.event_type == event_type
    restored = _Phase3Adapter.validate_python(event.model_dump(mode="json"))
    assert restored == event
    assert type(restored) is type(event)


def test_phase3_event_rejects_task_event_payload() -> None:
    """A ``task.created`` payload must NOT be accepted by Phase3Event.

    The phase-scoped union should be conservative; the wider
    ``AllEvent`` deserializer is the place that knows about all phases.
    """
    payload = {
        "event_type": "task.created",
        "aggregate_id": _agg(),
        "actor": "cli:create",
        "title": "t",
    }
    with pytest.raises(PydanticValidationError):
        _Phase3Adapter.validate_python(payload)


def test_phase3_event_rejects_phase2_payload() -> None:
    """Phase 2 ``inbox.enqueued`` must NOT be accepted by Phase3Event either."""
    payload = {
        "event_type": "inbox.enqueued",
        "aggregate_id": _agg(),
        "actor": "cli:inbox",
        "summary": "x",
    }
    with pytest.raises(PydanticValidationError):
        _Phase3Adapter.validate_python(payload)


# ---- AllEvent extension ----------------------------------------------------


def test_all_event_dispatches_to_task_event() -> None:
    """Backwards-compat: ``AllEvent`` must still decode Phase 1 task events."""
    payload = {
        "event_type": "task.created",
        "aggregate_id": _agg(),
        "actor": "cli:create",
        "title": "still works",
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, TaskCreated)


def test_all_event_dispatches_to_phase2_event() -> None:
    """Backwards-compat: ``AllEvent`` must still decode Phase 2 events."""
    payload = {
        "event_type": "inbox.enqueued",
        "aggregate_id": _agg(),
        "actor": "cli:inbox",
        "summary": "from all-event",
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, ItemEnqueued)


def test_all_event_dispatches_to_phase3_event() -> None:
    """Forwards-compat: ``AllEvent`` must decode every Phase 3 event type."""
    payload = {
        "event_type": "source.observed",
        "aggregate_id": _agg(),
        "actor": "connector:github",
        "connector_name": "github",
        "external_id": "owner/repo#1",
        "source_type": "issue",
        "title": "from all-event",
        # epic #470 / issue #481: ``body`` is required + non-empty.
        "body": "from all-event body",
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, SourceObserved)


def test_all_event_dispatches_to_file_ingested() -> None:
    """``AllEvent`` must decode the workspace.file_ingested family too."""
    payload = {
        "event_type": "workspace.file_ingested",
        "aggregate_id": _VALID_HASH,
        "actor": "cli:workspace_ingest",
        "file_path": "workspace/inbox/note.md",
        "content_hash": _VALID_HASH,
        "inbox_item_id": _agg(),
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, FileIngested)


def test_all_event_rejects_unknown_event_type() -> None:
    payload = {
        "event_type": "phase42.future",
        "aggregate_id": _agg(),
        "actor": "cli:future",
    }
    with pytest.raises(PydanticValidationError):
        _AllEventAdapter.validate_python(payload)
