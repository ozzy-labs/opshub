"""Dispatch-level tests for the LLM-backed widening handlers.

The pure read handlers (``graph.*``, ``source.*``) are exercised
directly in :mod:`tests.unit.mcp.test_widening_handlers` because they
talk to the engine. ``brief`` / ``propose.generate`` /
``embeddings.find_duplicates`` all delegate through
:func:`opshub.cli._wiring.build_*`, which in turn resolves an LLM /
embedder backend from settings — too much surface to set up here.

Instead this module monkey-patches the builders so the handler
round-trips against an in-memory stand-in. The point is to pin the
**handler shape**:

* the right method on the service gets called
* the response carries the expected envelope keys
* mode-switch logic (``format=md`` vs ``format=json``;
  ``reply_to_source_id`` vs ``topic``) routes correctly
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from opshub.mcp._tools import (
    build_brief_handler,
    build_embeddings_find_duplicates_handler,
)
from opshub.mcp._writes import build_propose_generate_handler

_NOW = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)


# Explicit-type factory helpers so monkeypatch.setattr can swap in our
# stubs without pyright/mypy complaining about an untyped lambda
# parameter. The real builders accept ``actor: str`` (with a default);
# keeping the stub factory signature matching means a static checker
# can verify the substitution shape.
def _briefing_factory(service: _StubBriefingService) -> Callable[..., _StubBriefingService]:
    def _factory(actor: str = "stub") -> _StubBriefingService:
        _ = actor
        return service

    return _factory


def _proposal_factory(service: _StubProposalService) -> Callable[..., _StubProposalService]:
    def _factory(actor: str = "stub") -> _StubProposalService:
        _ = actor
        return service

    return _factory


def _duplicate_factory(
    service: _StubDuplicateService,
) -> Callable[..., _StubDuplicateService]:
    def _factory() -> _StubDuplicateService:
        return service

    return _factory


# ---------------------------------------------------------------- brief


@dataclass(frozen=True, slots=True)
class _StubBriefing:
    briefing_id: str = "01HBRIEF00000000000000000"
    topic: str = "today"
    scope: str = "all"
    markdown: str = "# brief body"
    source_refs: list[tuple[str, str]] | None = None
    model_id: str = "stub-model"
    model_version: str = "v0"
    tokens_in: int = 1
    tokens_out: int = 2
    generated_at: datetime = _NOW


class _StubBriefingService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        topic: str,
        *,
        max_sources: int = 20,
        max_tokens: int = 1500,
        expand_graph: bool = False,
    ) -> _StubBriefing:
        self.calls.append(
            {
                "topic": topic,
                "max_sources": max_sources,
                "max_tokens": max_tokens,
                "expand_graph": expand_graph,
            }
        )
        return _StubBriefing(source_refs=[("task", "01HTASK")])


async def test_brief_handler_md_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _StubBriefingService()
    monkeypatch.setattr(
        "opshub.cli._wiring.build_briefing_service",
        _briefing_factory(service),
    )
    handler = build_brief_handler(engine=cast("Any", None))
    payload = json.loads(await handler({"topic": "today", "format": "md"}))
    assert payload["format"] == "md"
    assert payload["briefing_id"] == "01HBRIEF00000000000000000"
    assert payload["markdown"] == "# brief body"
    assert payload["source_count"] == 1
    # Ensure service was called with the right args.
    assert service.calls[0]["topic"] == "today"


async def test_brief_handler_json_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _StubBriefingService()
    monkeypatch.setattr(
        "opshub.cli._wiring.build_briefing_service",
        _briefing_factory(service),
    )
    handler = build_brief_handler(engine=cast("Any", None))
    payload = json.loads(
        await handler(
            {
                "topic": "weekly",
                "format": "json",
                "expand_graph": True,
                "max_sources": 5,
                "max_tokens": 200,
            }
        )
    )
    assert payload["format"] == "json"
    assert payload["model_id"] == "stub-model"
    assert payload["tokens_in"] == 1
    # source_refs renders as list of {entity_type, entity_id} dicts
    refs = payload["source_refs"]
    assert refs == [{"entity_type": "task", "entity_id": "01HTASK"}]
    # Service got the flags through.
    call = service.calls[0]
    assert call["expand_graph"] is True
    assert call["max_sources"] == 5
    assert call["max_tokens"] == 200


# ------------------------------------------------------ propose.generate


@dataclass(frozen=True, slots=True)
class _StubCandidate:
    """Tiny pydantic-like stub for proposal candidates.

    The real :class:`Candidate` is a discriminated union (Pydantic model)
    that exposes ``model_dump(mode="json")``. The stub mimics that
    surface so the handler does not need to import the real type.
    """

    kind: str = "task"
    title: str = "stub candidate"

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        return {"kind": self.kind, "title": self.title}


@dataclass(frozen=True, slots=True)
class _StubProposal:
    proposal_id: str = "01HPROP00000000000000000A"
    topic: str = "investigate"
    scope: str = "all"
    briefing_id: str | None = None
    candidates: list[Any] = None  # type: ignore[assignment]
    model_id: str = "stub-llm"
    model_version: str = "v0"
    tokens_in: int = 10
    tokens_out: int = 20
    generated_at: datetime = _NOW


def _make_stub_proposal(**overrides: Any) -> _StubProposal:
    defaults: dict[str, Any] = {
        "candidates": [_StubCandidate(kind="task", title="do x")],
    }
    defaults.update(overrides)
    return _StubProposal(**defaults)


class _StubProposalService:
    def __init__(self) -> None:
        self.generate_calls: list[dict[str, Any]] = []
        self.reply_calls: list[dict[str, Any]] = []

    def generate(
        self,
        topic: str,
        *,
        scope: str = "all",
        from_briefing_id: str | None = None,
        max_candidates: int = 5,
        max_tokens: int = 2000,
        expand_graph: bool = False,
    ) -> _StubProposal:
        self.generate_calls.append(
            {
                "topic": topic,
                "scope": scope,
                "from_briefing_id": from_briefing_id,
                "max_candidates": max_candidates,
                "max_tokens": max_tokens,
                "expand_graph": expand_graph,
            }
        )
        return _make_stub_proposal(topic=topic, scope=scope, briefing_id=from_briefing_id)

    def generate_reply_draft(
        self,
        reply_to_source_id: str,
        *,
        max_candidates: int = 3,
        max_tokens: int = 2000,
        expand_graph: bool = False,
    ) -> _StubProposal:
        self.reply_calls.append(
            {
                "reply_to_source_id": reply_to_source_id,
                "max_candidates": max_candidates,
                "max_tokens": max_tokens,
                "expand_graph": expand_graph,
            }
        )
        return _make_stub_proposal(
            topic="",
            candidates=[_StubCandidate(kind="reply_draft", title="reply preview")],
        )


async def test_propose_generate_topic_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _StubProposalService()
    monkeypatch.setattr(
        "opshub.cli._wiring.build_proposal_service",
        _proposal_factory(service),
    )
    handler = build_propose_generate_handler(engine=cast("Any", None))
    payload = json.loads(
        await handler(
            {
                "topic": "investigate",
                "from_briefing_id": "01HBRIEFSEED00000000000000",
                "max_candidates": 3,
                "max_tokens": 500,
            }
        )
    )
    assert payload["ok"] is True
    assert payload["proposal_id"] == "01HPROP00000000000000000A"
    assert payload["hitl_apply_required"] is True
    assert len(payload["candidates"]) == 1
    assert payload["candidates"][0]["kind"] == "task"
    assert payload["candidates"][0]["index"] == 0
    # The handler routed to the topic-mode service method.
    call = service.generate_calls[0]
    assert call["topic"] == "investigate"
    assert call["from_briefing_id"] == "01HBRIEFSEED00000000000000"
    assert call["max_candidates"] == 3
    assert service.reply_calls == []


async def test_propose_generate_reply_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _StubProposalService()
    monkeypatch.setattr(
        "opshub.cli._wiring.build_proposal_service",
        _proposal_factory(service),
    )
    handler = build_propose_generate_handler(engine=cast("Any", None))
    payload = json.loads(
        await handler(
            {
                "reply_to_source_id": "01HSRC00000000000000000R01",
                "max_candidates": 2,
            }
        )
    )
    assert payload["ok"] is True
    assert payload["hitl_apply_required"] is True
    assert payload["candidates"][0]["kind"] == "reply_draft"
    # Routed to the reply path; topic-mode service method was NOT called.
    call = service.reply_calls[0]
    assert call["reply_to_source_id"] == "01HSRC00000000000000000R01"
    assert service.generate_calls == []


async def test_propose_generate_rejects_combined_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Providing both topic and reply_to_source_id is a configuration error."""
    from opshub.core.errors import OpsHubError

    service = _StubProposalService()
    monkeypatch.setattr(
        "opshub.cli._wiring.build_proposal_service",
        _proposal_factory(service),
    )
    handler = build_propose_generate_handler(engine=cast("Any", None))
    with pytest.raises(OpsHubError) as excinfo:
        await handler(
            {
                "topic": "x",
                "reply_to_source_id": "01HSRC00000000000000000R02",
            }
        )
    assert "mutually exclusive" in str(excinfo.value)


async def test_propose_generate_rejects_empty_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both topic and reply_to_source_id absent → OpsHubError."""
    from opshub.core.errors import OpsHubError

    service = _StubProposalService()
    monkeypatch.setattr(
        "opshub.cli._wiring.build_proposal_service",
        _proposal_factory(service),
    )
    handler = build_propose_generate_handler(engine=cast("Any", None))
    with pytest.raises(OpsHubError) as excinfo:
        # Empty strings (schema defaults) should also count as absent.
        await handler({"topic": "", "reply_to_source_id": ""})
    msg = str(excinfo.value)
    assert "topic" in msg
    assert "reply_to_source_id" in msg


# --------------------------------- Phase 12 H4 ``mode`` dispatch tests


@pytest.mark.parametrize(
    "mode",
    ["inbox_triage", "source_extract", "meeting_followup"],
)
async def test_propose_generate_h4_mode_stamps_scope(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Phase 12 H4 (ADR-0016 §決定 (l)(b)): ``mode`` stamps proposal scope.

    Each H4 ``mode`` value must route through ``ProposalService.generate``
    with ``scope=<mode>`` so the persisted ``proposals.scope`` row
    records the originating skill. This is the audit / observability
    contract the ADR pins.
    """
    service = _StubProposalService()
    monkeypatch.setattr(
        "opshub.cli._wiring.build_proposal_service",
        _proposal_factory(service),
    )
    handler = build_propose_generate_handler(engine=cast("Any", None))
    payload = json.loads(
        await handler(
            {
                "topic": f"context for {mode}",
                "mode": mode,
                "max_candidates": 3,
            }
        )
    )
    assert payload["ok"] is True
    assert payload["scope"] == mode, (
        f"propose.generate must stamp scope={mode!r} (ADR-0016 §決定 (l)(b))"
    )
    # Routed to the topic path with the H4 scope label, not reply-draft.
    assert service.reply_calls == []
    assert len(service.generate_calls) == 1
    call = service.generate_calls[0]
    assert call["topic"] == f"context for {mode}"
    assert call["scope"] == mode


async def test_propose_generate_rejects_mode_with_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``mode`` is mutually exclusive with ``reply_to_source_id``.

    ADR-0016 §決定 (l)(b): the implicit reply-draft mode is signalled
    by ``reply_to_source_id``, not by ``mode=reply_draft``. Combining
    the two is a configuration error and must fail loud.
    """
    from opshub.core.errors import OpsHubError

    service = _StubProposalService()
    monkeypatch.setattr(
        "opshub.cli._wiring.build_proposal_service",
        _proposal_factory(service),
    )
    handler = build_propose_generate_handler(engine=cast("Any", None))
    with pytest.raises(OpsHubError) as excinfo:
        await handler(
            {
                "reply_to_source_id": "01HSRC00000000000000000R03",
                "mode": "inbox_triage",
            }
        )
    msg = str(excinfo.value)
    assert "mode" in msg
    assert "reply_to_source_id" in msg


async def test_propose_generate_default_scope_when_mode_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``mode``, ``ProposalService.generate`` keeps ``scope='all'``.

    Backward-compat pin: legacy Phase 6 callers that pass only
    ``topic`` must continue to land in the historical ``scope='all'``
    bucket so audit queries against existing proposals stay valid.
    """
    service = _StubProposalService()
    monkeypatch.setattr(
        "opshub.cli._wiring.build_proposal_service",
        _proposal_factory(service),
    )
    handler = build_propose_generate_handler(engine=cast("Any", None))
    await handler({"topic": "legacy call", "max_candidates": 1})
    call = service.generate_calls[0]
    assert call["scope"] == "all"


# --------------------------------------- embeddings.find_duplicates


@dataclass(frozen=True, slots=True)
class _StubDuplicatePair:
    entity_type: str = "source"
    entity_id_a: str = "01HSRC0000000000000000000A"
    entity_id_b: str = "01HSRC0000000000000000000B"
    text_a: str = "alpha"
    text_b: str = "alphaa"
    similarity: float = 0.97


class _StubDuplicateService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def find_duplicates(
        self,
        *,
        entity_type: str = "source",
        threshold: float = 0.92,
        limit: int = 100,
    ) -> list[_StubDuplicatePair]:
        self.calls.append(
            {
                "entity_type": entity_type,
                "threshold": threshold,
                "limit": limit,
            }
        )
        return [_StubDuplicatePair()]


async def test_embeddings_find_duplicates_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _StubDuplicateService()
    monkeypatch.setattr(
        "opshub.cli._wiring.build_duplicate_service",
        _duplicate_factory(service),
    )
    handler = build_embeddings_find_duplicates_handler(engine=cast("Any", None))
    payload = json.loads(await handler({"entity_type": "source", "threshold": 0.9, "limit": 5}))
    assert payload["entity_type"] == "source"
    items = payload["items"]
    assert len(items) == 1
    assert items[0]["similarity"] == 0.97
    # Underlying service got the args (defaults applied).
    call = service.calls[0]
    assert call["entity_type"] == "source"
    assert call["threshold"] == 0.9
    assert call["limit"] == 5
