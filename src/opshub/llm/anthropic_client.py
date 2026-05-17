"""Anthropic Claude LLM client (Phase 5 step A3, ADR-0015).

Implements :class:`opshub.llm.LLMClient` Protocol using the official
``anthropic`` SDK. The SDK is gated behind the ``[llm-anthropic]`` extras
(ADR-0001 + ADR-0015 §決定 (b)) — this module guards the import and
raises :class:`~opshub.core.errors.ConfigError` with an actionable install
hint when the SDK is missing.

Credential resolution follows ADR-0014 / ADR-0015 §決定 (d):

- keyring key: ``llm:anthropic:api_key``
- env var override: ``OPSHUB_LLM_ANTHROPIC_API_KEY`` (wins over keyring)

The SDK import is **lazy** (deferred to first :meth:`AnthropicLLMClient.complete`
call) so that ``import opshub.llm.anthropic_client`` succeeds even when
the extras are not installed. This mirrors the Phase 4 OpenAI embedder
pattern (``opshub.vectors.openai_embedder``) and keeps the cold-start
path light.

The ``anthropic`` SDK is opt-in extras and not present on every install,
so we type the client as :class:`Any` and pin the dynamic-import line
with a focused ``type: ignore``. The codebase stays strict overall while
this single optional dependency stays untyped at the module boundary.
"""

from __future__ import annotations

from typing import Any

from opshub.llm.client import LLMMessage, LLMResponse

__all__ = ["ANTHROPIC_API_KEY_SECRET", "AnthropicLLMClient"]

#: Keyring key used to store the Anthropic API key. Exposed so the CLI
#: command (Phase 5 step A5 ``opshub connector auth set llm:anthropic``)
#: writes to the exact same key this client reads at complete time.
ANTHROPIC_API_KEY_SECRET = "llm:anthropic:api_key"


class AnthropicLLMClient:
    """LLM client backed by Anthropic's Messages API.

    Implements the :class:`opshub.llm.LLMClient` Protocol. Network / SDK
    access is deferred until the first :meth:`complete` call so the
    module is safe to import without the ``[llm-anthropic]`` extras
    installed (matches the Phase 4 OpenAI embedder pattern).

    System messages from the input list are concatenated and passed via
    the SDK's ``system=`` kwarg (Anthropic's Messages API treats system
    prompts as a top-level field, **not** as a role inside ``messages``).
    Remaining ``user`` / ``assistant`` messages are forwarded as-is.

    Per ADR-0015 §決定 (g), API keys are never logged or placed on event
    payloads; the key lives only on the in-memory SDK client. Sanitised
    error paths (Phase 5 step B1) cover the failure side.
    """

    def __init__(
        self,
        *,
        model_id: str = "claude-haiku-4-5-20251001",
        model_version: str = "2026-05-01",
        api_key: str | None = None,
    ) -> None:
        """Create a new client.

        :param model_id: Anthropic model identifier. Default
            (``claude-haiku-4-5-20251001``) is the Phase 5 推奨 model
            from ADR-0015 §決定 (c) — cost-effective Haiku tier, briefing
            does not need tool_use.
        :param model_version: Stable version tag persisted alongside the
            ``BriefingGenerated`` event so callers can detect a model
            upgrade. Anthropic publishes a model ``id`` already
            embedding the release date; we keep ``model_version`` as a
            separate field for parity with :class:`opshub.llm.LLMResponse`
            and the Phase 4 ``Embedder`` pattern.
        :param api_key: Explicit API key. If ``None``, the key is
            resolved at first :meth:`complete` call via
            :func:`opshub.core.secrets.get_secret` with the
            ``OPSHUB_LLM_ANTHROPIC_API_KEY`` env-var override (ADR-0014).
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
        """Synchronous chat completion via Anthropic Messages API.

        System messages are split out and concatenated with ``\\n\\n``
        into the SDK's ``system=`` kwarg (Anthropic does not accept
        ``role="system"`` inside ``messages``). The remaining
        ``user`` / ``assistant`` messages are forwarded as-is.

        ``stop`` is only forwarded as ``stop_sequences`` when non-``None``
        so the SDK applies its default no-stop behaviour otherwise.

        The response must be a single text block — we do not request
        tool use, and a ``tool_use`` block would indicate the model
        ignored the prompt structure (hard error). Token usage is
        sourced from ``response.usage.input_tokens`` /
        ``output_tokens`` so callers (BriefingService) can persist
        per-briefing cost on the ``BriefingGenerated`` event.
        """
        client = self._ensure_client()
        system_parts: list[str] = []
        chat_messages: list[dict[str, str]] = []
        for message in messages:
            if message.role == "system":
                system_parts.append(message.content)
            else:
                chat_messages.append({"role": message.role, "content": message.content})
        system_text = "\n\n".join(system_parts)

        kwargs: dict[str, Any] = {
            "model": self._model_id_value,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system_text,
            "messages": chat_messages,
        }
        if stop is not None:
            kwargs["stop_sequences"] = stop

        response: Any = client.messages.create(**kwargs)

        content_blocks: list[Any] = list(response.content)
        if not content_blocks:
            raise RuntimeError(
                "AnthropicLLMClient: response.content is empty; expected one text block"
            )
        first_block = content_blocks[0]
        block_type = getattr(first_block, "type", None)
        if block_type != "text":
            raise RuntimeError(
                f"AnthropicLLMClient: expected first content block type 'text', "
                f"got {block_type!r}. Tool-use blocks are not requested by this client."
            )
        text: str = first_block.text
        usage: Any = response.usage
        return LLMResponse(
            text=text,
            model_id=self._model_id_value,
            model_version=self._model_version_value,
            tokens_in=int(usage.input_tokens),
            tokens_out=int(usage.output_tokens),
        )

    def _ensure_client(self) -> Any:
        """Return a cached SDK client, constructing it on first call.

        Raises :class:`~opshub.core.errors.ConfigError` if the SDK
        extras are missing or no API key is configured (neither passed
        to the constructor, nor in keyring, nor via the documented env
        var override).
        """
        if self._client is None:
            try:
                anthropic_module: Any = __import__("anthropic")
            except ImportError as exc:
                from opshub.core.errors import ConfigError

                raise ConfigError(
                    "AnthropicLLMClient requires the 'llm-anthropic' extras: "
                    "uv pip install 'opshub[llm-anthropic]'"
                ) from exc

            api_key = self._resolve_api_key()
            if not api_key:
                from opshub.core.errors import ConfigError

                raise ConfigError(
                    "Anthropic API key not configured. Run "
                    "`opshub connector auth set llm:anthropic` or set "
                    "OPSHUB_LLM_ANTHROPIC_API_KEY in the environment."
                )
            self._client = anthropic_module.Anthropic(api_key=api_key)
        client: Any = self._client
        return client

    def _resolve_api_key(self) -> str | None:
        """Resolve the API key: constructor arg → env var → keyring.

        Constructor-supplied keys always win (callers wiring up tests or
        bespoke setups). Otherwise fall back to the standard
        :func:`opshub.core.secrets.get_secret` path, which itself
        consults ``OPSHUB_LLM_ANTHROPIC_API_KEY`` before touching
        keyring (ADR-0014).
        """
        if self._explicit_api_key is not None:
            return self._explicit_api_key
        from opshub.core.secrets import get_secret

        return get_secret(ANTHROPIC_API_KEY_SECRET)
