"""Box Drive (local-filesystem-backed) connector package (Phase 9, ADR-0019).

Phase 9 step B1 shipped the *scanner* half of the connector: a pure
``os.scandir``-based walker that reads only stat metadata from a Box
Drive mount point and yields :class:`ScannedFile` records with a
``fingerprint = f"{size}:{mtime_ns}"`` identity. Phase 9 step B2
(this PR) adds the :func:`map_scanned_file` mapper, the
:class:`BoxDriveConnector` Protocol implementation, and registers the
connector with the process-wide registry as an import side effect —
mirroring the Phase 3 GitHub + Phase 7 Slack / MS365 / Box precedents.

Importing this module triggers ``register_connector(BoxDriveConnector())``
so the CLI driver (``opshub connector sync box_drive``) can discover
the connector through :func:`opshub.connectors.discover_connectors`.
Heavy dependencies (:class:`OpsHubSettings`,
:func:`box_drive_default_root_path`, SQLAlchemy ``sources_table``)
are deferred until :meth:`BoxDriveConnector.sync` runs, so this
side-effect import stays within the ADR-0001 ~300 ms cold-start
budget.
"""

from __future__ import annotations

from opshub.connectors._registry import register_connector
from opshub.connectors.box_drive.connector import BoxDriveConnector
from opshub.connectors.box_drive.mapper import (
    DEFAULT_ACTOR,
    SOURCE_TYPE,
    SUMMARY_MAX_CHARS,
    map_scanned_file,
)
from opshub.connectors.box_drive.scanner import BoxDriveScanner, ScannedFile

__all__ = [
    "DEFAULT_ACTOR",
    "SOURCE_TYPE",
    "SUMMARY_MAX_CHARS",
    "BoxDriveConnector",
    "BoxDriveScanner",
    "ScannedFile",
    "map_scanned_file",
]

# Register exactly once on first import. The registry's idempotency
# rule (registering the *same* instance twice is a no-op) makes this
# safe even when the package is imported through multiple paths within
# a single process; a *different* instance under the same name would
# raise — which is the guard we want against an accidental double-class
# refactor.
register_connector(BoxDriveConnector())
