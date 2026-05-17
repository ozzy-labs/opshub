"""Tests for ``opshub.llm.openai_client`` (Phase 5 step A4).

The ``openai`` SDK is in the ``[llm-openai]`` extras and may not be
installed in every CI lane; ``pytest.importorskip`` at module load gates
the entire file the same way the Phase 4 OpenAI embedder tests gate on
their extras.

The OpenAI SDK is always mocked — no real HTTP request leaves the
process. The patch target is ``openai.OpenAI`` because the client does a
lazy ``__import__("openai")`` inside ``_ensure_client``; once that
import succeeds the ``OpenAI`` symbol resolves through the package and
``patch`` can redirect it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip(
    "openai",
    reason="opshub.llm.openai_client tests require the 'llm-openai' extras",
)

from opshub.core.errors import ConfigError, OpsHubError
from opshub.llm import LLMClient, LLMMessage, LLMResponse
from opshub.llm.openai_client import OPENAI_API_KEY_SECRET, OpenAILLMClient

# ---- helpers --------------------------------------------------------------


def _fake_completion(
    *,
    text: str = "hello",
    prompt_tokens: int = 15,
    completion_tokens: int = 25,
    finish_reason: str = "stop",
) -> MagicMock:
    """Build a MagicMock shaped like ``openai.types.ChatCompletion``.

    The client reads ``response.choices[0].message.content`` and
    ``response.usage.prompt_tokens`` / ``completion_tokens`` so we mirror
    those access chains exactly. Using MagicMock per node keeps the test
    decoupled from the SDK's pydantic response types.
    """
    response = MagicMock()
    choice = MagicMock()
    choice.message = MagicMock(content=text)
    choice.finish_reason = finish_reason
    response.choices = [choice]
    response.usage = MagicMock(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
    return response


def _patch_get_secret(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    """Force ``opshub.core.secrets.get_secret`` to return ``value``.

    The client imports ``get_secret`` lazily inside ``_ensure_client``;
    patching on the source module (the canonical location) works
    regardless of import order.
    """

    def _stub(_key: str) -> str | None:
        return value

    monkeypatch.setattr("opshub.core.secrets.get_secret", _stub)


# ---- constants ------------------------------------------------------------


def test_openai_llm_api_key_secret_constant() -> None:
    """Public contract: CLI writer and client reader must agree on this key."""
    assert OPENAI_API_KEY_SECRET == "llm:openai:api_key"


# ---- properties -----------------------------------------------------------


def test_model_id_and_version_properties() -> None:
    """Constructor args surface verbatim through the LLMClient properties."""
    client = OpenAILLMClient(model_id="gpt-4o", model_version="2026-09-01")
    assert client.model_id == "gpt-4o"
    assert client.model_version == "2026-09-01"


def test_defaults_match_adr_0015() -> None:
    """Defaults are pinned by ADR-0015 §決定 (c) (推奨モデル)."""
    client = OpenAILLMClient()
    assert client.model_id == "gpt-4o-mini"
    # Stable version tag — we don't pin the exact string here, but it
    # must be non-empty (the LLMResponse contract requires a value).
    assert client.model_version  # non-empty truthy string


# ---- protocol conformance -------------------------------------------------


def test_satisfies_llm_client_protocol() -> None:
    """``OpenAILLMClient`` must satisfy ``isinstance(..., LLMClient)``.

    ``LLMClient`` is ``@runtime_checkable`` (ADR-0015 §決定 (a)). This
    test fails loudly if someone renames a method or drops a property,
    catching drift that escapes the Phase 5 A2 freeze test.
    """
    assert isinstance(OpenAILLMClient(), LLMClient)


# ---- complete: messages forwarded directly --------------------------------


def test_complete_passes_messages_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    """system / user / assistant all forward to the OpenAI ``messages=``
    array verbatim — unlike Anthropic, there is no ``system=`` separation."""
    monkeypatch.setenv("OPSHUB_LLM_OPENAI_API_KEY", "sk-test")

    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _fake_completion()
    with patch("openai.OpenAI", return_value=fake_client):
        client = OpenAILLMClient()
        client.complete(
            [
                LLMMessage(role="system", content="you are helpful"),
                LLMMessage(role="user", content="hi"),
                LLMMessage(role="assistant", content="hello"),
                LLMMessage(role="user", content="more"),
            ],
            max_tokens=200,
        )

    call = fake_client.chat.completions.create.call_args
    sent_messages = call.kwargs["messages"]
    assert sent_messages == [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "more"},
    ]
    # The model + budget kwargs travel through verbatim.
    assert call.kwargs["model"] == "gpt-4o-mini"
    assert call.kwargs["max_tokens"] == 200
    assert call.kwargs["temperature"] == 0.2  # Protocol default


# ---- complete: return shape -----------------------------------------------


def test_complete_returns_llm_response_with_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token counts map from ``response.usage`` to LLMResponse fields
    (ADR-0015 §決定 (g) — operational visibility into per-briefing cost)."""
    monkeypatch.setenv("OPSHUB_LLM_OPENAI_API_KEY", "sk-test")

    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _fake_completion(
        text="hello",
        prompt_tokens=15,
        completion_tokens=25,
    )
    with patch("openai.OpenAI", return_value=fake_client):
        client = OpenAILLMClient(model_id="gpt-4o-mini", model_version="2026-05-01")
        response = client.complete(
            [LLMMessage(role="user", content="hi")],
            max_tokens=100,
        )

    assert isinstance(response, LLMResponse)
    assert response.text == "hello"
    assert response.model_id == "gpt-4o-mini"
    assert response.model_version == "2026-05-01"
    assert response.tokens_in == 15
    assert response.tokens_out == 25


# ---- complete: stop kwarg handling ----------------------------------------


def test_complete_omits_stop_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """``stop=None`` must NOT be forwarded as a literal ``None`` kwarg.

    OpenAI accepts both forms but omitting is cleaner in mocks / traces
    and matches ADR-0015 §決定 (h) (caller-driven, no implicit defaults
    leaking into the SDK call)."""
    monkeypatch.setenv("OPSHUB_LLM_OPENAI_API_KEY", "sk-test")

    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _fake_completion()
    with patch("openai.OpenAI", return_value=fake_client):
        client = OpenAILLMClient()
        client.complete([LLMMessage(role="user", content="hi")], max_tokens=10)

    call = fake_client.chat.completions.create.call_args
    assert "stop" not in call.kwargs


def test_complete_passes_stop_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit ``stop=[...]`` forwards verbatim to the SDK."""
    monkeypatch.setenv("OPSHUB_LLM_OPENAI_API_KEY", "sk-test")

    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _fake_completion()
    with patch("openai.OpenAI", return_value=fake_client):
        client = OpenAILLMClient()
        client.complete(
            [LLMMessage(role="user", content="hi")],
            max_tokens=10,
            stop=["END", "STOP"],
        )

    call = fake_client.chat.completions.create.call_args
    assert call.kwargs["stop"] == ["END", "STOP"]


# ---- credential resolution ------------------------------------------------


def test_api_key_resolved_from_secrets_when_not_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No constructor key + keyring returns a value → that value reaches the SDK.

    Mirrors the Phase 4 embedder credential test."""
    monkeypatch.delenv("OPSHUB_LLM_OPENAI_API_KEY", raising=False)
    _patch_get_secret(monkeypatch, "sk-from-keyring")

    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _fake_completion()
    with patch("openai.OpenAI", return_value=fake_client) as openai_cls:
        OpenAILLMClient().complete([LLMMessage(role="user", content="hi")], max_tokens=10)

    openai_cls.assert_called_once_with(api_key="sk-from-keyring")


def test_api_key_env_var_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """``OPSHUB_LLM_OPENAI_API_KEY`` is the documented CI / docker / WSL2
    escape hatch (ADR-0014 + ADR-0015 §決定 (d)). The env var must reach
    the SDK without keyring involvement.

    The stub asserts the requested key matches the published constant so
    a typo on the keyring-side fails this test loudly."""
    monkeypatch.setenv("OPSHUB_LLM_OPENAI_API_KEY", "sk-from-env")

    import os

    def _stub(key: str) -> str | None:
        assert key == OPENAI_API_KEY_SECRET
        # Production ``get_secret`` consults the env var override before
        # touching keyring; the stub mirrors that contract while
        # guaranteeing no real keyring call happens in CI.
        return os.environ.get("OPSHUB_LLM_OPENAI_API_KEY")

    monkeypatch.setattr("opshub.core.secrets.get_secret", _stub)

    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _fake_completion()
    with patch("openai.OpenAI", return_value=fake_client) as openai_cls:
        OpenAILLMClient().complete([LLMMessage(role="user", content="hi")], max_tokens=10)

    openai_cls.assert_called_once_with(api_key="sk-from-env")


def test_explicit_constructor_api_key_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit ``api_key=`` constructor arg bypasses keyring / env entirely.

    This is the documented test-injection path; nothing else in the
    process should be consulted when the caller already knows the key."""
    monkeypatch.delenv("OPSHUB_LLM_OPENAI_API_KEY", raising=False)

    def _fail(_key: str) -> str | None:
        raise AssertionError("get_secret must not be called when api_key is explicit")

    monkeypatch.setattr("opshub.core.secrets.get_secret", _fail)

    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _fake_completion()
    with patch("openai.OpenAI", return_value=fake_client) as openai_cls:
        OpenAILLMClient(api_key="sk-explicit").complete(
            [LLMMessage(role="user", content="hi")], max_tokens=10
        )

    openai_cls.assert_called_once_with(api_key="sk-explicit")


def test_missing_api_key_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty keyring + no env var + no constructor arg → actionable
    ``ConfigError`` that points the user at the documented CLI command +
    env var override (ADR-0015 §決定 (d))."""
    monkeypatch.delenv("OPSHUB_LLM_OPENAI_API_KEY", raising=False)
    _patch_get_secret(monkeypatch, None)

    with (
        patch("openai.OpenAI") as openai_cls,
        pytest.raises(ConfigError) as excinfo,
    ):
        OpenAILLMClient().complete([LLMMessage(role="user", content="hi")], max_tokens=10)

    openai_cls.assert_not_called()
    message = str(excinfo.value)
    assert "opshub connector auth set llm:openai" in message
    assert "OPSHUB_LLM_OPENAI_API_KEY" in message


# ---- empty / malformed response ------------------------------------------


@pytest.mark.parametrize("bad_content", [None, ""])
def test_empty_response_raises(monkeypatch: pytest.MonkeyPatch, bad_content: Any) -> None:
    """``choices[0].message.content`` being ``None`` or empty must raise
    ``OpsHubError`` so BriefingService records ``BriefingFailed`` rather
    than silently persisting an empty markdown briefing."""
    monkeypatch.setenv("OPSHUB_LLM_OPENAI_API_KEY", "sk-test")

    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _fake_completion(text=bad_content)
    with (
        patch("openai.OpenAI", return_value=fake_client),
        pytest.raises(OpsHubError) as excinfo,
    ):
        OpenAILLMClient().complete([LLMMessage(role="user", content="hi")], max_tokens=10)

    assert "empty content" in str(excinfo.value)


# ---- missing extras -------------------------------------------------------


def test_missing_extras_raises_actionable_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the ``openai`` SDK is not installed, surface a ConfigError that
    names the ``llm-openai`` extras explicitly so the user fixes it in
    one step."""
    monkeypatch.setenv("OPSHUB_LLM_OPENAI_API_KEY", "sk-test")

    import builtins

    real_import = builtins.__import__

    def _fail_openai(
        name: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if name == "openai":
            raise ImportError("No module named 'openai'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fail_openai)

    with pytest.raises(ConfigError) as excinfo:
        OpenAILLMClient().complete([LLMMessage(role="user", content="hi")], max_tokens=10)

    message = str(excinfo.value)
    assert "llm-openai" in message
    assert "opshub[llm-openai]" in message


# ---- client caching -------------------------------------------------------


def test_client_is_constructed_once_across_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK client is expensive to instantiate; the LLM client caches
    it for the lifetime of the instance (mirrors the Phase 4 embedder)."""
    monkeypatch.setenv("OPSHUB_LLM_OPENAI_API_KEY", "sk-test")

    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _fake_completion()
    with patch("openai.OpenAI", return_value=fake_client) as openai_cls:
        client = OpenAILLMClient()
        client.complete([LLMMessage(role="user", content="a")], max_tokens=10)
        client.complete([LLMMessage(role="user", content="b")], max_tokens=10)
        client.complete([LLMMessage(role="user", content="c")], max_tokens=10)

    assert openai_cls.call_count == 1
