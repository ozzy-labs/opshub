"""Shared connector-module discovery helper.

Concrete :class:`~opshub.connectors.base.Connector` implementations
register themselves with the process-wide registry as an
import-side-effect of their package ``__init__`` (each calls
``register_connector(<Concrete>Connector())`` at module top level).
Callers therefore need to **explicitly import** every connector
subpackage before reading the registry — otherwise
:func:`~opshub.connectors.discover_connectors` returns an empty list.

This helper centralises that import set so the CLI surface
(``opshub connectors`` / ``opshub <connector> sync``) and the MCP
``connector.sync`` write handler all see the same connectors. Before
Phase 17-B the import block lived twice — once inline in
``opshub.cli.connector`` and once inline in ``opshub.mcp._writes`` —
and the duplication caused two bugs in tandem:

1. The Phase 17-B refactor (PR #414) split ``opshub connector list``
   into the new ``opshub connectors`` command but forgot to carry over
   the import call, so the new command reported "no connectors
   registered" on a fresh process even though every connector was
   wired in-tree.
2. The MCP inline list was missed when Phase 14 added
   ``google_calendar`` / ``google_mail``, so MCP clients invoking
   ``connector.sync`` for either name received "unknown connector"
   while the CLI surface accepted them.

Pulling the import set into one typer-free module fixes both at once
and prevents the same drift recurring when a future connector lands.

Why a dedicated ``_discovery`` module (not ``_registry``):

* :mod:`opshub.connectors._registry` is scoped to the registry data
  structure (``register_connector`` / ``discover_connectors`` /
  ``unregister_all``). Discovery — i.e. *which* modules to import —
  is a higher-level concern that does not belong inside the registry
  primitive.
* Keeping discovery separate preserves the contract that importing
  ``opshub.connectors`` itself stays cheap (no transitive connector
  imports). The CLI cold-start budget (~300ms, ADR-0001) depends on
  this.

Why not call this from :func:`discover_connectors` itself:

* Several test suites (e.g. ``tests/unit/cli/test_connectors_command.py``)
  rely on ``unregister_all()`` + ``register_connector(stub)`` to
  populate the registry with synthetic connectors only. Auto-importing
  every real connector inside ``discover_connectors`` would mix real
  registrations back in and break those isolation patterns.

Each ``try / except ImportError`` arm guards against partial-extras
installs (e.g. an operator who only installed
``[connectors-github]`` should still be able to sync GitHub). All
connector modules are import-clean (heavy SDKs are deferred into
method bodies), so these guards are defensive — they keep a future
refactor that adds a top-level SDK import from breaking discovery for
the *other* connectors.

Module-level imports are restricted to ``__future__`` only so this
module is safe to import from typer-free request paths (the MCP write
handler comment ``"avoid pulling typer into the request path"`` is
why the inline duplication existed in the first place).
"""

from __future__ import annotations


def import_connector_modules() -> None:
    """Best-effort import of every connector subpackage.

    Each subpackage triggers ``register_connector`` as an import side
    effect; the ``try / except ImportError`` arms guard against
    partial-extras installs. Safe to call multiple times — once a
    module is in ``sys.modules`` the second import is a no-op (and the
    initial ``register_connector`` already populated the registry).
    """
    # GitHub
    try:
        import opshub.connectors.github  # pyright: ignore[reportUnusedImport]
    except ImportError:
        pass

    # Slack
    try:
        import opshub.connectors.slack  # pyright: ignore[reportUnusedImport]
    except ImportError:
        pass

    # MS365
    try:
        import opshub.connectors.ms365  # pyright: ignore[reportUnusedImport]
    except ImportError:
        pass

    # Box
    try:
        import opshub.connectors.box  # pyright: ignore[reportUnusedImport]
    except ImportError:
        pass

    # Box Drive (Phase 9, ADR-0019)
    try:
        import opshub.connectors.box_drive  # pyright: ignore[reportUnusedImport]
    except ImportError:
        pass

    # OneDrive Drive (Phase 11 F4-b, ADR-0019 §(j))
    try:
        import opshub.connectors.onedrive_drive  # pyright: ignore[reportUnusedImport]
    except ImportError:
        pass

    # Teams (Phase 11 F5)
    try:
        import opshub.connectors.teams  # pyright: ignore[reportUnusedImport]
    except ImportError:
        pass

    # Google Workspace (Phase 13)
    try:
        import opshub.connectors.google_workspace  # pyright: ignore[reportUnusedImport]
    except ImportError:
        pass

    # Google Calendar (Phase 14)
    try:
        import opshub.connectors.google_calendar  # pyright: ignore[reportUnusedImport]
    except ImportError:
        pass

    # Gmail (Phase 14)
    try:
        import opshub.connectors.google_mail  # noqa: F401  # pyright: ignore[reportUnusedImport]
    except ImportError:
        pass
