"""Pluggable LLM client interface (Phase 5 surface, frozen now).

This module is intentionally **Protocol-only**: no concrete implementation, no
LLM SDK import. We freeze the surface in Phase 5 entry so that config (step
A5) / BriefingService (step B3) / CLI (step B4) can reference ``LLMClient``
by name without dragging ``anthropic`` / ``openai`` into core install
(ADR-0001, ADR-0015).

Design notes:

- Messages travel across the boundary as plain ``LLMMessage`` dataclasses
  (``role`` + ``content``). Tool-use / multimodal blocks are deferred to
  Phase 5.x; the briefing MVP only needs text-in / text-out.
- Token counts are surfaced on ``LLMResponse`` so ``BriefingService`` can
  persist per-briefing cost on the ``BriefingGenerated`` event (ADR-0015
  §決定 (g)).
- Protocol is ``@runtime_checkable`` so duck-typed fakes pass ``isinstance``
  in the few code paths that need a runtime guard (mirroring the Phase 1
  Embedder / VectorStore freeze).
- ``llm/`` may import from ``core/`` only (ADR-0004 dependency direction).
"""

from opshub.llm.client import LLMClient, LLMMessage, LLMResponse

__all__ = [
    "LLMClient",
    "LLMMessage",
    "LLMResponse",
]
