"""Tests for ``opshub connector auth set`` (Phase 3 step B1).

The CLI wraps :func:`opshub.core.secrets.set_secret` with three pieces
of behaviour worth pinning:

1. The ``--token`` flag round-trips into the same keyring key the
   connector reader uses (``GITHUB_PAT_SECRET_KEY``).
2. When ``--token`` is omitted the user is prompted with hidden input
   (we exercise the prompt path via ``CliRunner.invoke(..., input=...)``).
3. Empty / whitespace-only tokens and unknown connector names exit 2
   with a helpful stderr message — exit 0 on bad input would silently
   store an empty token.

By design there is **no** ``auth get`` command (we never echo tokens to
stdout); this test file pins that policy alongside the positive
behaviour. The in-memory keyring backend mirrors the pattern from
``tests/unit/core/test_secrets``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.connectors.github.auth import GITHUB_PAT_SECRET_KEY
from opshub.connectors.slack.auth import SLACK_BOT_TOKEN_SECRET_KEY
from opshub.core.secrets import get_secret
from opshub.vectors.openai_embedder import OPENAI_API_KEY_SECRET
from opshub.vectors.voyage_embedder import VOYAGE_API_KEY_SECRET

if TYPE_CHECKING:
    from collections.abc import Iterator


keyring: Any = pytest.importorskip(
    "keyring",
    reason="`opshub connector auth set` tests require the 'secrets' extras",
)
_KeyringBackend: Any = keyring.backend.KeyringBackend


class _InMemoryKeyring(_KeyringBackend):  # type: ignore[misc,unused-ignore]
    """Process-local keyring backend used by the test suite."""

    priority = 1  # type: ignore[assignment,unused-ignore]

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self._store.pop((service, username), None)


@pytest.fixture
def in_memory_keyring(monkeypatch: pytest.MonkeyPatch) -> Iterator[_InMemoryKeyring]:
    """Install an in-memory keyring backend and clear the env-var override.

    The env var is cleared here so ``get_secret`` (used to verify the
    round-trip) actually consults the in-memory backend instead of
    returning a leaked env value from an earlier test.
    """
    monkeypatch.delenv("OPSHUB_CONNECTOR_GITHUB_PAT", raising=False)
    previous = keyring.get_keyring()
    backend = _InMemoryKeyring()
    keyring.set_keyring(backend)
    try:
        yield backend
    finally:
        keyring.set_keyring(previous)


# ----- happy paths -------------------------------------------------------


def test_auth_set_github_with_token_flag_stores_to_keyring(
    in_memory_keyring: _InMemoryKeyring,
) -> None:
    """``--token`` writes to the key the connector reader uses."""
    runner = CliRunner()
    result = runner.invoke(app, ["connector", "auth", "set", "github", "--token", "ghp_xxx"])

    assert result.exit_code == 0, result.stdout
    assert "github" in result.stdout
    # The connector reads the token via this exact key; the round-trip
    # check pins the CLI writer / connector reader contract.
    assert get_secret(GITHUB_PAT_SECRET_KEY) == "ghp_xxx"


def test_auth_set_github_with_stdin_prompt(
    in_memory_keyring: _InMemoryKeyring,
) -> None:
    """No ``--token`` → prompt; CliRunner.input feeds the hidden prompt."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["connector", "auth", "set", "github"],
        input="ghp_prompted\n",
    )

    assert result.exit_code == 0, result.stdout
    assert get_secret(GITHUB_PAT_SECRET_KEY) == "ghp_prompted"


def test_auth_set_strips_surrounding_whitespace(
    in_memory_keyring: _InMemoryKeyring,
) -> None:
    """Tokens pasted with trailing newline / spaces should be stored clean.

    Users routinely copy a PAT from a web UI and the clipboard picks up
    a trailing newline; silently storing ``"ghp_xxx\\n"`` would cause
    confusing 401s at sync time. The CLI normalises by stripping.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["connector", "auth", "set", "github", "--token", "  ghp_xxx  "])

    assert result.exit_code == 0, result.stdout
    assert get_secret(GITHUB_PAT_SECRET_KEY) == "ghp_xxx"


# ----- slack auth target (Phase 7 step A1) ------------------------------


def test_auth_set_slack_with_token_flag_stores_to_keyring(
    in_memory_keyring: _InMemoryKeyring,
) -> None:
    """``--token`` writes to the keyring slot the SlackAuth reader uses.

    The ``connector:slack:bot_token`` key is the CLI writer ↔ SlackAuth
    reader contract (mirrors the Phase 3 GitHub PAT precedent). Pinning
    the round-trip here keeps the two halves from drifting.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["connector", "auth", "set", "slack", "--token", "xoxb-test"])

    assert result.exit_code == 0, result.stdout
    assert "slack" in result.stdout
    assert get_secret(SLACK_BOT_TOKEN_SECRET_KEY) == "xoxb-test"


def test_auth_set_slack_uses_distinct_key_from_github(
    in_memory_keyring: _InMemoryKeyring,
) -> None:
    """The Slack and GitHub connectors must store credentials under
    different keyring slots — an operator pasting a GitHub PAT into
    the Slack target (or vice versa) would silently overwrite the
    wrong credential under a shared key. This test pins the
    separation."""
    runner = CliRunner()
    r1 = runner.invoke(app, ["connector", "auth", "set", "github", "--token", "ghp_xxx"])
    r2 = runner.invoke(app, ["connector", "auth", "set", "slack", "--token", "xoxb-yyy"])

    assert r1.exit_code == 0
    assert r2.exit_code == 0
    assert get_secret(GITHUB_PAT_SECRET_KEY) == "ghp_xxx"
    assert get_secret(SLACK_BOT_TOKEN_SECRET_KEY) == "xoxb-yyy"
    # And the keys themselves are distinct strings — a sanity check
    # that nothing collapsed them into one constant during a refactor.
    assert GITHUB_PAT_SECRET_KEY != SLACK_BOT_TOKEN_SECRET_KEY


# ----- error paths -------------------------------------------------------


def test_auth_set_rejects_empty_token(
    in_memory_keyring: _InMemoryKeyring,
) -> None:
    """``--token ""`` is a user error, not a "stored empty token"."""
    runner = CliRunner()
    result = runner.invoke(app, ["connector", "auth", "set", "github", "--token", ""])

    assert result.exit_code == 2
    assert "non-empty" in result.stderr
    # Nothing was written to the keyring backend.
    assert get_secret(GITHUB_PAT_SECRET_KEY) is None


def test_auth_set_rejects_whitespace_only_token(
    in_memory_keyring: _InMemoryKeyring,
) -> None:
    """Whitespace-only tokens are also rejected — stored empty token would
    be just as broken at sync time as outright empty input."""
    runner = CliRunner()
    result = runner.invoke(app, ["connector", "auth", "set", "github", "--token", "   "])

    assert result.exit_code == 2
    assert "non-empty" in result.stderr
    assert get_secret(GITHUB_PAT_SECRET_KEY) is None


def test_auth_set_unknown_connector(
    in_memory_keyring: _InMemoryKeyring,
) -> None:
    """Unknown target → exit 2 with the supported list on stderr.

    Phase 4 step B3 extends the supported list to include the
    ``embedder:openai`` / ``embedder:voyage`` API-key targets alongside
    ``github``. Phase 5 step A5 further adds ``llm:anthropic`` /
    ``llm:openai`` (ADR-0015 §決定 (d)). Phase 7 step A1 adds ``slack``
    and step B1 adds ``connector:ms365`` (interactive OAuth paste-code
    flow). The error must enumerate every supported name so the
    operator can copy-paste the right one.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["connector", "auth", "set", "ms365", "--token", "x"])

    assert result.exit_code == 2
    assert "unknown auth target" in result.stderr
    assert "ms365" in result.stderr
    # All currently-supported names must appear in the hint.
    assert "github" in result.stderr
    assert "slack" in result.stderr
    assert "embedder:openai" in result.stderr
    assert "embedder:voyage" in result.stderr
    assert "llm:anthropic" in result.stderr
    assert "llm:openai" in result.stderr
    assert "connector:ms365" in result.stderr


# ----- embedder API-key targets (Phase 4 step B3) -----------------------


def test_auth_set_embedder_openai_with_token_flag(
    in_memory_keyring: _InMemoryKeyring,
) -> None:
    """``embedder:openai --token sk-xxx`` writes to the OpenAI keyring key.

    The :class:`OpenAIEmbedder` reader (:mod:`opshub.vectors.openai_embedder`)
    consults the exact same constant; pinning the round-trip here keeps
    the CLI writer / embedder reader contract from drifting.
    """
    runner = CliRunner()
    result = runner.invoke(
        app, ["connector", "auth", "set", "embedder:openai", "--token", "sk-xxx"]
    )

    assert result.exit_code == 0, result.stdout
    assert "embedder:openai" in result.stdout
    assert get_secret(OPENAI_API_KEY_SECRET) == "sk-xxx"


def test_auth_set_embedder_voyage_with_token_flag(
    in_memory_keyring: _InMemoryKeyring,
) -> None:
    """``embedder:voyage --token pa-xxx`` writes to the Voyage keyring key."""
    runner = CliRunner()
    result = runner.invoke(
        app, ["connector", "auth", "set", "embedder:voyage", "--token", "pa-xxx"]
    )

    assert result.exit_code == 0, result.stdout
    assert "embedder:voyage" in result.stdout
    assert get_secret(VOYAGE_API_KEY_SECRET) == "pa-xxx"


# ----- LLM API-key targets (Phase 5 step A5, ADR-0015 §決定 (d)) ---------


def test_auth_set_llm_anthropic_with_token_flag(
    in_memory_keyring: _InMemoryKeyring,
) -> None:
    """``llm:anthropic --token ...`` writes to the Anthropic keyring key.

    The :class:`AnthropicLLMClient` reader (:mod:`opshub.llm.anthropic_client`)
    consults the same ``ANTHROPIC_API_KEY_SECRET`` constant; pinning the
    round-trip here keeps the CLI writer / client reader contract from
    drifting (mirrors the Phase 4 ``embedder:openai`` test pattern).
    """
    anthropic_module: Any = pytest.importorskip(
        "opshub.llm.anthropic_client",
        reason="llm:anthropic auth path requires the 'llm-anthropic' extras",
    )
    runner = CliRunner()
    result = runner.invoke(
        app, ["connector", "auth", "set", "llm:anthropic", "--token", "sk-ant-xxx"]
    )

    assert result.exit_code == 0, result.stdout
    assert "llm:anthropic" in result.stdout
    assert get_secret(anthropic_module.ANTHROPIC_API_KEY_SECRET) == "sk-ant-xxx"


def test_auth_set_llm_openai_with_token_flag(
    in_memory_keyring: _InMemoryKeyring,
) -> None:
    """``llm:openai --token ...`` writes to the OpenAI LLM keyring key.

    Distinct from the Phase 4 ``embedder:openai`` target: the LLM and
    embedding paths each have their own keyring key so opting into one
    feature does not implicitly grant the other (ADR-0015 §決定 (a)
    — extras independence).
    """
    openai_module: Any = pytest.importorskip(
        "opshub.llm.openai_client",
        reason="llm:openai auth path requires the 'llm-openai' extras",
    )
    runner = CliRunner()
    result = runner.invoke(
        app, ["connector", "auth", "set", "llm:openai", "--token", "sk-proj-xxx"]
    )

    assert result.exit_code == 0, result.stdout
    assert "llm:openai" in result.stdout
    assert get_secret(openai_module.OPENAI_API_KEY_SECRET) == "sk-proj-xxx"


def test_auth_set_llm_keys_are_distinct_from_embedder_keys(
    in_memory_keyring: _InMemoryKeyring,
) -> None:
    """Writing ``llm:openai`` must NOT collide with ``embedder:openai``.

    Both backends pull from OpenAI but they store credentials under
    different keyring keys (``llm:openai:api_key`` vs
    ``embedder:openai:api_key``) so an operator can run briefing on
    OpenAI and embeddings on Voyage (or vice versa) without sharing one
    API key. This test pins that separation.
    """
    pytest.importorskip(
        "opshub.llm.openai_client",
        reason="llm:openai auth path requires the 'llm-openai' extras",
    )
    runner = CliRunner()
    # Write distinct values to each target.
    r1 = runner.invoke(app, ["connector", "auth", "set", "llm:openai", "--token", "sk-llm-only"])
    r2 = runner.invoke(
        app, ["connector", "auth", "set", "embedder:openai", "--token", "sk-emb-only"]
    )
    assert r1.exit_code == 0
    assert r2.exit_code == 0

    from opshub.llm.openai_client import OPENAI_API_KEY_SECRET as LLM_KEY

    assert get_secret(LLM_KEY) == "sk-llm-only"
    assert get_secret(OPENAI_API_KEY_SECRET) == "sk-emb-only"


def test_auth_does_not_expose_get_subcommand() -> None:
    """Security policy: there is no ``auth get`` (tokens must not echo to stdout).

    Pinning this in a test means a future contributor who innocently
    adds ``@auth_app.command("get")`` to "round out the CRUD" will see
    a red CI before the change ships.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["connector", "auth", "get", "github"])
    # Typer reports unknown subcommands with exit code != 0; the precise
    # code is 2 for usage errors. The important assertion is that the
    # command did NOT succeed.
    assert result.exit_code != 0
