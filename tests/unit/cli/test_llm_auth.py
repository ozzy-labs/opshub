"""Tests for ``opshub llm auth set`` (Phase 17-B, ADR-0031)."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.core.secrets import get_secret
from opshub.vectors.openai_embedder import OPENAI_API_KEY_SECRET
from tests.unit.cli.conftest import InMemoryKeyring


def test_llm_auth_set_anthropic_with_token_flag(
    in_memory_keyring: InMemoryKeyring,
) -> None:
    """``llm auth set anthropic --token ...`` writes to the Anthropic keyring key."""
    pytest.importorskip(
        "opshub.llm.anthropic_client",
        reason="llm:anthropic auth path requires the 'llm-anthropic' extras",
    )
    from opshub.llm.anthropic_client import ANTHROPIC_API_KEY_SECRET

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["llm", "auth", "set", "anthropic", "--token", "sk-ant-xxx"],
    )

    assert result.exit_code == 0, result.stdout
    assert "llm:anthropic" in result.stdout
    assert get_secret(ANTHROPIC_API_KEY_SECRET) == "sk-ant-xxx"


def test_llm_auth_set_openai_with_token_flag(
    in_memory_keyring: InMemoryKeyring,
) -> None:
    """``llm auth set openai --token ...`` writes to the LLM OpenAI keyring key."""
    pytest.importorskip(
        "opshub.llm.openai_client",
        reason="llm:openai auth path requires the 'llm-openai' extras",
    )
    from opshub.llm.openai_client import OPENAI_API_KEY_SECRET as LLM_OPENAI_KEY

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["llm", "auth", "set", "openai", "--token", "sk-proj-xxx"],
    )

    assert result.exit_code == 0, result.stdout
    assert "llm:openai" in result.stdout
    assert get_secret(LLM_OPENAI_KEY) == "sk-proj-xxx"


def test_llm_auth_set_unknown_vendor_exits_2(
    in_memory_keyring: InMemoryKeyring,
) -> None:
    """Unknown vendor → exit 2 with supported list."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["llm", "auth", "set", "gemini", "--token", "x"],
    )

    assert result.exit_code == 2
    assert "gemini" in result.stderr
    assert "anthropic" in result.stderr
    assert "openai" in result.stderr


def test_llm_auth_set_rejects_empty_token(
    in_memory_keyring: InMemoryKeyring,
) -> None:
    """Empty token → exit 2."""
    pytest.importorskip(
        "opshub.llm.anthropic_client",
        reason="llm:anthropic auth path requires the 'llm-anthropic' extras",
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["llm", "auth", "set", "anthropic", "--token", ""],
    )

    assert result.exit_code == 2
    assert "non-empty" in result.stderr


def test_llm_openai_distinct_from_embedder_openai(
    in_memory_keyring: InMemoryKeyring,
) -> None:
    """``llm:openai`` and ``embedder:openai`` use distinct keyring slots.

    ADR-0015 §決定 (a) — extras independence. Operators can run
    briefing on OpenAI and embeddings on Voyage (or vice versa)
    without sharing one API key.
    """
    pytest.importorskip(
        "opshub.llm.openai_client",
        reason="llm:openai auth path requires the 'llm-openai' extras",
    )
    from opshub.llm.openai_client import OPENAI_API_KEY_SECRET as LLM_KEY

    runner = CliRunner()
    r1 = runner.invoke(app, ["llm", "auth", "set", "openai", "--token", "sk-llm-only"])
    r2 = runner.invoke(app, ["embedder", "auth", "set", "openai", "--token", "sk-emb-only"])
    assert r1.exit_code == 0
    assert r2.exit_code == 0

    assert get_secret(LLM_KEY) == "sk-llm-only"
    assert get_secret(OPENAI_API_KEY_SECRET) == "sk-emb-only"
    assert LLM_KEY != OPENAI_API_KEY_SECRET
