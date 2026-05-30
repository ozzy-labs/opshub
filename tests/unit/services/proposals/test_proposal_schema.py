"""Tests for :class:`opshub.services.proposals.ProposalCandidatesSchema`.

The schema is the Pydantic SSOT for ``ProposalService.generate`` /
``generate_reply_draft`` structured output (ADR-0016 §決定 (b)). The
Phase 10 step E2 revision (§決定 (j)) added the ``triage`` field —
a 3-value (``respond`` / ``notify`` / ``ignore``) hint surfaced to the
operator but with **no auto-apply effect**: durable state still only
flips via operator-triggered ``opshub propose apply``.

This module pins the schema contract:

* Each ``triage`` literal (``respond`` / ``notify`` / ``ignore``)
  validates.
* Unrecognised triage strings fail Pydantic validation (so a
  drifted prompt that returns e.g. ``"maybe"`` is caught at the
  service boundary, not deeper in the apply path).
* ``triage`` defaults to ``None``, round-trips through JSON, and
  preserves Phase 6 backward compat (operators on Phase 6 callers see
  the parent shape unchanged).
* The presence of ``triage`` on the parent schema does NOT trigger
  auto-apply: ``ProposalService.apply`` continues to require explicit
  ``(proposal_id, candidate_index)`` from the operator regardless of
  what triage the parent carried — pinning ADR-0016 §決定 (c) HITL
  against a future "respond → auto-send" regression.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError as PydanticValidationError

from opshub.domain.events.proposal import (
    DecisionCandidatePayload,
    TaskCandidatePayload,
)
from opshub.services.proposals import ProposalCandidatesSchema

# ---- triage literal validation --------------------------------------------


@pytest.mark.parametrize("value", ["respond", "notify", "ignore"])
def test_proposal_candidates_schema_accepts_each_triage_literal(value: str) -> None:
    """All three EAIA-style triage values (ADR-0016 §決定 (j)) validate."""
    schema = ProposalCandidatesSchema(
        candidates=[TaskCandidatePayload(title="x")],
        triage=value,  # type: ignore[arg-type]
    )
    assert schema.triage == value


def test_proposal_candidates_schema_rejects_unknown_triage_value() -> None:
    """A drifted prompt returning e.g. ``"maybe"`` must fail validation.

    The triage field is a Pydantic ``Literal[...]`` so any string
    outside ``{respond, notify, ignore}`` raises at the schema layer —
    well before ``ProposalService.generate`` would mis-route the
    candidate list. Pins the §決定 (j) closed-set contract.
    """
    with pytest.raises(PydanticValidationError):
        ProposalCandidatesSchema.model_validate(
            {
                "candidates": [{"kind": "task", "title": "x"}],
                "triage": "maybe",
            }
        )


def test_proposal_candidates_schema_rejects_empty_triage_string() -> None:
    """Empty string is not a valid triage literal."""
    with pytest.raises(PydanticValidationError):
        ProposalCandidatesSchema.model_validate(
            {
                "candidates": [{"kind": "task", "title": "x"}],
                "triage": "",
            }
        )


# ---- triage default / backward compat -------------------------------------


def test_proposal_candidates_schema_triage_defaults_to_none() -> None:
    """ADR-0016 §決定 (j): ``triage`` is ``Optional`` for Phase 6 callers.

    A Phase 6 caller that has not been updated to emit triage MUST
    still produce a valid schema instance — ``triage`` defaults to
    ``None`` and the candidate list remains the only required content.
    """
    schema = ProposalCandidatesSchema(candidates=[TaskCandidatePayload(title="x")])
    assert schema.triage is None


def test_proposal_candidates_schema_triage_none_round_trips_via_json() -> None:
    """``triage=None`` survives ``model_dump(mode="json")`` → re-validate.

    Pins the Phase 6 backward-compat behaviour at the wire level: a
    Phase 6 caller's payload (no ``triage`` field) re-hydrates to the
    same shape after going through JSON, so the event store / projection
    do not need to special-case "old schema" payloads.
    """
    schema = ProposalCandidatesSchema(
        candidates=[
            TaskCandidatePayload(title="t"),
            DecisionCandidatePayload(text="d"),
        ]
    )
    assert schema.triage is None

    dumped = schema.model_dump(mode="json")
    assert dumped["triage"] is None
    restored = ProposalCandidatesSchema.model_validate(json.loads(json.dumps(dumped)))
    assert restored.triage is None
    assert len(restored.candidates) == 2


# ---- HITL boundary pin (ADR-0016 §決定 (c) + (j)) -------------------------


def test_proposal_service_apply_does_not_consult_triage_field() -> None:
    """ADR-0016 §決定 (c)+(j): triage is a hint, not an auto-apply switch.

    ``ProposalService.apply`` signature takes only
    ``(proposal_id, candidate_index)`` — the apply path has no
    branch on triage, so a parent ``triage="respond"`` cannot
    auto-route a candidate to durable state. The contract is held by
    **signature absence**: any future "auto-apply when triage = X"
    refactor would have to add a triage argument here (or read it from
    the projection), and that change would surface at code-review.

    This test pins the contract by ``inspect``ing the public
    :meth:`ProposalService.apply` signature: it must accept exactly
    ``self``, ``proposal_id``, and ``candidate_index`` — no
    ``triage`` / ``auto_apply`` parameter, no ``**kwargs`` escape
    hatch.
    """
    import inspect

    from opshub.services.proposals.service import ProposalService

    sig = inspect.signature(ProposalService.apply)
    params = list(sig.parameters.values())
    names = [p.name for p in params]
    assert names == ["self", "proposal_id", "candidate_index"], (
        f"ProposalService.apply signature drifted: {names}. "
        "ADR-0016 §決定 (c) HITL forbids triage-driven auto-apply — "
        "re-evaluation requires a new ADR that supersedes 0016."
    )
    # No VAR_KEYWORD / VAR_POSITIONAL backdoor that could smuggle in a
    # triage-driven branch via ``**kwargs``.
    kinds = {p.kind for p in params}
    assert inspect.Parameter.VAR_KEYWORD not in kinds
    assert inspect.Parameter.VAR_POSITIONAL not in kinds
