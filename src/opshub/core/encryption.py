"""Database encryption-at-rest key management (Phase 10, ADR-0021).

ADR-0020 (Full Local Content Retention) retains external bodies in the
SQLite DB; ADR-0021 mandates the DB be encrypted at rest with SQLCipher
(whole-DB AES-256) so those bodies never sit on disk in plaintext. This
module owns the **key**, not the encryption itself — the actual cipher is
applied by the SQLCipher-backed driver via ``PRAGMA key`` in
:func:`opshub.db.engine.create_engine_for_sqlite`.

Key storage reuses the ADR-0014 keyring path (``core.secrets``): the key
lives under keyring service ``"opshub"`` key :data:`DB_KEY_SECRET_KEY`,
with the env-var override ``OPSHUB_DB_ENCRYPTION_KEY`` (the standard
``OPSHUB_<KEY>`` transform) for CI / headless environments where the OS
keychain is unreachable. opshub never writes the key to disk in plaintext
itself (ADR-0021 §(b)).

Key lifecycle (ADR-0021 §(b)):

* :func:`get_or_create_db_key` — used by ``opshub init`` on a *fresh*
  DB. Mints a CSPRNG key and persists it to the keyring when none
  exists; returns the existing key otherwise. Creating a new key for an
  already-encrypted DB would make it permanently unreadable, so this is
  only safe at init time before the DB file exists.
* :func:`require_db_key` — used when opening an *existing* encrypted DB.
  Fails fast with an actionable :class:`ConfigError` when the key is
  absent rather than silently producing a new (wrong) key — opening an
  encrypted DB with the wrong key surfaces as "file is not a database",
  so we fail earlier with a clearer message.
"""

from __future__ import annotations

import secrets as _secrets

from opshub.core.errors import ConfigError
from opshub.core.secrets import get_secret, set_secret

__all__ = [
    "DB_KEY_SECRET_KEY",
    "get_or_create_db_key",
    "require_db_key",
]

#: keyring key (under service ``"opshub"``) holding the SQLCipher DB key.
#: The env-var override is ``OPSHUB_DB_ENCRYPTION_KEY`` via the standard
#: :func:`opshub.core.secrets._env_var_name` transform.
DB_KEY_SECRET_KEY = "db:encryption_key"

#: Bytes of entropy minted for a fresh DB key. 32 bytes → 64 hex chars;
#: SQLCipher treats a hex string passed to ``PRAGMA key`` as raw key
#: material, giving a full 256-bit key with no KDF round-trip.
_KEY_BYTES = 32


def get_or_create_db_key() -> str:
    """Return the DB encryption key, minting + persisting one if absent.

    Safe to call only on a *fresh* DB (before the file is created):
    minting a new key for an existing encrypted DB would make it
    unreadable. ``opshub init`` is the intended caller.

    Resolution order matches :func:`opshub.core.secrets.get_secret`
    (env-var override → keyring). When neither holds a key, a fresh
    CSPRNG key is generated and written to the keyring (never to the
    env, which is read-only override material per ADR-0014).
    """
    existing = get_secret(DB_KEY_SECRET_KEY)
    if existing is not None:
        return existing
    key = _secrets.token_hex(_KEY_BYTES)
    set_secret(DB_KEY_SECRET_KEY, key)
    return key


def require_db_key() -> str:
    """Return the DB encryption key, raising :class:`ConfigError` when absent.

    Used when opening an existing encrypted DB. Fails fast with an
    actionable message (env-var fallback + keyring pointer) rather than
    minting a new key that could not possibly decrypt the existing file.
    """
    key = get_secret(DB_KEY_SECRET_KEY)
    if key is None:
        raise ConfigError(
            "encryption is enabled ([storage] encryption = true) but no DB key "
            f"was found. Set OPSHUB_DB_ENCRYPTION_KEY in the environment, or store "
            f"the key in the keyring under {DB_KEY_SECRET_KEY!r} (opshub init mints "
            "one automatically for a fresh DB). See ADR-0021 / docs for details."
        )
    return key
