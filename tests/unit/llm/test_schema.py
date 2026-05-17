"""Tests for ``opshub.llm.schema.pydantic_to_tool_schema`` (Phase 6 A2).

The helper is the SSOT converter for ADR-0016 §決定 (b) — Anthropic /
OpenAI / Ollama backends all call it to translate a Pydantic v2 model
into the JSON schema embedded in their native tool / function
definition. These tests pin the shape so backend implementations in
A3 / A4 can rely on a stable contract.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from opshub.llm.schema import pydantic_to_tool_schema


def test_simple_model_round_trips_via_pydantic_json_schema() -> None:
    class Foo(BaseModel):
        x: int

    out = pydantic_to_tool_schema(Foo)
    assert set(out.keys()) == {"name", "description", "parameters"}
    params = out["parameters"]
    assert params["type"] == "object"
    assert params["properties"]["x"]["type"] == "integer"


def test_default_tool_name_is_snake_case_of_class_name() -> None:
    class ProposalCandidates(BaseModel):
        pass

    out = pydantic_to_tool_schema(ProposalCandidates)
    assert out["name"] == "proposal_candidates"


def test_name_override() -> None:
    class Foo(BaseModel):
        x: int

    out = pydantic_to_tool_schema(Foo, name="my_tool")
    assert out["name"] == "my_tool"


def test_description_falls_back_to_docstring_first_line() -> None:
    class WithDoc(BaseModel):
        """First line.

        Second para.
        """

        x: int

    out = pydantic_to_tool_schema(WithDoc)
    assert out["description"] == "First line."


def test_description_falls_back_to_generic_when_no_doc() -> None:
    class NoDoc(BaseModel):
        x: int

    NoDoc.__doc__ = None  # ensure no docstring
    out = pydantic_to_tool_schema(NoDoc)
    assert "NoDoc" in out["description"]


def test_description_override() -> None:
    class Foo(BaseModel):
        x: int

    out = pydantic_to_tool_schema(Foo, description="explicit description")
    assert out["description"] == "explicit description"


def test_additionalproperties_false_is_enforced_on_root() -> None:
    class Foo(BaseModel):
        x: int

    out = pydantic_to_tool_schema(Foo)
    assert out["parameters"]["additionalProperties"] is False


def test_additionalproperties_false_is_enforced_on_nested_objects() -> None:
    class Inner(BaseModel):
        y: int

    class Outer(BaseModel):
        inner: Inner

    out = pydantic_to_tool_schema(Outer)
    params = out["parameters"]
    # Root has it.
    assert params["additionalProperties"] is False
    # Pydantic v2 emits nested models under ``$defs``.
    defs = params.get("$defs", {})
    assert "Inner" in defs, f"expected Inner schema under $defs, got {sorted(defs)!r}"
    inner_schema = defs["Inner"]
    assert inner_schema["type"] == "object"
    assert inner_schema["additionalProperties"] is False


def test_pydantic_to_tool_schema_works_with_discriminated_union() -> None:
    """Sanity check: the helper does not crash on a discriminated union
    and the discriminator field surfaces somewhere in the output."""

    class TaskCandidate(BaseModel):
        kind: Literal["task"]
        title: str

    class DecisionCandidate(BaseModel):
        kind: Literal["decision"]
        text: str

    class Wrapper(BaseModel):
        candidate: TaskCandidate | DecisionCandidate

    out = pydantic_to_tool_schema(Wrapper)
    # Just verify the converter did not crash and produced a valid
    # schema dict containing a reference to one of the union members.
    flat = repr(out)
    assert "kind" in flat
    assert "TaskCandidate" in flat or "DecisionCandidate" in flat
