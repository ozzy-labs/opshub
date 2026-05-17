"""Freeze tests for the Phase 5 LLM interface.

These tests assert the *exact* shape of :class:`opshub.llm.LLMClient`,
:class:`opshub.llm.LLMMessage`, and :class:`opshub.llm.LLMResponse`. If
anyone changes a method name, renames a parameter, alters a type
annotation, or flips a kw-only marker, these tests fail loudly — that is
the entire point.

Rationale (ADR-0015 §決定 + Phase 5 plan §1.1): config (step A5),
BriefingService (step B3), and the ``opshub brief`` CLI (step B4) all
start referencing the Protocol by name in early Phase 5 PRs. Allowing it
to drift before the concrete Anthropic / OpenAI clients land would defeat
the freeze.
"""

from __future__ import annotations

import dataclasses
import inspect
from typing import Literal, Protocol, get_type_hints

import pytest

from opshub.llm import LLMClient, LLMMessage, LLMResponse

# ---- Protocol identity ----------------------------------------------------


def test_llm_client_is_runtime_checkable_protocol() -> None:
    assert issubclass(LLMClient, Protocol)  # type: ignore[arg-type]
    # runtime_checkable sets this dunder; assert it explicitly so dropping
    # the decorator fails the freeze test.
    assert getattr(LLMClient, "_is_runtime_protocol", False) is True


# ---- LLMClient surface ----------------------------------------------------


def test_llm_client_member_names_are_frozen() -> None:
    expected = {"model_id", "model_version", "complete"}
    actual = {name for name in vars(LLMClient) if not name.startswith("_")}
    assert actual == expected, f"LLMClient surface drifted. expected={expected!r} actual={actual!r}"


def test_llm_client_properties_are_properties() -> None:
    for name in ("model_id", "model_version"):
        attr = inspect.getattr_static(LLMClient, name)
        assert isinstance(attr, property), f"{name!r} must be a property on LLMClient"


def test_llm_client_property_return_types() -> None:
    # Property fget annotations capture the declared return types.
    model_id_fget = inspect.getattr_static(LLMClient, "model_id").fget
    model_version_fget = inspect.getattr_static(LLMClient, "model_version").fget
    assert model_id_fget is not None
    assert model_version_fget is not None
    assert get_type_hints(model_id_fget)["return"] is str
    assert get_type_hints(model_version_fget)["return"] is str


def test_llm_client_complete_signature() -> None:
    sig = inspect.signature(LLMClient.complete)
    params = sig.parameters
    assert list(params) == ["self", "messages", "max_tokens", "temperature", "stop"]
    # max_tokens / temperature / stop must be keyword-only so callers can't
    # silently break when we add new options later. ``messages`` stays
    # positional-or-keyword to match standard chat-completion call style.
    assert params["max_tokens"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["temperature"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["stop"].kind is inspect.Parameter.KEYWORD_ONLY
    # ``max_tokens`` has no default (caller must supply per ADR-0015 §決定 (h)).
    assert params["max_tokens"].default is inspect.Parameter.empty
    assert params["temperature"].default == 0.2
    assert params["stop"].default is None

    hints = get_type_hints(LLMClient.complete)
    assert hints["messages"] == list[LLMMessage]
    assert hints["max_tokens"] is int
    assert hints["temperature"] is float
    assert hints["stop"] == list[str] | None
    assert hints["return"] is LLMResponse


# ---- LLMMessage / LLMResponse dataclasses ---------------------------------


def test_llm_message_is_frozen_dataclass() -> None:
    assert dataclasses.is_dataclass(LLMMessage)
    # ``frozen=True`` raises FrozenInstanceError on any field mutation — this
    # is the documented public contract, so we exercise it instead of poking
    # at the private ``__dataclass_params__`` attribute (which is untyped).
    msg = LLMMessage(role="user", content="hi")
    with pytest.raises(dataclasses.FrozenInstanceError):
        msg.content = "nope"  # type: ignore[misc]
    # ``slots=True`` keeps the boundary value cheap and prevents accidental
    # attribute additions on instances.
    assert "__slots__" in vars(LLMMessage)

    fields = {f.name: f for f in dataclasses.fields(LLMMessage)}
    assert set(fields) == {"role", "content"}

    hints = get_type_hints(LLMMessage)
    # ``role`` pinned as a 3-value Literal — assistant / user / system only.
    assert hints["role"] == Literal["system", "user", "assistant"]
    assert hints["content"] is str


def test_llm_response_is_frozen_dataclass() -> None:
    assert dataclasses.is_dataclass(LLMResponse)
    resp = LLMResponse(
        text="hello",
        model_id="m",
        model_version="v",
        tokens_in=1,
        tokens_out=2,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        resp.text = "nope"  # type: ignore[misc]
    assert "__slots__" in vars(LLMResponse)

    fields = {f.name: f for f in dataclasses.fields(LLMResponse)}
    assert set(fields) == {
        "text",
        "model_id",
        "model_version",
        "tokens_in",
        "tokens_out",
    }

    hints = get_type_hints(LLMResponse)
    assert hints["text"] is str
    assert hints["model_id"] is str
    assert hints["model_version"] is str
    assert hints["tokens_in"] is int
    assert hints["tokens_out"] is int


# ---- Runtime checkability with a stdlib-only fake -------------------------


def test_llm_client_runtime_check_accepts_duck_typed_fake() -> None:
    """``@runtime_checkable`` should accept a structurally-conforming fake."""

    class _FakeLLMClient:
        model_id = "fake"
        model_version = "v0"

        def complete(
            self,
            messages: list[LLMMessage],
            *,
            max_tokens: int,
            temperature: float = 0.2,
            stop: list[str] | None = None,
        ) -> LLMResponse:
            return LLMResponse(
                text="".join(m.content for m in messages),
                model_id=self.model_id,
                model_version=self.model_version,
                tokens_in=0,
                tokens_out=0,
            )

    assert isinstance(_FakeLLMClient(), LLMClient)
