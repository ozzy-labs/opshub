"""OneDrive (local-FS-backed) scanner — pure FS walker, stat-only (Phase 11 F4-b, ADR-0019 §(j)).

This module is the second member of the ``local_drive`` family (Box
Drive shipped in Phase 9 as the first member; ADR-0019 §(j) factors
the shared contract). The walk logic is inherited verbatim from
:class:`opshub.connectors.box_drive.scanner.BoxDriveScanner` —
identical structural caps, identical Office content-extraction hook,
identical ``f"{size}:{mtime_ns}"`` fingerprint diff token, identical
ADR-0019 §不変条件 (b) no-``open()`` invariant on the default-off
path. The subclass overrides only three class-level seams (log key
prefix, human-readable client name, setup-doc reference) so the
runtime behaviour stays bit-for-bit identical to box_drive's
proven contract while keeping the two connectors' logs / errors
distinguishable.

Why a subclass rather than a sibling implementation
---------------------------------------------------

Duplicating ~500 lines of walk code would let the two scanners drift
on every subsequent ADR-0019 refinement (and the Phase 11 audit
flagged exactly this kind of drift risk for the broader connector
family). The subclass-with-class-vars pattern keeps the implementation
DRY without introducing a runtime polymorphism cost — the class-vars
resolve at MRO lookup time, no per-walk indirection. The trade-off is
that a future change to the walk loop in the base class
**must** be considered against both connectors' contract suites; the
shared test patterns in
``tests/unit/connectors/onedrive_drive/test_scanner.py`` make that a
mechanical check.

Identity / fingerprint / removal semantics carry over unchanged
(ADR-0019 §決定 (c)(d)(e)). The ``content_extraction = True`` opt-in
honours the same ADR-0019 §(b') narrow open() exception for
``.docx`` / ``.xlsx`` / ``.pptx`` (and legacy ``.doc`` / ``.xls`` /
``.ppt``) via :func:`opshub.core.document_extract.extract_document`,
again identical to box_drive.
"""

from __future__ import annotations

from typing import ClassVar

from opshub.connectors.box_drive.scanner import BoxDriveScanner, ScannedFile

__all__ = ["OneDriveDriveScanner", "ScannedFile"]


class OneDriveDriveScanner(BoxDriveScanner):
    """Recursive, stat-only walker for a OneDrive sync folder.

    Inherits all walk logic from
    :class:`opshub.connectors.box_drive.scanner.BoxDriveScanner`.
    The three class-level overrides re-namespace the structured-log
    keys and :class:`ConfigError` messages so operators can tell the
    two connectors apart in logs without a per-walk indirection cost.

    Constructor signature, ``scan()`` contract, ``ScannedFile`` shape
    (including the Phase 11 F4 ``body`` / ``office_source_type`` /
    ``body_truncated`` / ``body_skip_reason`` fields), and exclusion
    glob semantics are inherited verbatim.
    """

    #: Structured-log key prefix: ``"onedrive_drive.scan_*"`` (vs
    #: box_drive's ``"box_drive.scan_*"``). Lets ``opshub query`` /
    #: ``grep`` separate the two connectors in unified logs.
    _log_prefix: ClassVar[str] = "onedrive_drive"
    #: Human-readable client name. Embedded in :class:`ConfigError`
    #: messages so the operator's terminal output names the right
    #: client (``"OneDrive root_path does not exist ..."``).
    _client_name: ClassVar[str] = "OneDrive"
    #: Setup doc pointer for the operator-facing error message.
    _setup_doc: ClassVar[str] = "docs/onedrive-drive-setup.md"
