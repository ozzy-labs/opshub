"""Tests for the Phase 6 proposal domain events (Action loop, ADR-0016).

Covers all 5 new event classes plus the :data:`Candidate`
discriminated union and their dispatch through the unified
:data:`AllEvent` union. The shape mirrors ``test_briefing.py`` so the
conventions stay obvious to future readers:

- happy-path construction for each event
- field validation (length bounds, non-negative token counts,
  candidate-list bounds)
- ``frozen=True`` and ``extra="forbid"`` invariants on events and on
  the discriminated-union payloads
- ``occurred_at`` / ``recorded_at`` honour ``AfterValidator(to_utc)``
- ``candidates`` round-trip through JSON (list of discriminated union)
- ``Candidate`` discriminator dispatch on ``kind``
- ``schema_version: Literal["v1"]`` enforcement on candidates
- ``ProposalFailed.error_message`` does NOT auto-sanitise (Phase 5 B1
  contract — sanitisation is the caller's responsibility)
- round-trip through ``AllEvent``'s ``TypeAdapter``
- ``AllEvent`` still dispatches to Phase 1 / 2 / 3 / 4 / 5 events

Phase-scoped grouping aliases (``Phase2Event`` ... ``Phase8Event``) were
dropped in epic #470 — :data:`AllEvent` is the single discriminated
union over every event family OpsHub knows how to decode.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from opshub.core.errors import ValidationError as OpsHubValidationError
from opshub.core.ids import new_ulid
from opshub.domain.events import (
    AllEvent,
    BriefingRequested,
    Candidate,
    DecisionCandidatePayload,
    EmbeddingFailed,
    ItemEnqueued,
    ProposalApplied,
    ProposalFailed,
    ProposalGenerated,
    ProposalRejected,
    ProposalRequested,
    ReplyDraftCandidatePayload,
    SourceObserved,
    TaskCandidatePayload,
    TaskCreated,
    TextEmbedded,
)

# Module-level singletons so each test pays the schema-build cost once.
_AllEventAdapter: TypeAdapter[AllEvent] = TypeAdapter(AllEvent)  # pyright: ignore[reportCallIssue]
_CandidateAdapter: TypeAdapter[Candidate] = TypeAdapter(Candidate)  # pyright: ignore[reportCallIssue]


def _agg() -> str:
    return new_ulid()


# ---- TaskCandidatePayload / DecisionCandidatePayload ----------------------


def test_task_candidate_minimal_fields() -> None:
    payload = TaskCandidatePayload(title="ship phase 6")
    assert payload.kind == "task"
    assert payload.schema_version == "v1"
    assert payload.title == "ship phase 6"
    assert payload.body is None


def test_task_candidate_full_fields() -> None:
    payload = TaskCandidatePayload(
        title="ship phase 6",
        body="action loop end-to-end",
    )
    assert payload.body == "action loop end-to-end"


def test_decision_candidate_minimal_fields() -> None:
    payload = DecisionCandidatePayload(text="prefer Ollama for local dev")
    assert payload.kind == "decision"
    assert payload.schema_version == "v1"
    assert payload.text == "prefer Ollama for local dev"
    assert payload.context is None


def test_decision_candidate_full_fields() -> None:
    payload = DecisionCandidatePayload(
        text="prefer Ollama for local dev",
        context="Anthropic / OpenAI remain available for cloud",
    )
    assert payload.context == "Anthropic / OpenAI remain available for cloud"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", ""),
        ("title", "x" * 201),
        ("body", "x" * 2001),
    ],
)
def test_task_candidate_rejects_out_of_range_strings(field: str, value: str) -> None:
    data: dict[str, Any] = {"title": "t", "body": "b"}
    data[field] = value
    with pytest.raises(PydanticValidationError):
        TaskCandidatePayload(**data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("text", ""),
        ("text", "x" * 2001),
        ("context", "x" * 2001),
    ],
)
def test_decision_candidate_rejects_out_of_range_strings(field: str, value: str) -> None:
    data: dict[str, Any] = {"text": "t", "context": "c"}
    data[field] = value
    with pytest.raises(PydanticValidationError):
        DecisionCandidatePayload(**data)


def test_candidate_payloads_are_frozen() -> None:
    task = TaskCandidatePayload(title="t")
    with pytest.raises(PydanticValidationError):
        task.title = "other"
    decision = DecisionCandidatePayload(text="d")
    with pytest.raises(PydanticValidationError):
        decision.text = "other"


def test_candidate_payloads_forbid_extra_fields() -> None:
    with pytest.raises(PydanticValidationError):
        TaskCandidatePayload.model_validate({"title": "t", "unexpected": "boom"})
    with pytest.raises(PydanticValidationError):
        DecisionCandidatePayload.model_validate({"text": "t", "unexpected": "boom"})


def test_candidate_schema_version_rejects_v2() -> None:
    """``schema_version`` is pinned to ``"v1"`` on Phase 6 MVP.

    Phase 6.x will widen the literal to ``Literal["v1", "v2"]`` per
    ADR-0016 §決定 (f); until then ``"v2"`` payloads must fail
    validation so a future widening is detectable in CI.
    """
    with pytest.raises(PydanticValidationError):
        TaskCandidatePayload.model_validate({"kind": "task", "schema_version": "v2", "title": "t"})
    with pytest.raises(PydanticValidationError):
        DecisionCandidatePayload.model_validate(
            {"kind": "decision", "schema_version": "v2", "text": "t"}
        )


def test_candidate_discriminator_dispatches_to_task_payload() -> None:
    payload = _CandidateAdapter.validate_python({"kind": "task", "title": "t"})
    assert isinstance(payload, TaskCandidatePayload)


def test_candidate_discriminator_dispatches_to_decision_payload() -> None:
    payload = _CandidateAdapter.validate_python({"kind": "decision", "text": "d"})
    assert isinstance(payload, DecisionCandidatePayload)


def test_candidate_discriminator_rejects_unknown_kind() -> None:
    """ADR-0016 §決定 (e): ``"inbox_item"`` / ``"source"`` are Phase 6.x."""
    with pytest.raises(PydanticValidationError):
        _CandidateAdapter.validate_python({"kind": "inbox_item", "summary": "x"})
    with pytest.raises(PydanticValidationError):
        _CandidateAdapter.validate_python({"kind": "source", "title": "x"})


# ---- ReplyDraftCandidatePayload (Phase 10 step E2, ADR-0016 §決定 (i)) ----


_REPLY_SRC_ID = new_ulid()


def test_reply_draft_candidate_minimal_fields() -> None:
    payload = ReplyDraftCandidatePayload(
        reply_to_source_id=_REPLY_SRC_ID,
        reply_to_source_type="slack_message",
        body="OK, I'll take a look.",
    )
    assert payload.kind == "reply_draft"
    assert payload.schema_version == "v2"
    assert payload.reply_to_source_id == _REPLY_SRC_ID
    assert payload.reply_to_source_type == "slack_message"
    assert payload.body == "OK, I'll take a look."
    assert payload.subject is None


def test_reply_draft_candidate_with_subject() -> None:
    payload = ReplyDraftCandidatePayload(
        reply_to_source_id=_REPLY_SRC_ID,
        reply_to_source_type="ms365_outlook",
        body="Hi Alice, thanks for the follow-up.",
        subject="Re: Q3 planning",
    )
    assert payload.subject == "Re: Q3 planning"


def test_reply_draft_candidate_is_frozen() -> None:
    payload = ReplyDraftCandidatePayload(
        reply_to_source_id=_REPLY_SRC_ID,
        reply_to_source_type="slack_message",
        body="OK",
    )
    with pytest.raises(PydanticValidationError):
        payload.body = "modified"


def test_reply_draft_candidate_forbids_extra_fields() -> None:
    with pytest.raises(PydanticValidationError):
        ReplyDraftCandidatePayload.model_validate(
            {
                "kind": "reply_draft",
                "reply_to_source_id": _REPLY_SRC_ID,
                "reply_to_source_type": "slack_message",
                "body": "OK",
                "unexpected": "boom",
            }
        )


def test_reply_draft_candidate_rejects_schema_v1() -> None:
    """ADR-0016 §決定 (i): reply_draft is pinned at v2.

    Phase 6 v1 candidates (task / decision) are NOT migrated; the
    reader branches on schema_version per §決定 (f). A reply_draft
    payload tagged as v1 must fail validation so a future schema bump
    is detectable in CI.
    """
    with pytest.raises(PydanticValidationError):
        ReplyDraftCandidatePayload.model_validate(
            {
                "kind": "reply_draft",
                "schema_version": "v1",
                "reply_to_source_id": _REPLY_SRC_ID,
                "reply_to_source_type": "slack_message",
                "body": "OK",
            }
        )


def test_reply_draft_candidate_rejects_short_source_id() -> None:
    """ULID is exactly 26 chars; shorter values must fail."""
    with pytest.raises(PydanticValidationError):
        ReplyDraftCandidatePayload(
            reply_to_source_id="01J",  # too short
            reply_to_source_type="slack_message",
            body="OK",
        )


def test_reply_draft_candidate_rejects_empty_body() -> None:
    with pytest.raises(PydanticValidationError):
        ReplyDraftCandidatePayload(
            reply_to_source_id=_REPLY_SRC_ID,
            reply_to_source_type="slack_message",
            body="",
        )


def test_reply_draft_candidate_rejects_oversized_body() -> None:
    with pytest.raises(PydanticValidationError):
        ReplyDraftCandidatePayload(
            reply_to_source_id=_REPLY_SRC_ID,
            reply_to_source_type="slack_message",
            body="x" * 8001,  # 1 over cap
        )


def test_candidate_discriminator_dispatches_to_reply_draft_payload() -> None:
    payload = _CandidateAdapter.validate_python(
        {
            "kind": "reply_draft",
            "reply_to_source_id": _REPLY_SRC_ID,
            "reply_to_source_type": "slack_message",
            "body": "OK",
        }
    )
    assert isinstance(payload, ReplyDraftCandidatePayload)


# ---- ProposalRequested -----------------------------------------------------


def test_proposal_requested_minimal_fields() -> None:
    proposal_id = _agg()
    event = ProposalRequested(
        aggregate_id=proposal_id,
        actor="cli:propose",
        topic="phase 6 next steps",
        scope="all",
        requested_by="cli:propose",
    )
    assert event.event_type == "proposal.requested"
    assert event.schema_version == 1
    assert event.topic == "phase 6 next steps"
    assert event.scope == "all"
    assert event.requested_by == "cli:propose"
    assert event.briefing_id is None


def test_proposal_requested_with_briefing_id() -> None:
    proposal_id = _agg()
    briefing_id = _agg()
    event = ProposalRequested(
        aggregate_id=proposal_id,
        actor="cli:propose",
        topic="phase 6 next steps",
        scope="all",
        briefing_id=briefing_id,
        requested_by="cli:propose",
    )
    assert event.briefing_id == briefing_id


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("topic", ""),
        ("topic", "x" * 501),
        ("scope", ""),
        ("scope", "x" * 201),
        ("requested_by", ""),
        ("requested_by", "x" * 201),
        ("briefing_id", "x" * 25),
        ("briefing_id", "x" * 27),
    ],
)
def test_proposal_requested_rejects_out_of_range_strings(field: str, value: str) -> None:
    proposal_id = _agg()
    payload: dict[str, Any] = {
        "aggregate_id": proposal_id,
        "actor": "cli:propose",
        "topic": "topic",
        "scope": "all",
        "briefing_id": _agg(),
        "requested_by": "cli:propose",
    }
    payload[field] = value
    with pytest.raises(PydanticValidationError):
        ProposalRequested(**payload)


def test_proposal_requested_rejects_wrong_event_type_literal() -> None:
    proposal_id = _agg()
    with pytest.raises(PydanticValidationError):
        ProposalRequested.model_validate(
            {
                "event_type": "proposal.invented",
                "aggregate_id": proposal_id,
                "actor": "cli:propose",
                "topic": "t",
                "scope": "all",
                "requested_by": "cli:propose",
            }
        )


# ---- ProposalGenerated -----------------------------------------------------


def _task_cand(title: str = "ship") -> TaskCandidatePayload:
    return TaskCandidatePayload(title=title)


def _decision_cand(text: str = "go") -> DecisionCandidatePayload:
    return DecisionCandidatePayload(text=text)


def test_proposal_generated_minimal_fields() -> None:
    proposal_id = _agg()
    candidates: list[Candidate] = [
        _task_cand("ship"),
        _decision_cand("go"),
    ]
    event = ProposalGenerated(
        aggregate_id=proposal_id,
        actor="service:proposal",
        topic="phase 6",
        scope="all",
        candidates=candidates,
        model_id="claude-haiku-4-5-20251001",
        model_version="20251001",
        tokens_in=100,
        tokens_out=50,
    )
    assert event.event_type == "proposal.generated"
    assert event.schema_version == 1
    assert len(event.candidates) == 2
    assert isinstance(event.candidates[0], TaskCandidatePayload)
    assert isinstance(event.candidates[1], DecisionCandidatePayload)


def test_proposal_generated_rejects_empty_candidates() -> None:
    proposal_id = _agg()
    with pytest.raises(PydanticValidationError):
        ProposalGenerated(
            aggregate_id=proposal_id,
            actor="service:proposal",
            topic="t",
            scope="all",
            candidates=[],
            model_id="m",
            model_version="v",
            tokens_in=0,
            tokens_out=0,
        )


def test_proposal_generated_rejects_too_many_candidates() -> None:
    proposal_id = _agg()
    candidates: list[Candidate] = [_task_cand(f"t{i}") for i in range(21)]
    with pytest.raises(PydanticValidationError):
        ProposalGenerated(
            aggregate_id=proposal_id,
            actor="service:proposal",
            topic="t",
            scope="all",
            candidates=candidates,
            model_id="m",
            model_version="v",
            tokens_in=0,
            tokens_out=0,
        )


def test_proposal_generated_accepts_max_candidates() -> None:
    """20 candidates is the inclusive upper bound."""
    proposal_id = _agg()
    candidates: list[Candidate] = [_task_cand(f"t{i}") for i in range(20)]
    event = ProposalGenerated(
        aggregate_id=proposal_id,
        actor="service:proposal",
        topic="t",
        scope="all",
        candidates=candidates,
        model_id="m",
        model_version="v",
        tokens_in=0,
        tokens_out=0,
    )
    assert len(event.candidates) == 20


def test_proposal_generated_defaults_context_source_refs_to_empty() -> None:
    """Phase 10 step E2: ``context_source_refs`` is optional and defaults empty.

    Backward compatibility per ADR-0002 §4 — historic Phase 6 events
    must deserialise unchanged. The field default is an empty list so
    ``LinksProjector.apply`` finds nothing to derive when consuming a
    Phase 6 ``ProposalGenerated`` event.
    """
    proposal_id = _agg()
    event = ProposalGenerated(
        aggregate_id=proposal_id,
        actor="service:proposal",
        topic="t",
        scope="all",
        candidates=[_task_cand()],
        model_id="m",
        model_version="v",
        tokens_in=0,
        tokens_out=0,
    )
    assert event.context_source_refs == []


def test_proposal_generated_accepts_context_source_refs() -> None:
    """Phase 10 step E2 (ADR-0017 §決定 (b) Phase 10 改訂)."""
    proposal_id = _agg()
    ref_a = ("source", _agg())
    ref_b = ("task", _agg())
    event = ProposalGenerated(
        aggregate_id=proposal_id,
        actor="service:proposal",
        topic="t",
        scope="all",
        candidates=[_task_cand()],
        model_id="m",
        model_version="v",
        tokens_in=0,
        tokens_out=0,
        context_source_refs=[ref_a, ref_b],
    )
    assert event.context_source_refs == [ref_a, ref_b]


def test_proposal_generated_rejects_negative_tokens() -> None:
    proposal_id = _agg()
    with pytest.raises(PydanticValidationError):
        ProposalGenerated(
            aggregate_id=proposal_id,
            actor="service:proposal",
            topic="t",
            scope="all",
            candidates=[_task_cand()],
            model_id="m",
            model_version="v",
            tokens_in=-1,
            tokens_out=0,
        )
    with pytest.raises(PydanticValidationError):
        ProposalGenerated(
            aggregate_id=proposal_id,
            actor="service:proposal",
            topic="t",
            scope="all",
            candidates=[_task_cand()],
            model_id="m",
            model_version="v",
            tokens_in=0,
            tokens_out=-1,
        )


def test_proposal_generated_candidates_roundtrip_via_json() -> None:
    """``candidates`` survives ``model_dump(mode="json")`` → re-validate."""
    proposal_id = _agg()
    event = ProposalGenerated(
        aggregate_id=proposal_id,
        actor="service:proposal",
        topic="t",
        scope="all",
        candidates=[
            _task_cand("ship phase 6"),
            _decision_cand("prefer ollama"),
        ],
        model_id="m",
        model_version="v",
        tokens_in=10,
        tokens_out=20,
    )
    dumped = event.model_dump(mode="json")
    assert dumped["candidates"][0]["kind"] == "task"
    assert dumped["candidates"][1]["kind"] == "decision"
    assert dumped["candidates"][0]["schema_version"] == "v1"
    restored = ProposalGenerated.model_validate(dumped)
    assert restored == event
    assert isinstance(restored.candidates[0], TaskCandidatePayload)
    assert isinstance(restored.candidates[1], DecisionCandidatePayload)


def test_candidate_discriminator_round_trips_mixed_v1_and_v2_union() -> None:
    """ADR-0016 §決定 (f): v1 + v2 candidates coexist in one union list.

    Reader branches on ``(kind, schema_version)`` per §決定 (f). A
    mixed list with v1 ``task`` + v1 ``decision`` + v2 ``reply_draft``
    must serialise through ``model_dump(mode="json")`` and re-validate
    back to the **typed** discriminated-union members — no field is
    lost across the JSON round-trip, and the discriminator dispatch
    selects the correct payload subclass for each entry.

    Pins the Phase 10 cross-version reader contract: Phase 6 v1
    candidates are NOT rewritten when v2 ``reply_draft`` is added, so a
    single ``ProposalGenerated.candidates`` list can hold both
    versions side-by-side (event log immutability, ADR-0002).
    """
    src_id = new_ulid()
    proposal_id = _agg()
    event = ProposalGenerated(
        aggregate_id=proposal_id,
        actor="service:proposal",
        topic="mixed union round-trip",
        scope="all",
        candidates=[
            TaskCandidatePayload(title="v1 task"),
            DecisionCandidatePayload(text="v1 decision"),
            ReplyDraftCandidatePayload(
                reply_to_source_id=src_id,
                reply_to_source_type="slack_message",
                body="v2 reply draft",
            ),
        ],
        model_id="m",
        model_version="v",
        tokens_in=10,
        tokens_out=20,
    )

    dumped = event.model_dump(mode="json")
    # Sanity-check the wire shape carries kind + schema_version per
    # candidate so the reader can branch unambiguously.
    assert dumped["candidates"][0]["kind"] == "task"
    assert dumped["candidates"][0]["schema_version"] == "v1"
    assert dumped["candidates"][1]["kind"] == "decision"
    assert dumped["candidates"][1]["schema_version"] == "v1"
    assert dumped["candidates"][2]["kind"] == "reply_draft"
    assert dumped["candidates"][2]["schema_version"] == "v2"

    restored = ProposalGenerated.model_validate(dumped)
    assert restored == event
    assert isinstance(restored.candidates[0], TaskCandidatePayload)
    assert isinstance(restored.candidates[1], DecisionCandidatePayload)
    assert isinstance(restored.candidates[2], ReplyDraftCandidatePayload)
    assert restored.candidates[2].body == "v2 reply draft"
    assert restored.candidates[2].reply_to_source_id == src_id

    # Also pin the ``Candidate`` TypeAdapter (the shared discriminator
    # dispatcher used by ``ProposalCandidatesSchema`` and other
    # readers) round-trips each element individually.
    for original, payload in zip(event.candidates, dumped["candidates"], strict=True):
        round_tripped = _CandidateAdapter.validate_python(payload)
        assert round_tripped == original
        assert type(round_tripped) is type(original)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("topic", ""),
        ("topic", "x" * 501),
        ("scope", ""),
        ("scope", "x" * 201),
        ("model_id", ""),
        ("model_id", "x" * 201),
        ("model_version", ""),
        ("model_version", "x" * 101),
    ],
)
def test_proposal_generated_rejects_out_of_range_strings(field: str, value: str) -> None:
    proposal_id = _agg()
    payload: dict[str, Any] = {
        "aggregate_id": proposal_id,
        "actor": "service:proposal",
        "topic": "t",
        "scope": "all",
        "candidates": [_task_cand()],
        "model_id": "m",
        "model_version": "v",
        "tokens_in": 0,
        "tokens_out": 0,
    }
    payload[field] = value
    with pytest.raises(PydanticValidationError):
        ProposalGenerated(**payload)


# ---- ProposalApplied -------------------------------------------------------


def test_proposal_applied_minimal_fields() -> None:
    proposal_id = _agg()
    entity_id = _agg()
    event = ProposalApplied(
        aggregate_id=proposal_id,
        actor="cli:propose",
        candidate_index=0,
        applied_entity_type="task",
        applied_entity_id=entity_id,
        applied_by="cli:propose",
    )
    assert event.event_type == "proposal.applied"
    assert event.schema_version == 1
    assert event.candidate_index == 0
    assert event.applied_entity_type == "task"
    assert event.applied_entity_id == entity_id
    assert event.applied_by == "cli:propose"


def test_proposal_applied_accepts_decision_type() -> None:
    proposal_id = _agg()
    entity_id = _agg()
    event = ProposalApplied(
        aggregate_id=proposal_id,
        actor="cli:propose",
        candidate_index=3,
        applied_entity_type="decision",
        applied_entity_id=entity_id,
        applied_by="cli:propose",
    )
    assert event.applied_entity_type == "decision"
    assert event.candidate_index == 3


def test_proposal_applied_rejects_negative_index() -> None:
    proposal_id = _agg()
    with pytest.raises(PydanticValidationError):
        ProposalApplied(
            aggregate_id=proposal_id,
            actor="cli:propose",
            candidate_index=-1,
            applied_entity_type="task",
            applied_entity_id=_agg(),
            applied_by="cli:propose",
        )


def test_proposal_applied_rejects_unknown_entity_type() -> None:
    """ADR-0016 §決定 (e) restricts to ``"task"`` / ``"decision"`` / ``"reply_draft"``."""
    proposal_id = _agg()
    with pytest.raises(PydanticValidationError):
        ProposalApplied.model_validate(
            {
                "aggregate_id": proposal_id,
                "actor": "cli:propose",
                "candidate_index": 0,
                "applied_entity_type": "inbox_item",
                "applied_entity_id": _agg(),
                "applied_by": "cli:propose",
            }
        )


def test_proposal_applied_accepts_reply_draft_type() -> None:
    """Phase 10 step E2 (ADR-0016 §決定 (i)) widens the applied entity type union."""
    proposal_id = _agg()
    entity_id = _agg()
    event = ProposalApplied(
        aggregate_id=proposal_id,
        actor="cli:propose",
        candidate_index=2,
        applied_entity_type="reply_draft",
        applied_entity_id=entity_id,
        applied_by="cli:propose",
    )
    assert event.applied_entity_type == "reply_draft"
    assert event.applied_entity_id == entity_id


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("applied_entity_id", "x" * 25),
        ("applied_entity_id", "x" * 27),
        ("applied_entity_id", ""),
        ("applied_by", ""),
        ("applied_by", "x" * 201),
    ],
)
def test_proposal_applied_rejects_out_of_range_strings(field: str, value: str) -> None:
    proposal_id = _agg()
    payload: dict[str, Any] = {
        "aggregate_id": proposal_id,
        "actor": "cli:propose",
        "candidate_index": 0,
        "applied_entity_type": "task",
        "applied_entity_id": _agg(),
        "applied_by": "cli:propose",
    }
    payload[field] = value
    with pytest.raises(PydanticValidationError):
        ProposalApplied(**payload)


# ---- ProposalRejected ------------------------------------------------------


def test_proposal_rejected_minimal_fields() -> None:
    proposal_id = _agg()
    event = ProposalRejected(
        aggregate_id=proposal_id,
        actor="cli:propose",
        candidate_index=2,
        rejected_by="cli:propose",
    )
    assert event.event_type == "proposal.rejected"
    assert event.schema_version == 1
    assert event.candidate_index == 2
    assert event.reason is None


def test_proposal_rejected_with_reason() -> None:
    proposal_id = _agg()
    event = ProposalRejected(
        aggregate_id=proposal_id,
        actor="cli:propose",
        candidate_index=2,
        rejected_by="cli:propose",
        reason="duplicate of existing task",
    )
    assert event.reason == "duplicate of existing task"


def test_proposal_rejected_rejects_negative_index() -> None:
    proposal_id = _agg()
    with pytest.raises(PydanticValidationError):
        ProposalRejected(
            aggregate_id=proposal_id,
            actor="cli:propose",
            candidate_index=-1,
            rejected_by="cli:propose",
        )


def test_proposal_rejected_rejects_overlong_reason() -> None:
    proposal_id = _agg()
    with pytest.raises(PydanticValidationError):
        ProposalRejected(
            aggregate_id=proposal_id,
            actor="cli:propose",
            candidate_index=0,
            rejected_by="cli:propose",
            reason="x" * 1001,
        )


# ---- ProposalFailed --------------------------------------------------------


def test_proposal_failed_minimal_fields() -> None:
    proposal_id = _agg()
    event = ProposalFailed(
        aggregate_id=proposal_id,
        actor="service:proposal",
        topic="t",
        scope="all",
        model_id="m",
        error_message="upstream HTTP 500",
    )
    assert event.event_type == "proposal.failed"
    assert event.schema_version == 1
    assert event.error_message == "upstream HTTP 500"


def test_proposal_failed_does_not_auto_sanitise() -> None:
    """The event constructor is a pure value object (Phase 5 B1 contract).

    Sanitisation is the caller's responsibility — the event records
    whatever string is passed in (subject only to length / non-empty
    bounds). The integration with
    :func:`opshub.core.sanitise.sanitise_error_message` is tested at
    the service layer (Phase 6 B3).
    """
    proposal_id = _agg()
    raw = "boom with sk-anthropic-FAKE-KEY-1234567890 in it"
    event = ProposalFailed(
        aggregate_id=proposal_id,
        actor="service:proposal",
        topic="t",
        scope="all",
        model_id="m",
        error_message=raw,
    )
    # Verbatim: the event does NOT redact.
    assert event.error_message == raw


def test_proposal_failed_accepts_max_length_error_message() -> None:
    proposal_id = _agg()
    event = ProposalFailed(
        aggregate_id=proposal_id,
        actor="service:proposal",
        topic="t",
        scope="all",
        model_id="m",
        error_message="x" * 2000,
    )
    assert len(event.error_message) == 2000


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("topic", ""),
        ("topic", "x" * 501),
        ("scope", ""),
        ("scope", "x" * 201),
        ("model_id", ""),
        ("model_id", "x" * 201),
        ("error_message", ""),
        ("error_message", "x" * 2001),
    ],
)
def test_proposal_failed_rejects_out_of_range_strings(field: str, value: str) -> None:
    proposal_id = _agg()
    payload: dict[str, Any] = {
        "aggregate_id": proposal_id,
        "actor": "service:proposal",
        "topic": "t",
        "scope": "all",
        "model_id": "m",
        "error_message": "boom",
    }
    payload[field] = value
    with pytest.raises(PydanticValidationError):
        ProposalFailed(**payload)


# ---- frozen / extra=forbid / Literal-locked event_type --------------------


def test_phase6_event_is_frozen() -> None:
    proposal_id = _agg()
    event = ProposalRequested(
        aggregate_id=proposal_id,
        actor="cli:propose",
        topic="t",
        scope="all",
        requested_by="cli:propose",
    )
    with pytest.raises(PydanticValidationError):
        event.topic = "other"


def test_phase6_event_forbids_extra_fields() -> None:
    proposal_id = _agg()
    with pytest.raises(PydanticValidationError):
        ProposalRequested.model_validate(
            {
                "aggregate_id": proposal_id,
                "actor": "cli:propose",
                "topic": "t",
                "scope": "all",
                "requested_by": "cli:propose",
                "unexpected": "boom",
            }
        )


# ---- tz-aware datetime invariants -----------------------------------------


def test_proposal_event_default_datetimes_are_tz_aware_utc() -> None:
    """``occurred_at`` / ``recorded_at`` default to tz-aware UTC."""
    proposal_id = _agg()
    event = ProposalRequested(
        aggregate_id=proposal_id,
        actor="cli:propose",
        topic="t",
        scope="all",
        requested_by="cli:propose",
    )
    assert event.occurred_at.tzinfo is not None
    assert event.recorded_at.tzinfo is not None
    assert event.occurred_at.utcoffset() == timedelta(0)
    assert event.recorded_at.utcoffset() == timedelta(0)


def test_proposal_event_rejects_naive_datetime() -> None:
    """``AfterValidator(to_utc)`` raises on naive datetime input."""
    proposal_id = _agg()
    naive = datetime(2026, 5, 17, 12, 0, 0)  # intentional naive
    with pytest.raises((PydanticValidationError, OpsHubValidationError)):
        ProposalRequested(
            aggregate_id=proposal_id,
            actor="cli:propose",
            topic="t",
            scope="all",
            requested_by="cli:propose",
            occurred_at=naive,
        )


def test_proposal_event_normalises_non_utc_tz() -> None:
    """Non-UTC tz-aware values are converted to UTC, not rejected."""
    proposal_id = _agg()
    plus_nine = timezone(timedelta(hours=9))
    local = datetime(2026, 5, 17, 9, 0, 0, tzinfo=plus_nine)
    event = ProposalRequested(
        aggregate_id=proposal_id,
        actor="cli:propose",
        topic="t",
        scope="all",
        requested_by="cli:propose",
        occurred_at=local,
    )
    assert event.occurred_at == datetime(2026, 5, 17, 0, 0, 0, tzinfo=UTC)
    assert event.occurred_at.utcoffset() == timedelta(0)


# ---- AllEvent dispatch for Phase 6 event types ----------------------------


def _factory_proposal_requested() -> ProposalRequested:
    proposal_id = _agg()
    return ProposalRequested(
        aggregate_id=proposal_id,
        actor="cli:propose",
        topic="t",
        scope="all",
        requested_by="cli:propose",
    )


def _factory_proposal_generated() -> ProposalGenerated:
    proposal_id = _agg()
    return ProposalGenerated(
        aggregate_id=proposal_id,
        actor="service:proposal",
        topic="t",
        scope="all",
        candidates=[_task_cand("ship"), _decision_cand("go")],
        model_id="m",
        model_version="v",
        tokens_in=10,
        tokens_out=20,
    )


def _factory_proposal_applied() -> ProposalApplied:
    proposal_id = _agg()
    return ProposalApplied(
        aggregate_id=proposal_id,
        actor="cli:propose",
        candidate_index=0,
        applied_entity_type="task",
        applied_entity_id=_agg(),
        applied_by="cli:propose",
    )


def _factory_proposal_rejected() -> ProposalRejected:
    proposal_id = _agg()
    return ProposalRejected(
        aggregate_id=proposal_id,
        actor="cli:propose",
        candidate_index=1,
        rejected_by="cli:propose",
        reason="duplicate",
    )


def _factory_proposal_failed() -> ProposalFailed:
    proposal_id = _agg()
    return ProposalFailed(
        aggregate_id=proposal_id,
        actor="service:proposal",
        topic="t",
        scope="all",
        model_id="m",
        error_message="boom",
    )


_PHASE6_FACTORIES: list[tuple[str, Any]] = [
    ("proposal.requested", _factory_proposal_requested),
    ("proposal.generated", _factory_proposal_generated),
    ("proposal.applied", _factory_proposal_applied),
    ("proposal.rejected", _factory_proposal_rejected),
    ("proposal.failed", _factory_proposal_failed),
]


@pytest.mark.parametrize(
    ("event_type", "factory"),
    _PHASE6_FACTORIES,
    ids=[event_type for event_type, _ in _PHASE6_FACTORIES],
)
def test_phase6_event_roundtrip_via_model_dump(event_type: str, factory: Any) -> None:
    event = factory()
    assert event.event_type == event_type
    restored = _AllEventAdapter.validate_python(event.model_dump(mode="json"))
    assert restored == event
    assert type(restored) is type(event)


def test_phase6_event_rejects_unknown_event_type() -> None:
    payload = {
        "event_type": "proposal.invented",
        "aggregate_id": _agg(),
        "actor": "service:proposal",
    }
    with pytest.raises(PydanticValidationError):
        _AllEventAdapter.validate_python(payload)


# ---- AllEvent extension ---------------------------------------------------


def test_all_event_dispatches_to_proposal_requested() -> None:
    """``AllEvent`` must decode the new Phase 6 request event."""
    proposal_id = _agg()
    payload = {
        "event_type": "proposal.requested",
        "aggregate_id": proposal_id,
        "actor": "cli:propose",
        "topic": "t",
        "scope": "all",
        "requested_by": "cli:propose",
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, ProposalRequested)


def test_all_event_dispatches_to_proposal_generated() -> None:
    proposal_id = _agg()
    payload = {
        "event_type": "proposal.generated",
        "aggregate_id": proposal_id,
        "actor": "service:proposal",
        "topic": "t",
        "scope": "all",
        "candidates": [
            {"kind": "task", "schema_version": "v1", "title": "ship"},
            {"kind": "decision", "schema_version": "v1", "text": "go"},
        ],
        "model_id": "m",
        "model_version": "v",
        "tokens_in": 10,
        "tokens_out": 20,
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, ProposalGenerated)
    assert isinstance(event.candidates[0], TaskCandidatePayload)
    assert isinstance(event.candidates[1], DecisionCandidatePayload)


def test_all_event_dispatches_to_proposal_applied() -> None:
    proposal_id = _agg()
    payload = {
        "event_type": "proposal.applied",
        "aggregate_id": proposal_id,
        "actor": "cli:propose",
        "candidate_index": 0,
        "applied_entity_type": "task",
        "applied_entity_id": _agg(),
        "applied_by": "cli:propose",
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, ProposalApplied)


def test_all_event_dispatches_to_proposal_rejected() -> None:
    proposal_id = _agg()
    payload = {
        "event_type": "proposal.rejected",
        "aggregate_id": proposal_id,
        "actor": "cli:propose",
        "candidate_index": 1,
        "rejected_by": "cli:propose",
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, ProposalRejected)


def test_all_event_dispatches_to_proposal_failed() -> None:
    proposal_id = _agg()
    payload = {
        "event_type": "proposal.failed",
        "aggregate_id": proposal_id,
        "actor": "service:proposal",
        "topic": "t",
        "scope": "all",
        "model_id": "m",
        "error_message": "boom",
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, ProposalFailed)


# ---- Backwards-compat: AllEvent still decodes prior phases ----------------


def test_all_event_still_dispatches_to_task_event() -> None:
    payload = {
        "event_type": "task.created",
        "aggregate_id": _agg(),
        "actor": "cli:create",
        "title": "still works",
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, TaskCreated)


def test_all_event_still_dispatches_to_phase2_event() -> None:
    payload = {
        "event_type": "inbox.enqueued",
        "aggregate_id": _agg(),
        "actor": "cli:inbox",
        "summary": "from all-event",
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, ItemEnqueued)


def test_all_event_still_dispatches_to_phase3_event() -> None:
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


def test_all_event_still_dispatches_to_phase4_text_embedded() -> None:
    entity_id = _agg()
    payload = {
        "event_type": "embedding.text_embedded",
        "aggregate_id": entity_id,
        "actor": "service:embedding",
        "entity_type": "task",
        "entity_id": entity_id,
        "model_id": "BAAI/bge-m3",
        "model_version": "1.0.0",
        "dim": 1024,
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, TextEmbedded)


def test_all_event_still_dispatches_to_phase4_embedding_failed() -> None:
    entity_id = _agg()
    payload = {
        "event_type": "embedding.failed",
        "aggregate_id": entity_id,
        "actor": "service:embedding",
        "entity_type": "task",
        "entity_id": entity_id,
        "model_id": "BAAI/bge-m3",
        "error_message": "boom",
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, EmbeddingFailed)


def test_all_event_still_dispatches_to_phase5_briefing_requested() -> None:
    briefing_id = _agg()
    payload = {
        "event_type": "briefing.requested",
        "aggregate_id": briefing_id,
        "actor": "cli:brief",
        "briefing_id": briefing_id,
        "topic": "t",
        "scope": "all",
        "requested_by": "cli:brief",
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, BriefingRequested)
