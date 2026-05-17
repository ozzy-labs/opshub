"""Pluggable LLM client interface (Phase 5 surface, Phase 6 extended).

This module is intentionally **Protocol-only**: no concrete implementation, no
LLM SDK import. We freeze the surface in Phase 5 entry so that config (step
A5) / BriefingService (step B3) / CLI (step B4) can reference ``LLMClient``
by name without dragging ``anthropic`` / ``openai`` into core install
(ADR-0001, ADR-0015). Phase 6 step A2 (ADR-0016) extends the Protocol with
``complete_structured`` — extension only, the Phase 5 freeze contract
(no rename / no removal) is preserved.

Design notes:

- Messages travel across the boundary as plain ``LLMMessage`` dataclasses
  (``role`` + ``content``). Tool-use / multimodal blocks are deferred to
  Phase 5.x; the briefing MVP only needs text-in / text-out.
- Token counts are surfaced on ``LLMResponse`` / ``StructuredResponse`` so
  BriefingService / ProposalService can persist per-call cost on the
  ``BriefingGenerated`` / ``ProposalGenerated`` event (ADR-0015 §決定 (g),
  ADR-0016 §決定 (b)).
- Protocol is ``@runtime_checkable`` so duck-typed fakes pass ``isinstance``
  in the few code paths that need a runtime guard (mirroring the Phase 1
  Embedder / VectorStore freeze).
- ``llm/`` may import from ``core/`` only (ADR-0004 dependency direction).
"""

from opshub.llm.client import (
    LLMClient,
    LLMMessage,
    LLMResponse,
    StructuredResponse,
)

__all__ = [
    "LLMClient",
    "LLMMessage",
    "LLMResponse",
    "StructuredResponse",
]
