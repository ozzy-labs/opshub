"""Ollama local LLM client (Phase 6 step A4, ADR-0016 §決定 (h)).

Talks to a locally running Ollama daemon via its OpenAI-compatible
chat-completions endpoint (``<host>/v1/chat/completions``). The wire
format is identical to OpenAI's, so the same request / response
parsing pattern as :class:`opshub.llm.openai_client.OpenAILLMClient`
applies — including ``tool_use`` via the ``tools=`` field and
``tool_calls`` in responses.

Why Ollama and not ``llama.cpp`` direct (ADR-0016 §決定 (h)):

* Ollama daemon abstracts model file management, GPU detection,
  cross-platform packaging — operators don't need to ship a 4-30 GB
  model file with their opshub install (ADR-0001 distribution).
* OpenAI-compatible endpoint means we reuse the OpenAI translation
  layer (tool_use, system messages, token usage shape).
* No API key needed (local daemon); only ``host`` is configurable.

ADR-0015 §決定 (a) "Local LLM deferred" is closed by this backend
landing; the Phase 6 C1 closeout PR removes the corresponding entry
from ADR-0015 Known Limitations.

The ``httpx`` import is **lazy** (deferred to ``__init__``'s body) so
that ``import opshub.llm.ollama_client`` succeeds even when the
``[llm-ollama]`` extras are not installed — mirrors the Phase 5
SDK-gating pattern used by Anthropic / OpenAI clients.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from opshub.core.errors import ConfigError, OpsHubError
from opshub.llm.client import LLMMessage, LLMResponse, StructuredResponse
from opshub.llm.schema import pydantic_to_tool_schema

if TYPE_CHECKING:
    from pydantic import BaseModel

__all__ = ["OllamaLLMClient"]


class OllamaLLMClient:
    """LLMClient backed by a local Ollama daemon (OpenAI-compatible).

    Satisfies :class:`opshub.llm.LLMClient` structurally — the Protocol
    is ``@runtime_checkable`` so duck-typing is sufficient. The class
    deliberately does not inherit from the Protocol (mirrors the Phase 5
    OpenAI / Anthropic clients), keeping the runtime check decoupled
    from Protocol's metaclass machinery.

    Network / SDK access is deferred to first use only for the
    ``httpx`` *import*; the ``httpx.Client`` instance itself is built in
    ``__init__`` so that fail-fast probe (``GET /api/tags``) surfaces a
    missing daemon at construction time. This matches ADR-0016 §決定 (h)
    "daemon not reachable → ``ConfigError`` fail-fast".

    No API key handling: the Ollama daemon is unauthenticated by design
    (local-only). :func:`opshub.core.secrets.get_secret` is intentionally
    NOT called.
    """

    def __init__(
        self,
        *,
        model_id: str = "llama3.2:3b",
        model_version: str = "ollama",
        host: str = "http://localhost:11434",
        timeout_seconds: float = 60.0,
    ) -> None:
        """Create a new client.

        :param model_id: Ollama model tag (default ``"llama3.2:3b"`` per
            Phase 6 plan §2.1 / ADR-0016 §決定 (h)). The operator must
            have run ``ollama pull <model_id>`` already; we fail-fast
            on a missing daemon, and the daemon itself fails when asked
            to complete with an unknown model.
        :param model_version: Stable version tag persisted alongside the
            completion so callers can detect a model upgrade. Ollama
            does not surface a stable version string (the model tag
            *is* the version), so the default ``"ollama"`` is a
            backend-identifier sentinel; operators can override.
        :param host: Ollama daemon base URL (default
            ``"http://localhost:11434"``).
        :param timeout_seconds: HTTP timeout for chat completions.
            Local model latency varies wildly with hardware, so this
            is exposed explicitly.

        :raises ConfigError: When the ``httpx`` extras are missing, or
            when the daemon is not reachable at ``host``.
        """
        # Lazy import httpx (extras-gated, matches Phase 5 SDK gating).
        try:
            import httpx
        except ImportError as exc:
            raise ConfigError(
                "OllamaLLMClient requires the 'llm-ollama' extras: "
                "uv pip install 'opshub[llm-ollama]' "
                "(or: uv sync --extra llm-ollama)"
            ) from exc

        self._model_id_value = model_id
        self._model_version_value = model_version
        self._host = host.rstrip("/")
        self._timeout_seconds = timeout_seconds
        # Keep the ``httpx`` module on the instance for error-class
        # references (``httpx.HTTPError`` etc.) inside :meth:`complete` /
        # :meth:`complete_structured` so we don't re-import in hot paths.
        self._httpx: Any = httpx
        self._client: Any = httpx.Client(base_url=self._host, timeout=timeout_seconds)
        self._probe_daemon()

    @property
    def model_id(self) -> str:
        return self._model_id_value

    @property
    def model_version(self) -> str:
        return self._model_version_value

    def _probe_daemon(self) -> None:
        """Verify the daemon is reachable; fail-fast (ADR-0016 §決定 (h)).

        Uses the Ollama-native ``GET /api/tags`` endpoint (lists locally
        installed models) because it has no required body, returns
        quickly, and works on every Ollama version. The OpenAI-compat
        endpoint ``/v1/chat/completions`` would require a payload.
        """
        try:
            response = self._client.get("/api/tags", timeout=5.0)
            response.raise_for_status()
        except Exception as exc:
            raise ConfigError(
                f"Ollama daemon not reachable at {self._host}. "
                f"Install ollama (https://ollama.com) and run "
                f"`ollama serve` + `ollama pull {self._model_id_value}`."
            ) from exc

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int,
        temperature: float = 0.2,
        stop: list[str] | None = None,
    ) -> LLMResponse:
        """Run a chat completion via the OpenAI-compatible endpoint.

        See :class:`opshub.llm.LLMClient.complete` for the Protocol
        contract. The wire format mirrors OpenAI's chat-completions
        request / response shape exactly, so the implementation parallels
        :class:`opshub.llm.openai_client.OpenAILLMClient.complete`.

        ``stop`` is omitted from the payload when ``None`` (cleaner in
        traces; the OpenAI-compat endpoint accepts both shapes).

        :raises OpsHubError: On HTTP errors from the daemon, network
            failures, malformed responses, or empty completions.
        """
        api_messages = [{"role": m.role, "content": m.content} for m in messages]
        payload: dict[str, Any] = {
            "model": self._model_id_value,
            "messages": api_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if stop is not None:
            payload["stop"] = stop

        body = self._post_chat_completions(payload)
        choice = self._first_choice(body)
        message_obj = choice.get("message")
        if not isinstance(message_obj, dict):
            raise OpsHubError(
                "OllamaLLMClient: chat completion response missing 'message' object "
                f"(model={self._model_id_value!r})"
            )
        # Narrow to ``dict[str, Any]`` for downstream member access.
        # Pyright otherwise reports ``Unknown`` types on ``dict.get``.
        message = cast(dict[str, Any], message_obj)
        content = message.get("content")
        if not isinstance(content, str) or not content:
            raise OpsHubError(
                "OllamaLLMClient: chat completion returned empty content "
                f"(model={self._model_id_value!r}, finish_reason="
                f"{choice.get('finish_reason')!r})"
            )

        tokens_in, tokens_out = self._extract_usage(body)
        return LLMResponse(
            text=content,
            model_id=self._model_id_value,
            model_version=self._model_version_value,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

    def complete_structured(
        self,
        messages: list[LLMMessage],
        *,
        schema: type[BaseModel],
        max_tokens: int,
        temperature: float = 0.2,
    ) -> StructuredResponse[BaseModel]:
        """Run a structured-output completion via ``tools=`` function calling.

        Ollama's OpenAI-compat layer follows OpenAI's tool-calling spec:
        the request carries ``tools=[{"type": "function", "function":
        {...}}]`` + ``tool_choice={"type": "function", "function":
        {"name": ...}}``, and the response shape is
        ``choices[0].message.tool_calls[0].function.arguments`` (a JSON
        string).

        :raises OpsHubError: When the response does not contain a tool
            call, the arguments are not valid JSON, or the parsed
            arguments fail Pydantic validation.
        """
        tool_schema = pydantic_to_tool_schema(schema)
        tool_name = tool_schema["name"]
        # OpenAI / Ollama tool-call wire shape — ``strict: True`` is a
        # documented OpenAI extension that the Ollama compat layer
        # accepts as a no-op when the underlying model has no strict
        # decoding; including it preserves parity with OpenAI's
        # behaviour when an opshub operator points the same Pydantic
        # schema at both backends.
        function_def: dict[str, Any] = {
            "name": tool_name,
            "description": tool_schema["description"],
            "parameters": tool_schema["parameters"],
            "strict": True,
        }
        tools_payload: list[dict[str, Any]] = [{"type": "function", "function": function_def}]
        tool_choice: dict[str, Any] = {
            "type": "function",
            "function": {"name": tool_name},
        }

        api_messages = [{"role": m.role, "content": m.content} for m in messages]
        payload: dict[str, Any] = {
            "model": self._model_id_value,
            "messages": api_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "tools": tools_payload,
            "tool_choice": tool_choice,
        }

        body = self._post_chat_completions(payload)
        choice = self._first_choice(body)
        message_obj = choice.get("message")
        if not isinstance(message_obj, dict):
            raise OpsHubError(
                "OllamaLLMClient: structured completion response missing 'message' object "
                f"(model={self._model_id_value!r})"
            )
        message = cast(dict[str, Any], message_obj)
        tool_calls_obj = message.get("tool_calls")
        if not isinstance(tool_calls_obj, list) or not tool_calls_obj:
            raise OpsHubError(
                "OllamaLLMClient: structured completion returned no tool_calls "
                f"(model={self._model_id_value!r}, finish_reason="
                f"{choice.get('finish_reason')!r}). The model returned free text "
                "instead of invoking the requested tool."
            )
        # ``isinstance(x, list)`` narrows to ``list[Unknown]`` under
        # pyright's strict mode; the contents are application JSON so
        # we treat them as ``Any`` for [0] access without spurious
        # unknown-type fallout. Mirrors the ``schema.py`` precedent.
        tool_calls = tool_calls_obj  # pyright: ignore[reportUnknownVariableType]
        first_call = tool_calls[0]  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(first_call, dict):
            raise OpsHubError(
                f"OllamaLLMClient: tool_calls[0] is not an object (model={self._model_id_value!r})"
            )
        first_call_dict = cast(dict[str, Any], first_call)
        function_obj = first_call_dict.get("function")
        if not isinstance(function_obj, dict):
            raise OpsHubError(
                "OllamaLLMClient: tool_calls[0].function is not an object "
                f"(model={self._model_id_value!r})"
            )
        function_dict = cast(dict[str, Any], function_obj)
        arguments = function_dict.get("arguments")
        if not isinstance(arguments, str):
            raise OpsHubError(
                "OllamaLLMClient: tool_calls[0].function.arguments is not a JSON string "
                f"(model={self._model_id_value!r})"
            )
        try:
            parsed_args = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise OpsHubError(
                "OllamaLLMClient: tool_calls[0].function.arguments is not valid JSON "
                f"(model={self._model_id_value!r}): {exc}"
            ) from exc
        try:
            parsed = schema.model_validate(parsed_args)
        except Exception as exc:
            raise OpsHubError(
                "OllamaLLMClient: tool_calls[0].function.arguments failed Pydantic "
                f"validation against {schema.__name__} "
                f"(model={self._model_id_value!r}): {exc}"
            ) from exc

        tokens_in, tokens_out = self._extract_usage(body)
        return StructuredResponse(
            parsed=parsed,
            model_id=self._model_id_value,
            model_version=self._model_version_value,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

    def close(self) -> None:
        """Close the underlying ``httpx.Client``.

        Not strictly required — the connection pool is GC-managed — but
        provided so long-lived processes can release the daemon socket
        explicitly when the client goes out of scope.
        """
        self._client.close()

    # ---- internals -------------------------------------------------------

    def _post_chat_completions(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST to ``/v1/chat/completions`` and return the parsed JSON body.

        Wraps every ``httpx`` failure path in :class:`OpsHubError` so
        the caller (ProposalService in Phase 6 step B3, BriefingService
        in Phase 5 step B3) receives a stable error type. Network
        failures (``ConnectError`` / ``ReadTimeout`` etc.) are
        distinguished from HTTP-status failures because the operator
        action differs ("is the daemon up?" vs "is the request shape
        right?").
        """
        httpx_mod = self._httpx
        try:
            response = self._client.post("/v1/chat/completions", json=payload)
            response.raise_for_status()
        except httpx_mod.HTTPStatusError as exc:
            # response is bound on this exception in httpx's contract.
            status = exc.response.status_code
            # Truncate the body in the error message to keep logs sane —
            # a 4xx daemon response with a long stack trace would
            # otherwise flood operator output.
            body_excerpt = (exc.response.text or "")[:500]
            raise OpsHubError(f"Ollama API error: {status} {body_excerpt}") from exc
        except httpx_mod.HTTPError as exc:
            # Covers ConnectError, ReadTimeout, RemoteProtocolError, etc.
            raise OpsHubError("Ollama daemon connection lost during request") from exc

        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise OpsHubError("OllamaLLMClient: response body is not valid JSON") from exc
        if not isinstance(body, dict):
            raise OpsHubError("OllamaLLMClient: response body is not a JSON object")
        return cast(dict[str, Any], body)

    @staticmethod
    def _first_choice(body: dict[str, Any]) -> dict[str, Any]:
        """Return ``body["choices"][0]`` as a dict, raising on shape drift."""
        choices_obj = body.get("choices")
        if not isinstance(choices_obj, list) or not choices_obj:
            raise OpsHubError("OllamaLLMClient: response 'choices' is missing or empty")
        # Mirror ``schema.py``: narrowed ``list`` elements are JSON, treat
        # as ``Any`` to avoid Unknown propagation through [0].
        choices = choices_obj  # pyright: ignore[reportUnknownVariableType]
        first = choices[0]  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(first, dict):
            raise OpsHubError("OllamaLLMClient: response 'choices[0]' is not an object")
        return cast(dict[str, Any], first)

    @staticmethod
    def _extract_usage(body: dict[str, Any]) -> tuple[int, int]:
        """Pull (prompt_tokens, completion_tokens) from the response.

        Ollama's OpenAI-compat layer populates ``usage`` with
        ``prompt_tokens`` / ``completion_tokens`` keys. Older daemon
        versions or certain models omit one or both; we default to 0
        so the :class:`LLMResponse` / :class:`StructuredResponse`
        contract (non-negative ints) is always satisfied.
        """
        usage_obj = body.get("usage")
        if not isinstance(usage_obj, dict):
            return (0, 0)
        usage = cast(dict[str, Any], usage_obj)
        tokens_in_raw = usage.get("prompt_tokens", 0)
        tokens_out_raw = usage.get("completion_tokens", 0)
        try:
            tokens_in = int(tokens_in_raw)
        except (TypeError, ValueError):
            tokens_in = 0
        try:
            tokens_out = int(tokens_out_raw)
        except (TypeError, ValueError):
            tokens_out = 0
        return (tokens_in, tokens_out)
