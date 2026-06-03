"""LLM client factory + NoOp fallback (Phase 5 step A5, ADR-0015).

Mirrors :mod:`opshub.vectors.factory` (the Phase 4 embedder factory).
:func:`build_llm_client` reads ``settings.llm`` and returns a concrete
:class:`~opshub.llm.LLMClient` for the configured backend, or a
:class:`NoOpLLMClient` when ``backend == "disabled"`` (the safe
default — see ADR-0015 §決定 (b)).

The ``"disabled"`` backend returns a :class:`NoOpLLMClient` whose
:meth:`~NoOpLLMClient.complete` raises :class:`~opshub.core.errors.ConfigError`
so the Briefing CLI surfaces a clear "configure [llm] backend" message
rather than silently returning empty markdown.

Lazy-import discipline
----------------------

Module-level imports are intentionally lightweight. Each concrete LLM
client is imported **inside** the branch that selects it, so
``import opshub.llm.factory`` does not pull in ``anthropic`` /
``openai`` SDKs. This preserves the ADR-0001 §3 cold-start budget — the
M6 guard test (``tests/integration/test_cli_imports.py``) covers the
CLI surface, and the focused
:func:`tests.unit.llm.test_factory.test_factory_module_does_not_import_heavy_deps`
test guards the factory module's own globals (mirrors the Phase 4
``opshub.vectors.factory`` precedent).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opshub.core.errors import ConfigError
from opshub.llm.client import LLMMessage, LLMResponse, StructuredResponse

if TYPE_CHECKING:
    from pydantic import BaseModel

    from opshub.core.config import OpsHubSettings
    from opshub.llm.client import LLMClient

__all__ = ["NoOpLLMClient", "build_llm_client"]


class NoOpLLMClient:
    """LLMClient returned when ``settings.llm.backend == "disabled"``.

    The identity properties return sentinel values so callers (e.g.
    ``opshub brief`` status output, future ``opshub llm status``
    diagnostic) can introspect a well-formed
    :class:`~opshub.llm.LLMClient` without special-casing the disabled
    state.

    :meth:`complete` raises :class:`~opshub.core.errors.ConfigError`
    instead of returning an empty :class:`~opshub.llm.LLMResponse` so
    callers cannot accidentally persist a ``BriefingGenerated`` event
    with empty markdown. The error message documents how to flip the
    backend on, mirroring ADR-0015 §決定 (b)'s "fail loud" stance and
    the Phase 4 :class:`opshub.vectors.factory.NoOpEmbedder` precedent.
    """

    @property
    def model_id(self) -> str:
        return "disabled"

    @property
    def model_version(self) -> str:
        return "disabled"

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int,
        temperature: float = 0.2,
        stop: list[str] | None = None,
    ) -> LLMResponse:
        # Arguments are unused; the ConfigError is the only effect. We
        # still accept the full Protocol signature so ``NoOpLLMClient``
        # remains a structural :class:`~opshub.llm.LLMClient` — the
        # caller never has to type-narrow before invoking ``complete``.
        del messages, max_tokens, temperature, stop
        raise ConfigError(
            "[llm] backend is disabled; configure 'anthropic' or 'openai' "
            "in opshub.toml (or set OPSHUB_LLM_BACKEND env var) and run "
            "`opshub llm auth set <backend>` to store the API key."
        )

    def complete_structured(
        self,
        messages: list[LLMMessage],
        *,
        schema: type[BaseModel],
        max_tokens: int,
        temperature: float = 0.2,
    ) -> StructuredResponse[BaseModel]:
        """Phase 6 step A2 Protocol extension — same fail-loud contract.

        Keeps :class:`NoOpLLMClient` a structural
        :class:`~opshub.llm.LLMClient` after ADR-0016 added
        ``complete_structured`` to the Protocol. The disabled backend
        cannot serve any LLM call, structured or otherwise, so we raise
        the same actionable :class:`~opshub.core.errors.ConfigError` as
        :meth:`complete`.
        """
        del messages, schema, max_tokens, temperature
        raise ConfigError(
            "[llm] backend is disabled; configure 'anthropic' or 'openai' "
            "in opshub.toml (or set OPSHUB_LLM_BACKEND env var) and run "
            "`opshub llm auth set <backend>` to store the API key."
        )


def build_llm_client(settings: OpsHubSettings) -> LLMClient:
    """Resolve the configured :class:`~opshub.llm.LLMClient`.

    Reads from ``settings.llm`` (the section introduced in this PR,
    Phase 5 step A5). Each branch lazily imports its concrete client
    module so the heavy SDK dependency is loaded only for the backend
    the operator selected — same discipline as the Phase 4 embedder
    factory.

    :param settings: Resolved root settings (typically obtained via
        :func:`opshub.core.config.get_settings` or a direct
        ``OpsHubSettings()`` construction in tests).
    :returns: A concrete client satisfying the
        :class:`~opshub.llm.LLMClient` Protocol.
    :raises ConfigError: When ``settings.llm.backend`` is not one of the
        documented literals. Mirrors the typed
        :data:`~opshub.core.config.LLMBackend` literal so any future
        addition without a matching factory branch fails loud.
    """
    backend = settings.llm.backend
    if backend == "disabled":
        return NoOpLLMClient()
    if backend == "anthropic":
        from opshub.llm.anthropic_client import AnthropicLLMClient

        return AnthropicLLMClient(
            model_id=settings.llm.anthropic.model_id,
            model_version=settings.llm.anthropic.model_version,
        )
    if backend == "openai":
        from opshub.llm.openai_client import OpenAILLMClient

        return OpenAILLMClient(
            model_id=settings.llm.openai.model_id,
            model_version=settings.llm.openai.model_version,
        )
    if backend == "ollama":
        # Phase 6 step A4, ADR-0016 §決定 (h) — local daemon backend.
        # The lazy import keeps ``httpx`` off the cold-start path; the
        # ``[llm-ollama]`` extras gates installation but the import
        # itself is gated again inside ``OllamaLLMClient.__init__`` so
        # the error message points at the extras name explicitly.
        from opshub.llm.ollama_client import OllamaLLMClient

        return OllamaLLMClient(
            model_id=settings.llm.ollama.model_id,
            model_version=settings.llm.ollama.model_version,
            host=settings.llm.ollama.host,
            timeout_seconds=settings.llm.ollama.timeout_seconds,
        )
    raise ConfigError(
        f"unknown llm backend {backend!r}; "
        "expected one of 'disabled', 'anthropic', 'openai', 'ollama'"
    )
