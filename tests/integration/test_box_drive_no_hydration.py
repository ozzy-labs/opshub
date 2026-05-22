"""CldAPI / File Provider Extension non-hydration contract test (Phase 9, ADR-0019).

Microsoft (CldAPI) and Apple (File Provider Extension) both document a
contract that ``os.stat()`` against a cloud-storage placeholder file
returns metadata *without* triggering hydration (i.e. without
downloading the body from the SaaS). The Box Drive desktop client
relies on those primitives, so the Phase 9 scanner — which is
``stat()`` only by construction (ADR-0019 §決定 (b)) — should never
cause a placeholder to materialise on disk.

We assert that contract here. The test is **opt-in** because it
requires a real Box Drive environment to be meaningful: a tmp_path
filesystem has no placeholder semantics, and a CI runner without Box
Drive installed would always trivially pass even if the contract were
broken. Operators wiring this test into their setup run::

    OPSHUB_BOX_DRIVE_TEST_ROOT=/mnt/b/some-subset uv run pytest \
        tests/integration/test_box_drive_no_hydration.py

The default ``pytest`` invocation skips the test, so CI is unaffected.

What the test checks
--------------------

Per-file the scanner stat'd, we compare a stable platform-specific
"is this hydrated?" signal before and after the scan:

* **Linux / WSL2** — the file's xattr namespace under ``user.*`` does
  not gain a hydration marker, and ``st_blocks`` (the on-disk block
  count) does not transition from 0 to a non-zero value. Microsoft's
  CldAPI sets ``FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS`` on placeholders;
  the WSL2 view does not expose that flag directly, so we use
  ``st_blocks`` as the proxy ("placeholder = 0 blocks on disk").
* **macOS** — placeholder files report ``com.apple.fileprovider`` /
  ``com.apple.metadata:kMDItemDownloadedDate`` xattrs. The marker
  shape varies by macOS version; we record the full xattr set before
  the scan and assert it is unchanged afterwards.

Because the marker shape is platform- and vendor-specific, the test
captures a *snapshot* of the relevant attributes per file before the
scan and re-checks them afterwards. Any change is treated as a
hydration trigger and fails the test with a diff message that
identifies the culprit file.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

# Module-level env probe so the skip reason is evaluated once and is
# stable in pytest output across collection / execution.
ROOT_ENV = "OPSHUB_BOX_DRIVE_TEST_ROOT"
_ENABLED_ROOT = os.environ.get(ROOT_ENV)


def _snapshot_attrs(path: Path) -> dict[str, Any]:
    """Capture the hydration-relevant attributes of ``path`` as a dict.

    Returns a small dict that downstream code can compare equality on.
    The set of keys differs by platform because CldAPI and File Provider
    Extension expose hydration state through different surfaces:

    * ``"st_blocks"`` — on-disk block count. Drops to 0 for unhydrated
      placeholders on most filesystems; an increase after the scan
      indicates the body was downloaded.
    * ``"xattrs"`` — sorted tuple of extended-attribute names. Apple's
      File Provider Extension manipulates these on hydration; the
      Microsoft CldAPI Linux view occasionally surfaces them too.
    """
    stat_result = path.stat()
    xattrs: tuple[str, ...]
    # ``os.listxattr`` only exists on Linux / macOS; on other platforms
    # the contract test would not be meaningful anyway, so we just skip
    # the xattr capture. Resolve through ``getattr`` so type-checkers
    # do not have to special-case the Windows-only build of ``os``.
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
        f"Set {ROOT_ENV}=<box_drive_root> to enable (real Box Drive env "
        "required; CI default skips this contract test)."
    ),
)
@pytest.mark.skipif(
    sys.platform not in {"linux", "darwin"},
    reason="CldAPI / File Provider Extension only meaningful on Linux/WSL2 or macOS.",
)
def test_stat_does_not_hydrate_placeholder() -> None:
    """A full scan must not change any file's hydration-relevant attributes.

    The test enumerates every file under ``OPSHUB_BOX_DRIVE_TEST_ROOT``
    *before* the scan (using a cheap walk that only reads the
    attributes we plan to compare), runs the scanner end-to-end with
    its full diff-detection contract, then re-enumerates and asserts
    no per-file attribute changed.

    A failure indicates the OS contract under which Phase 9 was
    designed has shifted (or the scanner regressed into a content read
    elsewhere). Either way the connector must be paused; ADR-0019
    §軽減策 #2 makes this a CI-grade signal in real env.

    The "no change" assertion is conservative: it would also flag a
    file that the OS hydrated *spontaneously* between the two
    snapshots (background sync). Operators running the test should
    quiesce Box Drive sync briefly (or run it twice and look for stable
    failures) before raising an incident.
    """
    # ``_ENABLED_ROOT`` is non-None here because the skipif above
    # short-circuits when it is falsy.
    root = Path(_ENABLED_ROOT) if _ENABLED_ROOT else None
    assert root is not None  # for type-checkers; skipif guarantees this
    assert root.exists(), f"{ROOT_ENV}={root!s} does not exist"
    assert root.is_dir(), f"{ROOT_ENV}={root!s} is not a directory"

    # Import lazily so a missing env keeps collection cheap.
    from opshub.connectors.box_drive import BoxDriveScanner

    # 1. Pre-scan snapshot. Walk independently of the scanner so we
    #    are not measuring the scanner against itself.
    pre: dict[Path, dict[str, Any]] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            file_path = Path(dirpath) / name
            try:
                pre[file_path] = _snapshot_attrs(file_path)
            except OSError:
                # File vanished between listing and stat — acceptable
                # in a live Box Drive workspace; just skip.
                continue

    # 2. Run the scanner exactly once. We pass an empty prior map so
    #    every file goes through the full ``stat()`` + fingerprint
    #    code path — this is the worst case for hydration triggers.
    scanner = BoxDriveScanner(root_path=root, max_files=10**7)
    # Materialise the iterator so the full walk happens before
    # snapshot 2.
    list(scanner.scan(prior_fingerprints={}))

    # 3. Post-scan snapshot. Any per-file attribute change indicates
    #    a hydration event was triggered by the scan.
    diffs: list[str] = []
    for file_path, before in pre.items():
        if not file_path.exists():
            continue  # vanished; not the scanner's doing
        after = _snapshot_attrs(file_path)
        if before != after:
            diffs.append(f"{file_path}:\n  before={before}\n  after={after}")

    assert not diffs, (
        "Box Drive hydration contract appears broken: stat-only scan "
        "changed observable attributes on the following placeholders.\n"
        "(ADR-0019 §決定 (b) / §軽減策 #2 — quiesce Box Drive sync and "
        "re-run before raising an incident.)\n\n" + "\n".join(diffs)
    )
