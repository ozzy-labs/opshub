"""OpenAI Chat Completions LLM client (Phase 5 step A4, ADR-0015).

Implements :class:`opshub.llm.LLMClient` Protocol using the official
``openai`` SDK's chat-completions API. The SDK is gated behind the
``[llm-openai]`` extras (ADR-0001), kept independent from the Phase 4
``[api-embedding-openai]`` extras so a user opting into one feature
does not implicitly enable the other (ADR-0015 §決定 (a)).

API key resolution mirrors the Phase 4 OpenAI embedder
(:mod:`opshub.vectors.openai_embedder`) and Phase 3's ADR-0014 token
storage contract:

- keyring key: ``llm:openai:api_key``
- env var override: ``OPSHUB_LLM_OPENAI_API_KEY``

The env override is honoured by :func:`opshub.core.secrets.get_secret`
itself, so this module just calls ``get_secret`` and never reads
``os.environ`` directly.

The SDK import is **lazy** (deferred to first :meth:`complete` call) so
that ``import opshub.llm.openai_client`` succeeds even when the extras
are not installed. This keeps cold-start light and matches the Phase 3
connector pattern + Phase 4 embedder pattern.

The ``openai`` SDK is optional in CI (the ``justfile`` ``ci`` recipe
installs ``[llm-openai]`` so the tests can import it; production users
who only want embeddings install ``[api-embedding-openai]`` instead).
Static type-checkers see the SDK as optional, so we type the client as
:class:`Any` and pin the dynamic-import line — same strategy as
``opshub.vectors.openai_embedder``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from opshub.llm.client import LLMMessage, LLMResponse, StructuredResponse

__all__ = ["OPENAI_API_KEY_SECRET", "OpenAILLMClient"]

#: Keyring key used to store the OpenAI LLM API key. Exposed so a future
#: ``opshub connector auth set llm:openai`` CLI (Phase 5 step A5) writes
#: to the exact key this client reads at completion time.
OPENAI_API_KEY_SECRET = "llm:openai:api_key"


class OpenAILLMClient:
    """LLM client backed by OpenAI's ``/v1/chat/completions`` endpoint.

    Satisfies :class:`opshub.llm.LLMClient` structurally (the Protocol is
    ``@runtime_checkable``). Following the Phase 4 embedder precedent
    (:class:`opshub.vectors.openai_embedder.OpenAIEmbedder`), the class
    does **not** inherit from the Protocol — structural conformance is
    asserted by ``test_satisfies_llm_client_protocol`` instead, which
    catches surface drift just as effectively without coupling the
    concrete class to ``Protocol``'s metaclass machinery.

    Network / SDK access is deferred until the first :meth:`complete`
    call so the module is safe to import without the ``[llm-openai]``
    extras installed.
    """

    def __init__(
        self,
        *,
        model_id: str = "gpt-4o-mini",
        model_version: str = "2026-05-01",
        api_key: str | None = None,
    ) -> None:
        """Create a new client.

        :param model_id: OpenAI chat-completions model identifier
            (default ``"gpt-4o-mini"`` per ADR-0015 §決定 (c)). The
            operator can override via constructor / config.
        :param model_version: Stable version tag stored alongside the
            completion so callers can detect a model upgrade. OpenAI's
            chat models don't expose a stable version string the way
            embedding models do; we record the date the default was
            pinned in Phase 5 (operator can override).
        :param api_key: Explicit API key. If ``None`` (default), the
            key is resolved lazily at first :meth:`complete` call via
            :func:`opshub.core.secrets.get_secret` (which honours the
            ``OPSHUB_LLM_OPENAI_API_KEY`` env override).
        """
        self._model_id_value = model_id
        self._model_version_value = model_version
        self._explicit_api_key = api_key
        self._client: Any = None

    @property
    def model_id(self) -> str:
        return self._model_id_value

    @property
    def model_version(self) -> str:
        return self._model_version_value

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int,
        temperature: float = 0.2,
        stop: list[str] | None = None,
    ) -> LLMResponse:
        """Run a chat completion against OpenAI.

        See :class:`opshub.llm.LLMClient.complete` for the Protocol
        contract. SDK exceptions propagate as-is — the caller
        (BriefingService in Phase 5 step B3) maps them to OpsHub-typed
        errors and ``BriefingFailed`` events (ADR-0015 §決定 (h)).
        """
        client = self._ensure_client()
        # OpenAI chat-completions accepts ``system`` messages directly in
        # the messages array — no special parameter handling needed
        # (unlike Anthropic, where the system prompt is a separate
        # ``system=`` kwarg).
        api_messages = [{"role": m.role, "content": m.content} for m in messages]
        kwargs: dict[str, Any] = {
            "model": self._model_id_value,
            "messages": api_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        # Omit ``stop`` when ``None`` rather than forwarding the literal
        # ``None``: OpenAI accepts both but the omission is cleaner in
        # mocks and tracing.
        if stop is not None:
            kwargs["stop"] = stop

        response = client.chat.completions.create(**kwargs)

        # Defensive extraction — the SDK normally returns one choice with
        # a string ``content``. If a tool-use response or a refusal
        # zeroes out ``content``, fail loud so the briefing layer can
        # record ``BriefingFailed`` instead of silently storing an empty
        # markdown (ADR-0015 §決定 (g)).
        choice = response.choices[0]
        content = choice.message.content
        if not isinstance(content, str) or not content:
            from opshub.core.errors import OpsHubError

            raise OpsHubError(
                "OpenAILLMClient: chat completion returned empty content "
                f"(model={self._model_id_value!r}, finish_reason="
                f"{getattr(choice, 'finish_reason', None)!r})"
            )

        usage = response.usage
        return LLMResponse(
            text=content,
            model_id=self._model_id_value,
            model_version=self._model_version_value,
            tokens_in=int(usage.prompt_tokens),
            tokens_out=int(usage.completion_tokens),
        )

    def complete_structured(
        self,
        messages: list[LLMMessage],
        *,
        schema: type[BaseModel],
        max_tokens: int,
        temperature: float = 0.2,
    ) -> StructuredResponse[BaseModel]:
        """Structured-output completion (Phase 6 step A2 Protocol stub).

        The full OpenAI ``tools=`` function-calling round-trip is
        implemented in Phase 6 step A3. This stub exists so
        :class:`OpenAILLMClient` satisfies the extended
        :class:`~opshub.llm.LLMClient` Protocol's ``runtime_checkable``
        isinstance check at A2 merge time.
        """
        raise NotImplementedError(
            "OpenAILLMClient.complete_structured is implemented in Phase 6 step A3"
        )

    def _ensure_client(self) -> Any:
        """Return a cached SDK client, constructing it on first call.

        Raises :class:`~opshub.core.errors.ConfigError` if the SDK
        extras are missing or no API key is configured (neither
        explicitly passed to the constructor, nor in keyring / env).
        """
        if self._client is None:
            try:
                openai_module: Any = __import__("openai")
            except ImportError as exc:
                from opshub.core.errors import ConfigError

                raise ConfigError(
                    "OpenAILLMClient requires the 'llm-openai' extras: "
                    "uv pip install 'opshub[llm-openai]'"
                ) from exc

            api_key = self._explicit_api_key
            if api_key is None:
                from opshub.core.secrets import get_secret

                api_key = get_secret(OPENAI_API_KEY_SECRET)
            if not api_key:
                from opshub.core.errors import ConfigError

                raise ConfigError(
                    "OpenAI LLM API key not configured. Run "
                    "`opshub connector auth set llm:openai` or set "
                    "OPSHUB_LLM_OPENAI_API_KEY in the environment."
                )
            self._client = openai_module.OpenAI(api_key=api_key)
        client: Any = self._client
        return client
