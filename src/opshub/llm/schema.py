"""Pydantic v2 → tool-definition schema converter (Phase 6 A2, ADR-0016 §決定 (b)).

The single source of truth for tool / function schemas is a Pydantic
``BaseModel``. Each LLM backend (Anthropic / OpenAI / Ollama) calls
this helper to convert the model into the JSON schema that the
provider's tool definition expects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pydantic import BaseModel

__all__ = ["pydantic_to_tool_schema"]


def pydantic_to_tool_schema(
    model: type[BaseModel],
    *,
    name: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Convert a Pydantic v2 ``BaseModel`` into a JSON schema dict.

    The returned dict is the ``parameters`` payload that Anthropic /
    OpenAI / Ollama tool definitions take. Wrappers (e.g. wrapping
    in ``{"name": ..., "description": ..., "input_schema": ...}`` for
    Anthropic) are the responsibility of each client because the
    outer shape differs per provider.

    Parameters
    ----------
    model:
        The Pydantic model that defines the structured response shape.
    name:
        Optional override for the tool name (default: derived from
        ``model.__name__``, lowercased + underscore-separated).
    description:
        Optional human description (default: ``model.__doc__`` first
        line if present, else a generic fallback).

    Returns
    -------
    dict
        JSON schema dictionary with ``"name"`` / ``"description"`` /
        ``"parameters"`` keys. ``"parameters"`` is the Pydantic
        ``model.model_json_schema()`` output, with one small
        adjustment: ``"additionalProperties": false`` is enforced so
        backends reject extra fields (per ADR-0016 schema-validation
        contract).
    """
    parameters = model.model_json_schema()
    # Force strict schema: providers (especially OpenAI's strict mode)
    # require ``additionalProperties: false`` on every nested object
    # for the structured-output guarantee. Pydantic v2 emits these
    # only on the root by default.
    _enforce_strict(parameters)

    tool_name = name or _default_tool_name(model)
    tool_description = description or _default_description(model)
    return {
        "name": tool_name,
        "description": tool_description,
        "parameters": parameters,
    }


def _enforce_strict(node: Any) -> None:
    """Recursively add ``additionalProperties: false`` to every object schema.

    Pydantic v2's ``model_json_schema()`` output is a deeply nested
    ``dict[str, Any] | list[Any]`` tree with no static guarantees about
    the contents, so the recursive walker takes :class:`Any`. The
    isinstance gates restrict the actual recursion to dict / list
    nodes; primitives are no-ops. The :func:`cast` annotations narrow
    pyright's view of the narrowed dict / list (which it otherwise
    reports as ``dict[Unknown, Unknown]`` / ``list[Unknown]``); mypy
    sees the casts as widening from already-``Any`` so it does not
    flag them as redundant.
    """
    if isinstance(node, dict):
        dict_node = cast(dict[str, Any], node)
        if dict_node.get("type") == "object" and "additionalProperties" not in dict_node:
            dict_node["additionalProperties"] = False
        for value in dict_node.values():
            _enforce_strict(value)
    elif isinstance(node, list):
        for item in node:  # pyright: ignore[reportUnknownVariableType]
            _enforce_strict(item)


def _default_tool_name(model: type[BaseModel]) -> str:
    """Convert ``ProposalCandidates`` → ``proposal_candidates``."""
    name = model.__name__
    out: list[str] = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0 and not name[i - 1].isupper():
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def _default_description(model: type[BaseModel]) -> str:
    doc = (model.__doc__ or "").strip()
    if doc:
        return doc.splitlines()[0].strip()
    return f"Structured output schema for {model.__name__}"
