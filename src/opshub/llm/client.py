"""LLMClient Protocol — Phase 5 surface, frozen at Phase 5 entry.

Mirrors the Phase 1 freeze pattern (Embedder / VectorStore). Concrete
implementations land in steps A3 (Anthropic) / A4 (OpenAI); this module
itself stays stdlib-only so config / CLI / tests can reference the
Protocol without pulling in any LLM SDK.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class LLMMessage:
    """One chat-completions message.

    ``role`` is the canonical OpenAI / Anthropic schema (system / user /
    assistant). Tool messages are deferred to Phase 5.x; this MVP only
    needs text-in / text-out for briefing generation.
    """

    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """One chat-completions response.

    ``tokens_in`` / ``tokens_out`` are surfaced from the provider SDK
    so BriefingService can persist them on the BriefingGenerated event
    (operational visibility into per-briefing cost).
    """

    text: str
    model_id: str
    model_version: str
    tokens_in: int
    tokens_out: int


@runtime_checkable
class LLMClient(Protocol):
    """Chat-completions style LLM client."""

    @property
    def model_id(self) -> str: ...

    @property
    def model_version(self) -> str: ...

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int,
        temperature: float = 0.2,
        stop: list[str] | None = None,
    ) -> LLMResponse:
        """Synchronous chat completion.

        ``max_tokens`` is required (caller responsibility for cost
        control per ADR-0015 §決定 (h)). ``stop`` is optional; backends
        that lack stop-sequence support should ignore it.
        """
        ...
