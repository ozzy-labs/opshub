"""Tests for :mod:`opshub.llm.factory` (Phase 5 step A5, ADR-0015).

The factory module is lightweight (no SDK imports at module level), so
its disabled-path / unknown-backend / sentinel-properties tests run on
every CI lane. Per-backend tests that materialise the concrete client
gate on the matching extras via :func:`pytest.importorskip`:

- ``test_build_llm_client_returns_anthropic_when_configured`` requires
  ``anthropic`` (``[llm-anthropic]``).
- ``test_build_llm_client_returns_openai_when_configured`` requires
  ``openai`` (``[llm-openai]``).

Mirrors the Phase 4 ``tests/unit/vectors/test_factory.py`` precedent in
both structure and naming.
"""

from __future__ import annotations

from typing import Any

import pytest

from opshub.core.config import (
    AnthropicLLMSettings,
    LLMSettings,
    OpenAILLMSettings,
    OpsHubSettings,
)
from opshub.core.errors import ConfigError
from opshub.llm import LLMClient, LLMMessage
from opshub.llm.factory import NoOpLLMClient, build_llm_client


def _make_settings(**llm_kwargs: Any) -> OpsHubSettings:
    """Build an :class:`OpsHubSettings` with a specific ``llm`` section.

    Keeps each test focused on the LLM fields under test without
    repeating the boilerplate of constructing the full settings tree.
    Mirrors the ``_make_settings`` helper in
    ``tests/unit/vectors/test_factory.py``.
    """
    return OpsHubSettings(llm=LLMSettings(**llm_kwargs))


# ---- disabled backend / NoOpLLMClient -----------------------------------


def test_build_llm_client_returns_noop_when_disabled() -> None:
    settings = _make_settings(backend="disabled")
    client = build_llm_client(settings)
    assert isinstance(client, NoOpLLMClient)


def test_noop_client_properties_have_sentinels() -> None:
    """``NoOpLLMClient`` exposes sentinel identity so future ``opshub llm
    status`` style commands can introspect it like any other
    :class:`LLMClient`."""
    client = NoOpLLMClient()
    assert client.model_id == "disabled"
    assert client.model_version == "disabled"


def test_noop_client_satisfies_protocol() -> None:
    """The Protocol is ``@runtime_checkable``; the sentinel must satisfy it."""
    assert isinstance(NoOpLLMClient(), LLMClient)


def test_noop_client_complete_raises_config_error() -> None:
    """The disabled path must fail loud, not return empty markdown.

    ADR-0015 §決定 (b) calls this out explicitly: callers (BriefingService)
    cannot accidentally persist a ``BriefingGenerated`` event with empty
    text because the NoOp client refuses to run.
    """
    client = NoOpLLMClient()
    messages = [LLMMessage(role="user", content="hello")]
    with pytest.raises(ConfigError) as exc_info:
        client.complete(messages, max_tokens=100)
    message = str(exc_info.value)
    # Operator-facing message must point at the config knob and the
    # auth set command, otherwise "backend is disabled" is dead-end.
    assert "backend" in message
    assert "disabled" in message
    assert "anthropic" in message
    assert "openai" in message
    assert "OPSHUB_LLM_BACKEND" in message
    assert "opshub llm auth set" in message


def test_noop_client_complete_passes_protocol_kwargs() -> None:
    """``complete`` accepts the full Protocol signature including
    ``temperature`` / ``stop`` — the kwargs are ignored but must not
    cause a TypeError when callers (BriefingService) pass them.
    """
    client = NoOpLLMClient()
    with pytest.raises(ConfigError):
        client.complete(
            [LLMMessage(role="user", content="x")],
            max_tokens=10,
            temperature=0.5,
            stop=["END"],
        )


# ---- unknown backend ----------------------------------------------------


def test_build_llm_client_unknown_backend_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a future ``LLMBackend`` literal grows a new value without a
    matching factory branch, operators get a ``ConfigError`` listing
    every supported backend instead of silently falling off the
    ``if/elif`` chain.
    """
    settings = _make_settings(backend="disabled")
    # Bypass the Pydantic Literal guard by mutating the resolved
    # field — simulates the "Literal grew, factory didn't" regression.
    monkeypatch.setattr(settings.llm, "backend", "grok", raising=False)
    with pytest.raises(ConfigError) as exc_info:
        build_llm_client(settings)
    message = str(exc_info.value)
    assert "grok" in message
    # Every known literal should appear in the error so the operator
    # sees the supported set without grepping the source.
    assert "disabled" in message
    assert "anthropic" in message
    assert "openai" in message
    assert "ollama" in message


# ---- factory module is lazy --------------------------------------------


def test_factory_module_does_not_import_heavy_deps() -> None:
    """Importing :mod:`opshub.llm.factory` must NOT pull ``anthropic`` /
    ``openai`` SDKs into the factory module's globals. Those imports
    live inside each backend branch so the cold-start path stays fast
    (ADR-0001 §3, M6 guard). Mirrors the Phase 4
    :func:`tests.unit.vectors.test_factory.test_factory_module_does_not_import_heavy_deps`
    precedent.

    Heavy deps may already be in ``sys.modules`` if some other test
    imported them earlier (e.g. the Anthropic client tests pull in
    ``anthropic``). The guard we care about is the *factory module's
    own globals* — those must not reference the heavy SDKs even after
    import.
    """
    from opshub.llm import factory as factory_module

    factory_globals = set(vars(factory_module).keys())
    for heavy_name in ("anthropic", "openai", "httpx"):
        assert heavy_name not in factory_globals, (
            f"factory module exposes {heavy_name!r}; lazy import discipline broken"
        )


# ---- anthropic backend (gated) -----------------------------------------


def test_build_llm_client_returns_anthropic_when_configured() -> None:
    pytest.importorskip(
        "anthropic",
        reason="anthropic factory branch requires the 'llm-anthropic' extras",
    )
    from opshub.llm.anthropic_client import AnthropicLLMClient

    settings = _make_settings(backend="anthropic")
    client = build_llm_client(settings)
    assert isinstance(client, AnthropicLLMClient)
    # The default model id / version flow through from
    # ``AnthropicLLMSettings`` (ADR-0015 §決定 (c)).
    assert client.model_id == "claude-haiku-4-5-20251001"
    assert client.model_version == "2026-05-01"


def test_build_llm_client_respects_anthropic_overrides() -> None:
    pytest.importorskip(
        "anthropic",
        reason="anthropic factory branch requires the 'llm-anthropic' extras",
    )
    from opshub.llm.anthropic_client import AnthropicLLMClient

    settings = _make_settings(
        backend="anthropic",
        anthropic=AnthropicLLMSettings(
            model_id="claude-sonnet-4-5-20251001",
            model_version="2026-06-01",
        ),
    )
    client = build_llm_client(settings)
    assert isinstance(client, AnthropicLLMClient)
    assert client.model_id == "claude-sonnet-4-5-20251001"
    assert client.model_version == "2026-06-01"


# ---- openai backend (gated) --------------------------------------------


def test_build_llm_client_returns_openai_when_configured() -> None:
    pytest.importorskip(
        "openai",
        reason="openai factory branch requires the 'llm-openai' extras",
    )
    from opshub.llm.openai_client import OpenAILLMClient

    settings = _make_settings(backend="openai")
    client = build_llm_client(settings)
    assert isinstance(client, OpenAILLMClient)
    # Default model id / version per ADR-0015 §決定 (c).
    assert client.model_id == "gpt-4o-mini"
    assert client.model_version == "2026-05-01"


def test_build_llm_client_respects_openai_overrides() -> None:
    pytest.importorskip(
        "openai",
        reason="openai factory branch requires the 'llm-openai' extras",
    )
    from opshub.llm.openai_client import OpenAILLMClient

    settings = _make_settings(
        backend="openai",
        openai=OpenAILLMSettings(
            model_id="gpt-4o",
            model_version="2026-06-01",
        ),
    )
    client = build_llm_client(settings)
    assert isinstance(client, OpenAILLMClient)
    assert client.model_id == "gpt-4o"
    assert client.model_version == "2026-06-01"
