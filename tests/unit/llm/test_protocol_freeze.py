"""Freeze tests for the Phase 5 LLM interface + Phase 6 extension.

These tests assert the *exact* shape of :class:`opshub.llm.LLMClient`,
:class:`opshub.llm.LLMMessage`, :class:`opshub.llm.LLMResponse`, and
:class:`opshub.llm.StructuredResponse`. If anyone changes a method name,
renames a parameter, alters a type annotation, or flips a kw-only
marker, these tests fail loudly — that is the entire point.

Rationale (ADR-0015 §決定 + Phase 5 plan §1.1): config (step A5),
BriefingService (step B3), and the ``opshub brief`` CLI (step B4) all
start referencing the Protocol by name in early Phase 5 PRs. Allowing it
to drift before the concrete Anthropic / OpenAI clients land would defeat
the freeze.

Phase 6 step A2 (ADR-0016 §決定 (a)+(b)) extends the surface with
``complete_structured`` + the ``StructuredResponse`` boundary dataclass.
This is **extension only** — no existing member is renamed or removed,
so the Phase 5 freeze contract is preserved. The freeze tests below pin
both the original Phase 5 surface (`complete` / `LLMMessage` /
`LLMResponse`) and the Phase 6 additions.
"""

from __future__ import annotations

import dataclasses
import inspect
import typing
from typing import Literal, Protocol, get_type_hints

import pytest
from pydantic import BaseModel

from opshub.llm import LLMClient, LLMMessage, LLMResponse, StructuredResponse

# ---- Protocol identity ----------------------------------------------------


def test_llm_client_is_runtime_checkable_protocol() -> None:
    assert issubclass(LLMClient, Protocol)  # type: ignore[arg-type]
    # runtime_checkable sets this dunder; assert it explicitly so dropping
    # the decorator fails the freeze test.
    assert getattr(LLMClient, "_is_runtime_protocol", False) is True


# ---- LLMClient surface ----------------------------------------------------


def test_llm_client_member_names_extended_to_include_complete_structured() -> None:
    """Phase 6 freeze extension: the required member set now includes
    ``complete_structured`` (ADR-0016 §決定 (a)+(b)). The Phase 5 members
    (``model_id`` / ``model_version`` / ``complete``) are still present —
    no rename, no removal, only extension.
    """
    expected = {"model_id", "model_version", "complete", "complete_structured"}
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


def test_llm_client_complete_structured_signature() -> None:
    """Phase 6 step A2 (ADR-0016 §決定 (a)+(b)) — pin the structured-output
    method signature. ``schema`` / ``max_tokens`` / ``temperature`` are
    keyword-only so callers can't reorder positionals. ``max_tokens`` has
    no default (caller responsibility for cost control per ADR-0015 §決定
    (h)). Return is ``StructuredResponse`` (we accept either the raw
    class or a parameterised generic alias — callers may add a TypeVar
    in their stubs).
    """
    sig = inspect.signature(LLMClient.complete_structured)
    params = sig.parameters
    assert list(params) == ["self", "messages", "schema", "max_tokens", "temperature"]
    assert params["schema"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["max_tokens"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["temperature"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["max_tokens"].default is inspect.Parameter.empty
    assert params["temperature"].default == 0.2

    hints = get_type_hints(LLMClient.complete_structured)
    assert hints["messages"] == list[LLMMessage]
    assert hints["schema"] == type[BaseModel]
    assert hints["max_tokens"] is int
    assert hints["temperature"] is float
    # Return is StructuredResponse; accept either the raw class or a
    # parameterised generic alias (``StructuredResponse[T]``).
    return_hint = hints["return"]
    origin = typing.get_origin(return_hint)
    assert return_hint is StructuredResponse or origin is StructuredResponse, (
        f"return type must be StructuredResponse, got {return_hint!r}"
    )


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


def test_structured_response_is_frozen_dataclass() -> None:
    """Phase 6 step A2 (ADR-0016 §決定 (b)) — pin the StructuredResponse
    boundary dataclass. Frozen + slots, all 5 fields present."""
    assert dataclasses.is_dataclass(StructuredResponse)

    class _Schema(BaseModel):
        x: int

    resp = StructuredResponse(
        parsed=_Schema(x=1),
        model_id="m",
        model_version="v",
        tokens_in=1,
        tokens_out=2,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        resp.model_id = "nope"  # type: ignore[misc]
    assert "__slots__" in vars(StructuredResponse)

    fields = {f.name: f for f in dataclasses.fields(StructuredResponse)}
    assert set(fields) == {
        "parsed",
        "model_id",
        "model_version",
        "tokens_in",
        "tokens_out",
    }


# ---- Runtime checkability with a stdlib-only fake -------------------------


def test_llm_client_runtime_check_still_accepts_duck_typed_fake_with_structured() -> None:
    """Phase 6 freeze extension adds ``complete_structured`` to the
    required member set. ``@runtime_checkable`` Protocol's ``isinstance``
    check verifies all Protocol members exist as attributes on the
    candidate — so the Phase 5 minimal fake (without ``complete_structured``)
    no longer satisfies ``isinstance(..., LLMClient)``. This is the
    documented contract change for Phase 6 step A2.

    A fake that implements **both** ``complete`` and ``complete_structured``
    continues to be accepted by the runtime check.
    """

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

        def complete_structured(
            self,
            messages: list[LLMMessage],
            *,
            schema: type[BaseModel],
            max_tokens: int,
            temperature: float = 0.2,
        ) -> StructuredResponse[BaseModel]:
            # Build a trivial instance of the requested schema; real
            # backends will JSON-parse + Pydantic-validate the LLM's
            # tool-call arguments here.
            parsed = schema.model_construct()
            return StructuredResponse(
                parsed=parsed,
                model_id=self.model_id,
                model_version=self.model_version,
                tokens_in=0,
                tokens_out=0,
            )

    assert isinstance(_FakeLLMClient(), LLMClient)
