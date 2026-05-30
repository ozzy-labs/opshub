"""OneDrive (local-filesystem-backed) connector package (Phase 11 F4-b, ADR-0019 §(j)).

The OneDrive Drive connector is the second member of the
``local_drive`` family — Box Drive shipped in Phase 9 as the first.
ADR-0019 §(j) factors the shared local-FS contract so both connectors
inherit identical structural guarantees (``stat()``-only walk,
fingerprint diff, ADR-0019 §(b') opt-in content extraction hook,
identical safety caps). Only the connector identity (``connector_name``
/ ``source_type`` / ``actor`` / log-key prefix / setup-doc pointer)
differs between the two.

Importing this module triggers
``register_connector(OneDriveDriveConnector())`` so the CLI driver
(``opshub connector sync onedrive_drive``) discovers it through
:func:`opshub.connectors.discover_connectors`. Heavy dependencies
(:class:`OpsHubSettings`, SQLAlchemy ``sources_table``,
``markitdown`` when opted in) load lazily inside
:meth:`OneDriveDriveConnector.sync` so this side-effect import
stays within the ADR-0001 ~300ms cold-start budget.
"""

from __future__ import annotations

from opshub.connectors._registry import register_connector
from opshub.connectors.onedrive_drive.connector import OneDriveDriveConnector
from opshub.connectors.onedrive_drive.mapper import (
    DEFAULT_ACTOR,
    SOURCE_TYPE,
    SUMMARY_MAX_CHARS,
    map_scanned_file,
)
from opshub.connectors.onedrive_drive.scanner import (
    OneDriveDriveScanner,
    ScannedFile,
)

__all__ = [
    "DEFAULT_ACTOR",
    "SOURCE_TYPE",
    "SUMMARY_MAX_CHARS",
    "OneDriveDriveConnector",
    "OneDriveDriveScanner",
    "ScannedFile",
    "map_scanned_file",
]

# Register exactly once on first import. The registry's idempotency
# rule (re-registering the same instance is a no-op) makes this safe
# even when the package is imported via multiple paths within a single
# process; a different instance under the same name raises so a
# competing implementation cannot silently overwrite us.
register_connector(OneDriveDriveConnector())
