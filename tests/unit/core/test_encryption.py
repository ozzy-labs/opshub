"""Tests for encryption-at-rest key management (ADR-0021).

Key management is exercised through the env-var override path
(``OPSHUB_DB_ENCRYPTION_KEY``) so the tests stay hermetic and do not
require the ``secrets`` / ``encryption`` extras. The actual SQLCipher
round-trip (and the plaintext-on-disk contrast that proves *why*
ADR-0021 is needed) is gated separately.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opshub.core.encryption import DB_KEY_SECRET_KEY, require_db_key
from opshub.core.errors import ConfigError


def test_require_db_key_reads_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """``require_db_key`` resolves the key from the env override (ADR-0014 reuse)."""
    monkeypatch.setenv("OPSHUB_DB_ENCRYPTION_KEY", "deadbeef")
    assert require_db_key() == "deadbeef"


def test_require_db_key_raises_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing key fails fast with an actionable error (ADR-0021 §(b)).

    The env override is cleared; the keyring lookup must miss too. We
    stub ``get_secret`` to return ``None`` so the test does not depend on
    the OS keychain / keyring extras.
    """
    monkeypatch.delenv("OPSHUB_DB_ENCRYPTION_KEY", raising=False)

    def _no_secret(_key: str) -> None:
        return None

    monkeypatch.setattr("opshub.core.encryption.get_secret", _no_secret)
    with pytest.raises(ConfigError, match="no DB key"):
        require_db_key()


def test_db_key_secret_key_is_stable() -> None:
    """The keyring slot name is part of the operator contract (ADR-0021 §(b))."""
    assert DB_KEY_SECRET_KEY == "db:encryption_key"


def test_resolve_encryption_key_none_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """With ``[storage] encryption`` off, the engine gets ``None`` (plain sqlite3)."""
    from opshub.core.config import OpsHubSettings
    from opshub.db.engine import resolve_encryption_key

    settings = OpsHubSettings()
    assert settings.storage.encryption is False
    assert resolve_encryption_key(settings) is None


def test_unencrypted_db_leaks_body_as_plaintext(tmp_path: Path) -> None:
    """An *unencrypted* DB stores retained bodies as on-disk plaintext.

    This is the ADR-0021 motivation pinned as a regression test: with
    encryption disabled, a secret retained in ``sources.body`` is
    recoverable by reading the raw SQLite file bytes. ADR-0021 closes
    this by encrypting the whole DB (the encrypted-path contrast is
    covered by :func:`test_encrypted_db_does_not_leak_body` when the
    SQLCipher extras are installed).
    """
    from datetime import UTC, datetime

    from opshub.core.ids import new_ulid
    from opshub.db.engine import create_engine_for_sqlite
    from opshub.domain.events import SourceObserved
    from opshub.projections.sources import SourcesProjection, sources_table

    secret_body = "TOPSECRET-BODY-MARKER-9f3a"  # gitleaks:allow
    db_path = tmp_path / "plain.sqlite"
    engine = create_engine_for_sqlite(db_path)
    try:
        sources_table.create(engine)
        event = SourceObserved(
            aggregate_id=new_ulid(),
            occurred_at=datetime(2026, 5, 30, tzinfo=UTC),
            recorded_at=datetime(2026, 5, 30, tzinfo=UTC),
            actor="test",
            connector_name="slack",
            external_id="C1:1.0",
            source_type="slack_message",
            title="alice in #general",
            body=secret_body,
            provenance_origin="external",
            provenance_trust="untrusted",
        )
        with engine.begin() as conn:
            SourcesProjection().apply(conn, event)
    finally:
        engine.dispose()

    raw = db_path.read_bytes()
    assert secret_body.encode() in raw, (
        "an unencrypted DB is expected to leak the body verbatim — this is the "
        "exact exposure ADR-0021 (encryption at rest) closes"
    )


def test_encrypted_db_does_not_leak_body(tmp_path: Path) -> None:
    """When SQLCipher is installed, the same body is NOT plaintext on disk.

    Skipped when the ``encryption`` extras (SQLCipher) are absent — the
    plaintext-leak test above already pins the motivation; this test
    pins the closure when the binding is available.
    """
    pytest.importorskip(
        "sqlcipher3",
        reason="encrypted-path test requires the 'encryption' extras (SQLCipher)",
    )
    from datetime import UTC, datetime

    from opshub.core.ids import new_ulid
    from opshub.db.engine import create_engine_for_sqlite
    from opshub.domain.events import SourceObserved
    from opshub.projections.sources import SourcesProjection, sources_table

    secret_body = "TOPSECRET-BODY-MARKER-9f3a"  # gitleaks:allow
    db_path = tmp_path / "encrypted.sqlite"
    key = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"  # gitleaks:allow
    engine = create_engine_for_sqlite(db_path, encryption_key=key)
    try:
        sources_table.create(engine)
        event = SourceObserved(
            aggregate_id=new_ulid(),
            occurred_at=datetime(2026, 5, 30, tzinfo=UTC),
            recorded_at=datetime(2026, 5, 30, tzinfo=UTC),
            actor="test",
            connector_name="slack",
            external_id="C1:1.0",
            source_type="slack_message",
            title="alice in #general",
            body=secret_body,
            provenance_origin="external",
            provenance_trust="untrusted",
        )
        with engine.begin() as conn:
            SourcesProjection().apply(conn, event)
    finally:
        engine.dispose()

    raw = db_path.read_bytes()
    assert secret_body.encode() not in raw, (
        "an encrypted DB must NOT contain the body verbatim on disk (ADR-0021)"
    )
    # And the SQLCipher header replaces the plain ``SQLite format 3`` magic.
    assert not raw.startswith(b"SQLite format 3"), "SQLCipher DB must not have the plain header"


def test_missing_extras_raises_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requesting encryption without the SQLCipher binding fails with a clear message."""
    import builtins

    from opshub.db.engine import create_engine_for_sqlite

    real_import = builtins.__import__

    def _fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "sqlcipher3.dbapi2" or name.startswith("sqlcipher3"):
            raise ImportError("no sqlcipher3")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    with pytest.raises(ConfigError, match=r"encryption.*extras"):
        create_engine_for_sqlite(tmp_path / "x.sqlite", encryption_key="abc")
