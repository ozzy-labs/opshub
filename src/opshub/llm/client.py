"""LLMClient Protocol — Phase 5 freeze + Phase 6 structured-output extension.

Mirrors the Phase 1 freeze pattern (Embedder / VectorStore). Concrete
implementations land in steps A3 (Anthropic) / A4 (OpenAI); this module
itself stays stdlib-only (plus Pydantic, which is already a base dep) so
config / CLI / tests can reference the Protocol without pulling in any
LLM SDK.

Phase 6 step A2 (ADR-0016 §決定 (a)+(b)) adds ``complete_structured`` to
the Protocol — extension only, no existing member is renamed or removed
so the Phase 5 freeze contract is preserved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel


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


@dataclass(frozen=True, slots=True)
class StructuredResponse[TSchema: BaseModel]:
    """A typed chat-completions response (Phase 6 step A2, ADR-0016 §決定 (b)).

    ``parsed`` is the Pydantic-validated instance constructed from the
    LLM's tool_call / tool_use arguments. Backends serialise the
    ``schema`` argument of :meth:`LLMClient.complete_structured` into
    their native tool-definition format (Anthropic ``tool_use`` /
    OpenAI-compatible ``tools=``) and parse the response back into
    ``parsed`` via JSON parse + Pydantic validate.

    ``tokens_in`` / ``tokens_out`` are surfaced from the provider SDK
    so :class:`ProposalService` can persist them on the
    ``ProposalGenerated`` event (operational visibility into per-
    proposal cost). ``model_id`` / ``model_version`` are surfaced for
    the same reason — proposals persist which backend produced them.
    """

    parsed: TSchema
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

    def complete_structured(
        self,
        messages: list[LLMMessage],
        *,
        schema: type[BaseModel],
        max_tokens: int,
        temperature: float = 0.2,
    ) -> StructuredResponse[BaseModel]:
        """Synchronous chat completion with structured output (ADR-0016 §決定 (a)+(b)).

        The provided ``schema`` Pydantic model is serialised to the
        backend's native tool-definition format and offered as the only
        tool. Backends MUST set tool_choice to "required" (or equivalent)
        so the response is guaranteed to be a tool call rather than free
        text. The tool-call arguments are JSON-parsed + Pydantic-validated
        and surfaced as ``StructuredResponse.parsed``.

        ``max_tokens`` is required (caller responsibility for cost
        control per ADR-0015 §決定 (h)).

        Backends raise :class:`OpsHubError` if the model returns text
        instead of a tool call, returns malformed JSON, or returns
        arguments that fail Pydantic validation. ``ConfigError`` is
        raised for fail-fast issues like missing extras / API key.
        """
        ...
