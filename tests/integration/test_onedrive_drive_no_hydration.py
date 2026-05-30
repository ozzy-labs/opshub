"""CldAPI / File Provider Extension non-hydration contract test (Phase 11 F4-b, ADR-0019 §(j)).

Microsoft (CldAPI) and Apple (File Provider Extension) both document a
contract that ``os.stat()`` against a cloud-storage placeholder file
returns metadata *without* triggering hydration (i.e. without
downloading the body from the SaaS). The OneDrive desktop client uses
the exact same CldAPI primitives as the Box Drive client — so the
Phase 11 F4-b OneDrive scanner — which is ``stat()`` only by
construction (ADR-0019 §不変条件 (b), inherited from
:class:`BoxDriveScanner`) — must never cause a OneDrive placeholder to
materialise on disk.

This test mirrors the Phase 9 box_drive contract test verbatim, with
``OPSHUB_BOX_DRIVE_TEST_ROOT`` swapped for
``OPSHUB_ONEDRIVE_DRIVE_TEST_ROOT``. It is **opt-in** for the same
reason: a tmp_path filesystem has no placeholder semantics, and a CI
runner without OneDrive installed would always trivially pass even if
the contract were broken. Operators wiring this test into their setup
run::

    OPSHUB_ONEDRIVE_DRIVE_TEST_ROOT=/mnt/onedrive/some-subset uv run pytest \
        tests/integration/test_onedrive_drive_no_hydration.py

The default ``pytest`` invocation skips the test, so CI is unaffected.

What the test checks
--------------------

Per-file the scanner stat'd, we compare a stable platform-specific
"is this hydrated?" signal before and after the scan. The signals are
the same as for box_drive because both clients ride on CldAPI / FSE
on the same OS surfaces:

* **Linux / WSL2** — ``st_blocks`` proxy (placeholder = 0 blocks).
* **macOS** — xattr set (``com.apple.fileprovider`` /
  ``com.apple.metadata:kMDItemDownloadedDate``).

A failure indicates that either the OS contract under which Phase 11
F4-b was designed has shifted, or the OneDriveDriveScanner regressed
into a content read (e.g. inherited from a base-class change). Either
way the connector must be paused; the operator action mirrors
ADR-0019 §軽減策 #2.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

# Module-level env probe so the skip reason is evaluated once and is
# stable in pytest output across collection / execution.
ROOT_ENV = "OPSHUB_ONEDRIVE_DRIVE_TEST_ROOT"
_ENABLED_ROOT = os.environ.get(ROOT_ENV)


def _snapshot_attrs(path: Path) -> dict[str, Any]:
    """Capture the hydration-relevant attributes of ``path`` as a dict.

    See the box_drive variant
    (``tests/integration/test_box_drive_no_hydration.py``) for the
    rationale behind each key. The OneDrive surface uses identical
    CldAPI / FSE primitives so the same snapshot shape applies.
    """
    stat_result = path.stat()
    xattrs: tuple[str, ...]
    listxattr = getattr(os, "listxattr", None)
    if listxattr is None:
        xattrs = ()
    else:
        try:
            xattrs = tuple(sorted(listxattr(path)))
        except OSError:
            xattrs = ()
    return {
        "st_blocks": getattr(stat_result, "st_blocks", None),
        "st_size": stat_result.st_size,
        "xattrs": xattrs,
    }


@pytest.mark.skipif(
    not _ENABLED_ROOT,
    reason=(
        f"Set {ROOT_ENV}=<onedrive_drive_root> to enable (real OneDrive env "
        "required; CI default skips this contract test)."
    ),
)
@pytest.mark.skipif(
    sys.platform not in {"linux", "darwin"},
    reason="CldAPI / File Provider Extension only meaningful on Linux/WSL2 or macOS.",
)
def test_stat_does_not_hydrate_placeholder() -> None:
    """A full OneDrive scan must not change any file's hydration-relevant attributes.

    The test enumerates every file under ``OPSHUB_ONEDRIVE_DRIVE_TEST_ROOT``
    *before* the scan (using a cheap walk that only reads the
    attributes we plan to compare), runs the scanner end-to-end with
    its full diff-detection contract, then re-enumerates and asserts
    no per-file attribute changed.

    Implementation mirrors the box_drive contract test verbatim —
    both connectors share the inherited ``OneDriveDriveScanner.scan()``
    code path so the same per-file invariants apply.
    """
    root = Path(_ENABLED_ROOT) if _ENABLED_ROOT else None
    assert root is not None  # for type-checkers; skipif guarantees this
    assert root.exists(), f"{ROOT_ENV}={root!s} does not exist"
    assert root.is_dir(), f"{ROOT_ENV}={root!s} is not a directory"

    # Import lazily so a missing env keeps collection cheap.
    from opshub.connectors.onedrive_drive import OneDriveDriveScanner

    # 1. Pre-scan snapshot.
    pre: dict[Path, dict[str, Any]] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            file_path = Path(dirpath) / name
            try:
                pre[file_path] = _snapshot_attrs(file_path)
            except OSError:
                continue

    # 2. Run the scanner exactly once with an empty prior map so every
    #    file goes through the full ``stat()`` + fingerprint code path.
    scanner = OneDriveDriveScanner(root_path=root, max_files=10**7)
    list(scanner.scan(prior_fingerprints={}))

    # 3. Post-scan snapshot. Any per-file attribute change indicates a
    #    hydration event was triggered by the scan.
    diffs: list[str] = []
    for file_path, before in pre.items():
        if not file_path.exists():
            continue  # vanished; not the scanner's doing
        after = _snapshot_attrs(file_path)
        if before != after:
            diffs.append(f"{file_path}:\n  before={before}\n  after={after}")

    assert not diffs, (
        "OneDrive hydration contract appears broken: stat-only scan "
        "changed observable attributes on the following placeholders.\n"
        "(ADR-0019 §不変条件 (b) / §軽減策 #2 — quiesce OneDrive sync "
        "and re-run before raising an incident.)\n\n" + "\n".join(diffs)
    )
