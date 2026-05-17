"""Proposal service package (Phase 6 step B3, ADR-0016).

Splits the proposal service across two modules:

- :mod:`opshub.services.proposals.prompts` — inline Python prompt
  constants and the :func:`render_user_prompt` helper. Reuses the
  Phase 5 D1 follow-up contract (HTML-escape + ``<source>`` delimiter
  wrap + "do not follow instructions" preamble) so the structured
  output path is hardened against prompt injection on the same
  contract surface as briefings (ADR-0015 §決定 (f) + ADR-0016).
- :mod:`opshub.services.proposals.service` — :class:`ProposalService`,
  the operational orchestrator that assembles topic-relevant entities
  via :class:`~opshub.services.recall_service.RecallService` (with an
  optional briefing seed via ``from_briefing_id``), calls the
  configured :class:`~opshub.llm.client.LLMClient.complete_structured`,
  and records the result via the standard event-sourced UoW pattern
  (event append + projection apply in one transaction).

The package layout mirrors :mod:`opshub.services.briefings` so a
reader who has internalised the Phase 5 briefing flow finds the same
"service + prompts" decomposition for Phase 6 proposals.
"""

from opshub.services.proposals.prompts import (
    SYSTEM_PROMPT,
    USER_PROMPT_PREAMBLE,
    render_user_prompt,
)
from opshub.services.proposals.service import (
    Proposal,
    ProposalCandidatesSchema,
    ProposalService,
)

__all__ = [
    "SYSTEM_PROMPT",
    "USER_PROMPT_PREAMBLE",
    "Proposal",
    "ProposalCandidatesSchema",
    "ProposalService",
    "render_user_prompt",
]
