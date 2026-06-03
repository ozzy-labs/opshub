"""Tests for ``opshub embedder auth set`` (Phase 17-B, ADR-0031)."""

from __future__ import annotations

from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.core.secrets import get_secret
from opshub.vectors.openai_embedder import OPENAI_API_KEY_SECRET
from opshub.vectors.voyage_embedder import VOYAGE_API_KEY_SECRET
from tests.unit.cli.conftest import InMemoryKeyring


def test_embedder_auth_set_openai_with_token_flag(
    in_memory_keyring: InMemoryKeyring,
) -> None:
    """``embedder auth set openai --token sk-xxx`` writes to the OpenAI keyring key."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["embedder", "auth", "set", "openai", "--token", "sk-xxx"],
    )

    assert result.exit_code == 0, result.stdout
    assert "embedder:openai" in result.stdout
    assert get_secret(OPENAI_API_KEY_SECRET) == "sk-xxx"


def test_embedder_auth_set_voyage_with_token_flag(
    in_memory_keyring: InMemoryKeyring,
) -> None:
    """``embedder auth set voyage --token pa-xxx`` writes to the Voyage keyring key."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["embedder", "auth", "set", "voyage", "--token", "pa-xxx"],
    )

    assert result.exit_code == 0, result.stdout
    assert "embedder:voyage" in result.stdout
    assert get_secret(VOYAGE_API_KEY_SECRET) == "pa-xxx"


def test_embedder_auth_set_unknown_vendor_exits_2(
    in_memory_keyring: InMemoryKeyring,
) -> None:
    """Unknown vendor → exit 2 with supported list on stderr."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["embedder", "auth", "set", "cohere", "--token", "x"],
    )

    assert result.exit_code == 2
    assert "cohere" in result.stderr
    assert "openai" in result.stderr
    assert "voyage" in result.stderr


def test_embedder_auth_set_rejects_empty_token(
    in_memory_keyring: InMemoryKeyring,
) -> None:
    """Empty token → exit 2."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["embedder", "auth", "set", "openai", "--token", ""],
    )

    assert result.exit_code == 2
    assert "non-empty" in result.stderr
    assert get_secret(OPENAI_API_KEY_SECRET) is None


def test_embedder_auth_set_with_stdin_prompt(
    in_memory_keyring: InMemoryKeyring,
) -> None:
    """No ``--token`` → prompt with hidden input."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["embedder", "auth", "set", "openai"],
        input="sk-prompted\n",
    )

    assert result.exit_code == 0, result.stdout
    assert get_secret(OPENAI_API_KEY_SECRET) == "sk-prompted"
