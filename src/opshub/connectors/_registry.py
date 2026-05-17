"""Process-wide Connector registry.

The registry is the single source of truth that ``opshub connector
list`` / ``opshub connector sync <name>`` consult. Connector modules
register themselves via :func:`register_connector`; the CLI iterates
the registry through :func:`discover_connectors`.

Why a module-level dict (not entry points / plugin discovery):

* Phase 3 MVP ships a single concrete connector (GitHub) in-tree under
  ``opshub.connectors.github``. There is no third-party plugin story
  yet, so the overhead of ``importlib.metadata.entry_points`` is not
  justified.
* Explicit ``register_connector`` calls keep import order observable:
  if a connector forgot to register, ``discover_connectors`` returns
  an empty list and the CLI prints a friendly "no connectors
  registered" message rather than a confusing import error.
* ``unregister_all`` exists strictly for test isolation. Production
  code paths never call it.

Idempotency: registering the **same** instance twice is a no-op so that
re-importing a connector module under reload-heavy test harnesses
doesn't blow up. Registering a **different** instance under an
already-occupied name raises :class:`ValueError` — that almost always
indicates two competing implementations of the same connector and
should fail loudly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opshub.connectors.base import Connector


_REGISTRY: dict[str, Connector] = {}


def register_connector(connector: Connector) -> None:
    """Register ``connector`` under its ``.name``.

    Idempotent if the **same** instance is registered twice. Registering
    a different instance under an already-occupied name raises
    :class:`ValueError` so two competing implementations of the same
    connector cannot silently overwrite each other.
    """
    existing = _REGISTRY.get(connector.name)
    if existing is not None and existing is not connector:
        raise ValueError(
            f"connector {connector.name!r} already registered "
            f"(existing={type(existing).__name__}, new={type(connector).__name__})"
        )
    _REGISTRY[connector.name] = connector


def discover_connectors() -> list[Connector]:
    """Return every registered :class:`Connector` instance.

    Phase 3 MVP is empty until sub-issue B (GitHub) lands; the CLI
    falls back to a friendly "no connectors registered" message in
    that case.
    """
    return list(_REGISTRY.values())


def unregister_all() -> None:
    """Test helper: clear the registry between tests.

    Phase 3 production code never calls this. Lives here (not in a
    ``conftest``) so unit tests can import it without coupling to
    pytest internals.
    """
    _REGISTRY.clear()
