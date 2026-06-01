"""Tests for ``opshub.llm.ollama_client`` (Phase 6 step A4, ADR-0016 §決定 (h)).

The ``httpx`` library is in the ``[llm-ollama]`` extras (also pulled by
``[connectors-github]``); ``pytest.importorskip`` at module load gates
the entire file the same way the Phase 5 LLM client tests gate on the
matching SDK extras.

Every test routes HTTP traffic through :class:`httpx.MockTransport` so
the suite never reaches a real Ollama daemon. The pattern mirrors
``tests/unit/connectors/github/test_api.py``: a small ``routes`` table
keyed by ``(method, path)`` feeds a mock transport, and any unexpected
request raises ``AssertionError`` from the handler.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip(
    "httpx",
    reason="opshub.llm.ollama_client tests require the 'llm-ollama' extras",
)

import httpx
from pydantic import BaseModel

from opshub.core.errors import ConfigError, OpsHubError
from opshub.llm import LLMClient, LLMMessage, LLMResponse, StructuredResponse

# ---- helpers --------------------------------------------------------------


def _tags_response() -> httpx.Response:
    """Build a typical ``GET /api/tags`` response body for the probe."""
    return httpx.Response(200, json={"models": [{"name": "llama3.2:3b"}]})


def _completion_response(
    *,
    text: str = "hello",
    prompt_tokens: int = 15,
    completion_tokens: int = 25,
    finish_reason: str = "stop",
) -> httpx.Response:
    """Build an OpenAI-shape chat-completions response body."""
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-xxx",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish_reason,
                    "message": {"role": "assistant", "content": text},
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        },
    )


def _tool_call_response(
    *,
    arguments: str,
    prompt_tokens: int = 10,
    completion_tokens: int = 20,
    tool_name: str = "echo",
    finish_reason: str = "tool_calls",
) -> httpx.Response:
    """Build an OpenAI-shape response with one ``tool_calls`` entry."""
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-xxx",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish_reason,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": arguments,
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        },
    )


def _make_client_with_handler(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
    **kwargs: Any,
) -> tuple[Any, list[httpx.Request]]:
    """Build an ``OllamaLLMClient`` whose ``httpx.Client`` uses ``handler``.

    Returns the constructed client plus a recording list of every
    request sent through the mock transport. Construction triggers the
    ``GET /api/tags`` probe, so ``handler`` must answer that route.
    """
    from opshub.llm.ollama_client import OllamaLLMClient

    requests: list[httpx.Request] = []

    def _recorded(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response: httpx.Response = handler(request)
        return response

    real_client_cls = httpx.Client

    def _factory(*args: Any, **client_kwargs: Any) -> httpx.Client:
        client_kwargs.pop("transport", None)
        return real_client_cls(  # pyright: ignore[reportUnknownVariableType]
            *args,
            transport=httpx.MockTransport(_recorded),
            **client_kwargs,
        )

    monkeypatch.setattr("httpx.Client", _factory)
    # ``OllamaLLMClient.__init__`` stashes the ``httpx`` module on the
    # instance for error-class references; we leave that module-level
    # import alone so ``httpx.HTTPStatusError`` etc. resolve normally.
    client = OllamaLLMClient(**kwargs)
    return client, requests


# ---- import-time / module surface ----------------------------------------


def test_module_imports_without_extras_marker() -> None:
    """Importing the module must succeed even before httpx is in scope.

    Strictly speaking, the ``importorskip`` at the top of this file
    already proves httpx is installed in this CI lane. This test pins
    the *module*'s import-time discipline: the ``httpx`` symbol is NOT
    pulled into the module's globals at import time (it's imported
    lazily inside ``__init__``), so the cold-start guard
    (``tests/integration/test_cli_imports.py``) stays unbroken.
    """
    from opshub.llm import ollama_client as ollama_module

    module_globals = set(vars(ollama_module).keys())
    assert "httpx" not in module_globals, (
        "ollama_client module exposes 'httpx' at import time; lazy import discipline broken"
    )


# ---- constructor / probe -------------------------------------------------


def test_init_probes_daemon_via_tags_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Construction must issue ``GET /api/tags`` before returning."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/tags":
            return _tags_response()
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    _, requests = _make_client_with_handler(monkeypatch, _handler)

    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/api/tags"


def test_init_raises_config_error_when_daemon_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe failure → ``ConfigError`` with actionable hint (ADR-0016 §決定 (h))."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    real_client_cls = httpx.Client

    def _factory(*args: Any, **client_kwargs: Any) -> httpx.Client:
        client_kwargs.pop("transport", None)
        return real_client_cls(  # pyright: ignore[reportUnknownVariableType]
            *args,
            transport=httpx.MockTransport(_handler),
            **client_kwargs,
        )

    monkeypatch.setattr("httpx.Client", _factory)
    from opshub.llm.ollama_client import OllamaLLMClient

    with pytest.raises(ConfigError) as excinfo:
        OllamaLLMClient(host="http://127.0.0.1:11434", model_id="llama3.2:3b")
    message = str(excinfo.value)
    assert "Ollama daemon not reachable" in message
    assert "127.0.0.1:11434" in message
    assert "ollama" in message.lower()
    assert "llama3.2:3b" in message


def test_init_strips_trailing_slash_from_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """``host="http://localhost:11434/"`` should not produce ``//api/tags``."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/tags":
            return _tags_response()
        raise AssertionError(f"unexpected request path: {request.url.path}")

    client, _ = _make_client_with_handler(monkeypatch, _handler, host="http://localhost:11434/")
    # Recorded property, no leading-slash drift.
    assert client.model_id == "llama3.2:3b"


# ---- properties / protocol -----------------------------------------------


def test_model_id_and_version_properties(monkeypatch: pytest.MonkeyPatch) -> None:
    def _handler(_request: httpx.Request) -> httpx.Response:
        return _tags_response()

    client, _ = _make_client_with_handler(
        monkeypatch,
        _handler,
        model_id="mistral:7b",
        model_version="ollama-2026",
    )
    assert client.model_id == "mistral:7b"
    assert client.model_version == "ollama-2026"


def test_defaults_match_adr_0016(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defaults pinned by ADR-0016 §決定 (h) / Phase 6 plan §2.1."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        return _tags_response()

    client, _ = _make_client_with_handler(monkeypatch, _handler)
    assert client.model_id == "llama3.2:3b"
    assert client.model_version  # non-empty truthy string


def test_satisfies_llm_client_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    """``OllamaLLMClient`` must satisfy ``isinstance(..., LLMClient)``.

    ``LLMClient`` is ``@runtime_checkable``; this test fails loudly if
    someone renames a method or drops a property, catching drift that
    escapes the Phase 6 freeze test.
    """

    def _handler(_request: httpx.Request) -> httpx.Response:
        return _tags_response()

    client, _ = _make_client_with_handler(monkeypatch, _handler)
    assert isinstance(client, LLMClient)


# ---- complete: messages / return shape -----------------------------------


def test_complete_passes_messages_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    """system / user / assistant all forward to the OpenAI-compat
    ``messages=`` array verbatim — Ollama's OpenAI-compat layer
    accepts the same wire shape as OpenAI (no system-role split)."""
    posts: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/tags":
            return _tags_response()
        if request.method == "POST" and request.url.path == "/v1/chat/completions":
            import json as _json

            posts.append(_json.loads(request.content))
            return _completion_response()
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client, _ = _make_client_with_handler(monkeypatch, _handler)
    client.complete(
        [
            LLMMessage(role="system", content="you are helpful"),
            LLMMessage(role="user", content="hi"),
            LLMMessage(role="assistant", content="hello"),
            LLMMessage(role="user", content="more"),
        ],
        max_tokens=200,
    )

    assert len(posts) == 1
    sent = posts[0]
    assert sent["messages"] == [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "more"},
    ]
    assert sent["model"] == "llama3.2:3b"
    assert sent["max_tokens"] == 200
    assert sent["temperature"] == 0.2  # Protocol default


def test_complete_returns_llm_response_with_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """Token counts map from ``response.usage`` to LLMResponse fields."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/tags":
            return _tags_response()
        return _completion_response(text="hello", prompt_tokens=15, completion_tokens=25)

    client, _ = _make_client_with_handler(
        monkeypatch, _handler, model_id="llama3.2:3b", model_version="ollama-2026"
    )
    response = client.complete([LLMMessage(role="user", content="hi")], max_tokens=100)

    assert isinstance(response, LLMResponse)
    assert response.text == "hello"
    assert response.model_id == "llama3.2:3b"
    assert response.model_version == "ollama-2026"
    assert response.tokens_in == 15
    assert response.tokens_out == 25


def test_complete_does_not_send_auth_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local daemon is unauthenticated; no ``Authorization`` header allowed."""
    captured_headers: list[httpx.Headers] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/tags":
            return _tags_response()
        captured_headers.append(request.headers)
        return _completion_response()

    client, _ = _make_client_with_handler(monkeypatch, _handler)
    client.complete([LLMMessage(role="user", content="hi")], max_tokens=10)

    assert len(captured_headers) == 1
    headers = captured_headers[0]
    # httpx.Headers is case-insensitive.
    assert "authorization" not in headers
    assert "x-api-key" not in headers


def test_complete_omits_stop_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """``stop=None`` is not forwarded as a literal kwarg."""
    posts: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/tags":
            return _tags_response()
        import json as _json

        posts.append(_json.loads(request.content))
        return _completion_response()

    client, _ = _make_client_with_handler(monkeypatch, _handler)
    client.complete([LLMMessage(role="user", content="hi")], max_tokens=10)
    assert "stop" not in posts[0]


def test_complete_passes_stop_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit ``stop=[...]`` forwards verbatim to the payload."""
    posts: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/tags":
            return _tags_response()
        import json as _json

        posts.append(_json.loads(request.content))
        return _completion_response()

    client, _ = _make_client_with_handler(monkeypatch, _handler)
    client.complete(
        [LLMMessage(role="user", content="hi")],
        max_tokens=10,
        stop=["END", "STOP"],
    )
    assert posts[0]["stop"] == ["END", "STOP"]


# ---- complete: error paths -----------------------------------------------


def test_complete_raises_opshub_error_on_http_status_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4xx / 5xx daemon response → :class:`OpsHubError`."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/tags":
            return _tags_response()
        return httpx.Response(500, text="model not loaded")

    client, _ = _make_client_with_handler(monkeypatch, _handler)
    with pytest.raises(OpsHubError) as excinfo:
        client.complete([LLMMessage(role="user", content="hi")], max_tokens=10)
    message = str(excinfo.value)
    assert "Ollama API error" in message
    assert "500" in message


def test_complete_raises_opshub_error_on_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Network failure mid-request → :class:`OpsHubError`."""
    call_count = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/tags":
            return _tags_response()
        call_count["n"] += 1
        raise httpx.ConnectError("network unreachable")

    client, _ = _make_client_with_handler(monkeypatch, _handler)
    with pytest.raises(OpsHubError) as excinfo:
        client.complete([LLMMessage(role="user", content="hi")], max_tokens=10)
    assert "Ollama daemon connection lost" in str(excinfo.value)


def test_complete_raises_opshub_error_on_empty_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty / ``null`` content must surface as :class:`OpsHubError`."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/tags":
            return _tags_response()
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": ""},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 0},
            },
        )

    client, _ = _make_client_with_handler(monkeypatch, _handler)
    with pytest.raises(OpsHubError) as excinfo:
        client.complete([LLMMessage(role="user", content="hi")], max_tokens=10)
    assert "empty content" in str(excinfo.value)


def test_complete_handles_missing_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing ``usage`` block → tokens default to 0 (not a crash)."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/tags":
            return _tags_response()
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ]
            },
        )

    client, _ = _make_client_with_handler(monkeypatch, _handler)
    response = client.complete([LLMMessage(role="user", content="hi")], max_tokens=10)
    assert response.text == "ok"
    assert response.tokens_in == 0
    assert response.tokens_out == 0


# ---- complete_structured -------------------------------------------------


class _EchoSchema(BaseModel):
    """Trivial structured schema used to exercise tool_use round-trips."""

    x: int


def test_complete_structured_passes_function_tool_with_strict_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Request includes ``tools=[{type: function, function: {..., strict: True}}]``
    and ``tool_choice={type: function, function: {name: ...}}``.
    """
    posts: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/tags":
            return _tags_response()
        import json as _json

        posts.append(_json.loads(request.content))
        return _tool_call_response(arguments='{"x": 42}', tool_name="__echo_schema")

    client, _ = _make_client_with_handler(monkeypatch, _handler)
    client.complete_structured(
        [LLMMessage(role="user", content="echo 42")],
        schema=_EchoSchema,
        max_tokens=100,
    )

    assert len(posts) == 1
    sent = posts[0]
    assert sent["tools"][0]["type"] == "function"
    func = sent["tools"][0]["function"]
    assert func["name"] == "__echo_schema"
    assert func["strict"] is True
    assert "parameters" in func
    assert sent["tool_choice"] == {"type": "function", "function": {"name": "__echo_schema"}}


def test_complete_structured_parses_tool_calls_arguments_to_pydantic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``tool_calls[0].function.arguments`` JSON parses + Pydantic validates."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/tags":
            return _tags_response()
        return _tool_call_response(
            arguments='{"x": 42}',
            tool_name="__echo_schema",
            prompt_tokens=11,
            completion_tokens=22,
        )

    client, _ = _make_client_with_handler(monkeypatch, _handler)
    response = client.complete_structured(
        [LLMMessage(role="user", content="echo 42")],
        schema=_EchoSchema,
        max_tokens=100,
    )

    assert isinstance(response, StructuredResponse)
    # ``client`` is ``Any`` (the mock factory drops type info), so
    # ``response.parsed`` arrives as ``Unknown`` to pyright. ``isinstance``
    # narrows it explicitly without needing a manual type assignment.
    parsed_obj = response.parsed  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    assert isinstance(parsed_obj, _EchoSchema)
    assert parsed_obj.x == 42
    assert response.tokens_in == 11
    assert response.tokens_out == 22
    assert response.model_id == "llama3.2:3b"


def test_complete_structured_raises_when_no_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Free-text response (no tool_calls) → :class:`OpsHubError`."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/tags":
            return _tags_response()
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "I refuse to call the tool.",
                        },
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 10},
            },
        )

    client, _ = _make_client_with_handler(monkeypatch, _handler)
    with pytest.raises(OpsHubError) as excinfo:
        client.complete_structured(
            [LLMMessage(role="user", content="hi")],
            schema=_EchoSchema,
            max_tokens=100,
        )
    assert "no tool_calls" in str(excinfo.value)


def test_complete_structured_raises_when_arguments_not_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed arguments string → :class:`OpsHubError`."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/tags":
            return _tags_response()
        return _tool_call_response(arguments="{not json", tool_name="__echo_schema")

    client, _ = _make_client_with_handler(monkeypatch, _handler)
    with pytest.raises(OpsHubError) as excinfo:
        client.complete_structured(
            [LLMMessage(role="user", content="hi")],
            schema=_EchoSchema,
            max_tokens=100,
        )
    assert "not valid JSON" in str(excinfo.value)


def test_complete_structured_raises_when_arguments_fail_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid JSON but wrong shape → :class:`OpsHubError` mentions Pydantic."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/tags":
            return _tags_response()
        # ``x`` should be int; the model returns a string.
        return _tool_call_response(arguments='{"x": "not-an-int-string"}')

    client, _ = _make_client_with_handler(monkeypatch, _handler)
    with pytest.raises(OpsHubError) as excinfo:
        client.complete_structured(
            [LLMMessage(role="user", content="hi")],
            schema=_EchoSchema,
            max_tokens=100,
        )
    assert "Pydantic" in str(excinfo.value)
    assert "_EchoSchema" in str(excinfo.value)


def test_complete_structured_does_not_send_auth_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same no-auth contract as :meth:`complete` (ADR-0016 §決定 (h))."""
    captured_headers: list[httpx.Headers] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/tags":
            return _tags_response()
        captured_headers.append(request.headers)
        return _tool_call_response(arguments='{"x": 1}')

    client, _ = _make_client_with_handler(monkeypatch, _handler)
    client.complete_structured(
        [LLMMessage(role="user", content="hi")],
        schema=_EchoSchema,
        max_tokens=10,
    )
    assert len(captured_headers) == 1
    assert "authorization" not in captured_headers[0]


# ---- missing extras -------------------------------------------------------


def test_missing_extras_raises_actionable_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``httpx`` is not installed, surface a ``ConfigError`` that names
    the ``llm-ollama`` extras explicitly so the user fixes it in one step.
    """
    import builtins

    real_import = builtins.__import__

    def _fail_httpx(
        name: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if name == "httpx":
            raise ImportError("No module named 'httpx'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fail_httpx)

    from opshub.llm.ollama_client import OllamaLLMClient

    with pytest.raises(ConfigError) as excinfo:
        OllamaLLMClient()
    message = str(excinfo.value)
    assert "llm-ollama" in message
    assert "opshub[llm-ollama]" in message


# ---- factory integration -------------------------------------------------


def test_factory_build_llm_client_returns_ollama_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The factory must dispatch ``backend="ollama"`` to OllamaLLMClient."""
    from opshub.core.config import LLMSettings, OllamaLLMSettings, OpsHubSettings
    from opshub.llm.factory import build_llm_client
    from opshub.llm.ollama_client import OllamaLLMClient

    def _handler(_request: httpx.Request) -> httpx.Response:
        return _tags_response()

    real_client_cls = httpx.Client

    def _factory(*args: Any, **client_kwargs: Any) -> httpx.Client:
        client_kwargs.pop("transport", None)
        return real_client_cls(  # pyright: ignore[reportUnknownVariableType]
            *args,
            transport=httpx.MockTransport(_handler),
            **client_kwargs,
        )

    monkeypatch.setattr("httpx.Client", _factory)

    settings = OpsHubSettings(
        llm=LLMSettings(
            backend="ollama",
            ollama=OllamaLLMSettings(
                model_id="llama3.2:3b",
                model_version="ollama",
                host="http://localhost:11434",
                timeout_seconds=30.0,
            ),
        )
    )
    client = build_llm_client(settings)
    assert isinstance(client, OllamaLLMClient)
    assert client.model_id == "llama3.2:3b"
    assert client.model_version == "ollama"


def test_factory_respects_ollama_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-field overrides flow through the factory verbatim."""
    from opshub.core.config import LLMSettings, OllamaLLMSettings, OpsHubSettings
    from opshub.llm.factory import build_llm_client
    from opshub.llm.ollama_client import OllamaLLMClient

    def _handler(_request: httpx.Request) -> httpx.Response:
        return _tags_response()

    real_client_cls = httpx.Client

    def _factory(*args: Any, **client_kwargs: Any) -> httpx.Client:
        client_kwargs.pop("transport", None)
        return real_client_cls(  # pyright: ignore[reportUnknownVariableType]
            *args,
            transport=httpx.MockTransport(_handler),
            **client_kwargs,
        )

    monkeypatch.setattr("httpx.Client", _factory)

    settings = OpsHubSettings(
        llm=LLMSettings(
            backend="ollama",
            ollama=OllamaLLMSettings(
                model_id="mistral:7b",
                model_version="ollama-2026",
                host="http://localhost:11434",
                timeout_seconds=90.0,
            ),
        )
    )
    client = build_llm_client(settings)
    assert isinstance(client, OllamaLLMClient)
    assert client.model_id == "mistral:7b"
    assert client.model_version == "ollama-2026"
