"""Shared pytest fixtures for ``tests/unit/cli/`` (Phase 17-B, ADR-0031).

The per-noun ``auth set / test`` tests
(``test_slack_auth.py`` / ``test_github_auth.py`` / ...) all need the
same in-memory keyring + env-var override clearing. Centralising the
boilerplate here keeps the per-noun test files focussed on the
behaviour they pin.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator


keyring: Any = pytest.importorskip(
    "keyring",
    reason="`opshub <connector> auth set` tests require the 'secrets' extras",
)
_KeyringBackend: Any = keyring.backend.KeyringBackend


class InMemoryKeyring(_KeyringBackend):  # type: ignore[misc,unused-ignore]
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
def in_memory_keyring(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemoryKeyring]:
    """Install an in-memory keyring backend and clear env-var overrides.

    Env vars are cleared so ``get_secret`` (used to verify the
    round-trip) actually consults the in-memory backend instead of
    returning a leaked env value from an earlier test.

    ADR-0031 §non-goals pins env var names as unchanged — listing
    them here mirrors the keyring slots the production CLI writes to.
    """
    for var in (
        "OPSHUB_CONNECTOR_GITHUB_PAT",
        "OPSHUB_CONNECTOR_SLACK_TOKEN",
        "OPSHUB_CONNECTOR_TEAMS_TOKEN",
        "OPSHUB_EMBEDDER_OPENAI_API_KEY",
        "OPSHUB_EMBEDDER_VOYAGE_API_KEY",
        "OPSHUB_LLM_ANTHROPIC_API_KEY",
        "OPSHUB_LLM_OPENAI_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    previous = keyring.get_keyring()
    backend = InMemoryKeyring()
    keyring.set_keyring(backend)
    try:
        yield backend
    finally:
        keyring.set_keyring(previous)
