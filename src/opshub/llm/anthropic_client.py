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

from pydantic import BaseModel, ValidationError

from opshub.llm.client import LLMMessage, LLMResponse, StructuredResponse
from opshub.llm.schema import pydantic_to_tool_schema

__all__ = ["ANTHROPIC_API_KEY_SECRET", "AnthropicLLMClient"]

#: Keyring key used to store the Anthropic API key. Exposed so the CLI
#: command (Phase 5 step A5 ``opshub llm auth set anthropic``)
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

    def complete_structured(
        self,
        messages: list[LLMMessage],
        *,
        schema: type[BaseModel],
        max_tokens: int,
        temperature: float = 0.2,
    ) -> StructuredResponse[BaseModel]:
        """Synchronous chat completion with structured output via Anthropic ``tool_use``.

        Translation flow (ADR-0016 §決定 (a)+(b)):

        1. Convert ``schema`` Pydantic model → tool definition via
           :func:`opshub.llm.schema.pydantic_to_tool_schema` (shared
           converter, identical wire format used by OpenAI / Ollama).
        2. Wrap into Anthropic's tool-definition shape:
           ``{"name": ..., "description": ..., "input_schema": <parameters>}``.
        3. Call ``client.messages.create(..., tools=[tool_def],
           tool_choice={"type": "tool", "name": tool_name})`` so the
           model MUST emit a ``tool_use`` block (no free-text fallback).
        4. Extract the first ``tool_use`` content block. ``thinking``
           blocks are skipped (Claude sometimes emits them before
           tool_use). If no ``tool_use`` block is present, raise
           :class:`OpsHubError` listing the observed block types so the
           caller (ProposalService) can record a ``ProposalFailed``
           event with actionable debug info.
        5. Pydantic-validate ``block.input`` (already a parsed ``dict``
           per the SDK) into an instance of ``schema``.
        6. Return :class:`StructuredResponse` with the validated
           instance + token usage (mirrors :meth:`complete` mapping:
           ``response.usage.input_tokens`` / ``output_tokens``).
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

        # Shared converter returns ``{name, description, parameters}``.
        # Anthropic's tool-definition shape names the JSON-schema slot
        # ``input_schema`` (OpenAI keeps the OpenAI-style ``parameters``
        # key, hence the rename here rather than in the helper).
        tool_def_common = pydantic_to_tool_schema(schema)
        tool_name = tool_def_common["name"]
        anthropic_tool_def: dict[str, Any] = {
            "name": tool_name,
            "description": tool_def_common["description"],
            "input_schema": tool_def_common["parameters"],
        }

        kwargs: dict[str, Any] = {
            "model": self._model_id_value,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system_text,
            "messages": chat_messages,
            "tools": [anthropic_tool_def],
            # ``"type": "tool"`` + explicit ``name`` forces THIS tool
            # (not "any" / "auto"). The model has no free-text fallback.
            "tool_choice": {"type": "tool", "name": tool_name},
        }

        response: Any = client.messages.create(**kwargs)

        content_blocks: list[Any] = list(response.content)
        observed_types: list[str | None] = [
            getattr(block, "type", None) for block in content_blocks
        ]
        tool_use_block: Any = None
        for block in content_blocks:
            block_type = getattr(block, "type", None)
            if block_type == "thinking":
                # Claude may emit "thinking" blocks before tool_use in
                # tool-use contexts; they carry no payload we need.
                continue
            if block_type == "tool_use":
                tool_use_block = block
                break
            # Any other type (text, etc.) before tool_use means the
            # model deviated from the forced tool_choice contract.
            break

        if tool_use_block is None:
            from opshub.core.errors import OpsHubError

            raise OpsHubError(
                "AnthropicLLMClient.complete_structured: no tool_use block "
                f"in response; got types: {observed_types!r}"
            )

        # The Anthropic SDK already parses ``input`` into a ``dict``
        # (not a JSON string). Pass directly to Pydantic.
        try:
            parsed = schema.model_validate(tool_use_block.input)
        except ValidationError as exc:
            from opshub.core.errors import OpsHubError

            raise OpsHubError(
                "AnthropicLLMClient.complete_structured: tool_use input "
                f"failed schema validation: {exc}"
            ) from exc

        usage: Any = response.usage
        return StructuredResponse[BaseModel](
            parsed=parsed,
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
                    "`opshub llm auth set anthropic` or set "
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
