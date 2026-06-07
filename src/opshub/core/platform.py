r"""Host platform detection for filesystem-backed connectors (Phase 9, ADR-0019).

Phase 9 introduces the ``box_drive`` connector, which reads a Box Drive
desktop client mount point on the local filesystem rather than the Box
Platform API (ADR-0019 §決定 (a)). The choice of *where* Box Drive
mounts on a given host is platform-specific:

* **WSL2** — operator pre-configures ``mountvol B: \\?\Volume{GUID}\``
  + ``wsl --shutdown`` so ``/mnt/b`` becomes visible inside WSL2
  (ADR-0019 §決定 (h); the OS setup itself is documented in
  ``docs/box-drive-setup.md`` and is *not* automated by opshub).
* **macOS** — Box Drive installs under ``~/Box`` by default.
* **Linux native** — Box Drive provides no Linux client. The connector
  must fail fast with ``ConfigError`` (raised by the caller, not here)
  so the operator is directed to ``docs/box-drive-setup.md``.
* **Windows native / other** — opshub is POSIX-only
  (``pyproject.toml`` classifier ``Operating System :: POSIX``), so
  anything else is ``"unsupported"``.

This module is intentionally tiny: it must respect the ADR-0001 cold
start budget for ``opshub --help`` (≤300ms target), so its module-level
imports are restricted to ``__future__`` / ``pathlib`` / ``sys`` /
``typing`` (no third-party dependencies, no transitive ``sqlalchemy``
or ``pydantic`` pull-in via ``opshub.core.config``).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Final, Literal

Platform = Literal["wsl2", "macos", "linux", "unsupported"]
"""Discrete host-platform tag for FS-backed connector defaults.

* ``"wsl2"`` — Linux kernel with ``microsoft`` in ``osrelease`` (i.e.
  the kernel was built by Microsoft for WSL2). The box_drive connector
  defaults to ``/mnt/b`` here.
* ``"macos"`` — Darwin. Defaults to ``~/Box``.
* ``"linux"`` — Linux without WSL2 markers. No Box Drive client
  exists; callers should ``ConfigError`` when used as a Box Drive root.
* ``"unsupported"`` — Anything else (notably Windows native, BSDs).
  opshub is POSIX-only by ``pyproject.toml`` classifier, and box_drive
  has no Windows-native code path.
"""


def detect_platform() -> Platform:
    """Detect the host platform for FS-backed connector defaults.

    WSL2 detection uses ``/proc/sys/kernel/osrelease`` (rather than
    ``/proc/version``) because the file is shorter and its contents are
    deterministic: a WSL2 kernel always reports a release string
    containing ``microsoft`` (case-insensitive). Reading
    ``/proc/version`` would work too but pulls in extra noise (build
    timestamps, gcc version) that has nothing to do with the WSL2
    marker.

    Pure Linux (Linux without the ``microsoft`` marker) is reported as
    ``"linux"`` rather than ``"unsupported"`` so callers can distinguish
    "the host *can* run other things, just not Box Drive" from "we have
    no idea what this host is" (ADR-0019 §決定 (f)). The Box Drive
    connector turns ``"linux"`` into a ``ConfigError`` at use time by
    way of :func:`box_drive_default_root_path` returning ``None``.

    Returns
    -------
    Platform
        One of ``"wsl2"`` / ``"macos"`` / ``"linux"`` / ``"unsupported"``.
    """
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "linux":
        try:
            release = Path("/proc/sys/kernel/osrelease").read_text(errors="replace")
        except OSError:
            # /proc unavailable (e.g. sandboxed test env, restricted
            # container). Fall back to "linux" rather than "unsupported"
            # so the caller still gets a meaningful platform tag and
            # the ConfigError surfaces with a Linux-specific message.
            return "linux"
        if "microsoft" in release.lower():
            return "wsl2"
        return "linux"
    return "unsupported"


def box_drive_default_root_path(platform: Platform | None = None) -> Path | None:
    r"""Return the platform-default Box Drive root path, or ``None``.

    Defaults follow ADR-0019 §決定 (f):

    * ``"wsl2"`` → ``Path("/mnt/b")`` — assumes the operator has run
      ``mountvol B: \\?\Volume{GUID}\`` + ``wsl --shutdown`` on the
      Windows host so the Box Drive volume is visible inside WSL2.
      The Windows-side setup is *not* automated by opshub
      (ADR-0019 §決定 (h)); see ``docs/box-drive-setup.md``.
    * ``"macos"`` → ``Path.home() / "Box"`` — Box Drive's default
      install location on macOS.
    * ``"linux"`` / ``"unsupported"`` → ``None`` — Box Drive provides
      no Linux client, and Windows native is outside the POSIX-only
      project scope (``pyproject.toml`` classifier). Callers should
      turn ``None`` into a ``ConfigError`` that names
      ``docs/box-drive-setup.md`` (ADR-0019 §決定 (f) trailing note).

    Parameters
    ----------
    platform:
        Optional override for the detected platform. ``None`` (default)
        calls :func:`detect_platform` once. Tests pass an explicit value
        to pin behaviour without monkeypatching ``sys.platform``.

    Returns
    -------
    Path | None
        Default Box Drive root path, or ``None`` if no default exists
        for the platform.
    """
    if platform is None:
        platform = detect_platform()
    if platform == "wsl2":
        return Path("/mnt/b")
    if platform == "macos":
        return Path.home() / "Box"
    return None


def onedrive_drive_default_root_path(platform: Platform | None = None) -> Path | None:
    r"""Return the platform-default OneDrive root path, or ``None``.

    Defaults follow ADR-0019 §(j-2) Phase 11 改訂 (the same pattern
    helper :func:`box_drive_default_root_path` already uses, factored
    into a sibling so the two connectors share the platform-detect
    machinery but not a shared default that would surprise operators
    with one mounted client but not the other):

    * ``"wsl2"`` → ``Path("/mnt/onedrive")`` — assumes the operator
      has set up a Windows-side mount of the OneDrive folder (e.g.
      ``mklink /D`` from a known path to the OneDrive sync root, then
      mapped through ``mountvol`` / WSL2 mount config). The
      Windows-side setup is *not* automated by opshub; the connector
      surfaces a ``ConfigError`` with a pointer to
      ``docs/onedrive-drive-setup.md`` when the path is missing.
    * ``"macos"`` → ``Path.home() / "OneDrive"`` — OneDrive's default
      install location on macOS as documented by Microsoft.
    * ``"linux"`` / ``"unsupported"`` → ``None`` — Microsoft provides
      no OneDrive Linux client (the unofficial ``rclone`` mounts are
      out of scope for the default path detection). Callers turn
      ``None`` into a ``ConfigError`` with a pointer to
      ``docs/onedrive-drive-setup.md``.

    Parameters
    ----------
    platform:
        Optional override for the detected platform. ``None`` (default)
        calls :func:`detect_platform` once. Tests pass an explicit value
        to pin behaviour without monkeypatching ``sys.platform``.

    Returns
    -------
    Path | None
        Default OneDrive root path, or ``None`` if no default exists
        for the platform.
    """
    if platform is None:
        platform = detect_platform()
    if platform == "wsl2":
        return Path("/mnt/onedrive")
    if platform == "macos":
        return Path.home() / "OneDrive"
    return None


#: Name of the opshub-dedicated browser profile directory under the
#: data dir (Phase 21-B, ADR-0037 §決定 (c)). Module constant so the
#: browser core and any future caller agree on the spelling.
BROWSER_PROFILE_DIRNAME: Final[str] = "browser"


def browser_user_data_dir(data_dir: Path) -> Path:
    """Return the opshub-dedicated browser ``user-data-dir`` (ADR-0037 §決定 (c)).

    The browser read layer launches Chromium against an
    **opshub-owned** persistent profile under the data dir rather than
    the operator's everyday Chrome profile. ADR-0037 §決定 (c) pins two
    reasons:

    * **Isolation** — opshub never touches the operator's browsing
      cookies / extensions, and a crashed render can never corrupt the
      operator's real profile.
    * **Chrome 136+ constraint** — Chrome 136+ refuses a debug
      connection against the default profile and requires a dedicated
      ``user-data-dir``; keeping our own directory means the
      ``connect_over_cdp`` escape hatch (ADR-0037 §決定 (b)) stays
      compatible with that hardening.

    The directory is ``<data_dir>/browser`` — a sibling of the
    ``db`` / ``cache`` trees under
    :func:`opshub.core.config.default_data_dir`. The caller passes the
    resolved data dir (typically ``settings.data_dir``) so the function
    stays stdlib-only and import-cheap (ADR-0001 cold-start budget — this
    module must not pull in :mod:`opshub.core.config`'s pydantic graph).

    Parameters
    ----------
    data_dir:
        The resolved opshub data dir (``settings.data_dir`` /
        :func:`opshub.core.config.default_data_dir`).

    Returns
    -------
    Path
        ``<data_dir>/browser``. The path is **not** created here — the
        browser core / Playwright creates it on first launch.
    """
    return data_dir / BROWSER_PROFILE_DIRNAME
