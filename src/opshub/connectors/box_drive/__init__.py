"""Box Drive (local-filesystem-backed) connector package (Phase 9, ADR-0019).

Phase 9 step B1 ships the *scanner* half of the connector: a pure
``os.scandir``-based walker that reads only stat metadata from a Box
Drive mount point and yields :class:`ScannedFile` records with a
``fingerprint = f"{size}:{mtime_ns}"`` identity. The scanner is
deliberately decoupled from the eventual :class:`Connector` Protocol
implementation so it can be unit-tested in isolation (and so the
``open()`` / ``read()`` ban from ADR-0019 §決定 (b) can be enforced via
``monkeypatch`` without dragging service-layer dependencies into the
test).

Subsequent Phase 9 PRs (B2 / C1) add the mapper, the
:class:`Connector` implementation, settings glue, and the registry
side-effect import. This ``__init__.py`` therefore stays minimal —
re-exporting the public symbols of :mod:`scanner` only — so the
``opshub --help`` cold-start budget (ADR-0001) is unaffected and the
``connectors._registry`` discovery sweep does not eagerly import a
half-finished connector.
"""

from __future__ import annotations

from opshub.connectors.box_drive.scanner import BoxDriveScanner, ScannedFile

__all__ = ["BoxDriveScanner", "ScannedFile"]
