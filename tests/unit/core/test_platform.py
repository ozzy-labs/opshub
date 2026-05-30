"""Unit tests for :mod:`opshub.core.platform` (Phase 9, ADR-0019 §決定 (f)).

The four detection paths (WSL2 / macOS / Linux native / unsupported)
and the four ``box_drive_default_root_path`` paths are pinned here so
the Phase 9 ``BoxDriveConnector`` (step B2) can rely on stable defaults
on every host without re-deriving them.

The tests deliberately use ``monkeypatch`` against ``sys.platform`` and
``pathlib.Path.read_text`` rather than spinning up a real WSL2 / macOS
VM. The detection logic is a thin wrapper over those two inputs, so
mocking them exercises the full decision tree without introducing OS
fixtures.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from opshub.core.platform import (
    box_drive_default_root_path,
    detect_platform,
    onedrive_drive_default_root_path,
)

# ---------------------------------------------------------------------------
# detect_platform()
# ---------------------------------------------------------------------------


def test_detect_platform_returns_macos_on_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    """``sys.platform == 'darwin'`` shortcuts to ``'macos'`` without touching /proc.

    On macOS there is no ``/proc/sys/kernel/osrelease`` — the Darwin
    branch must return before the Linux probe runs. We assert that by
    leaving the Linux probe unpatched: any ``read_text`` call would
    raise ``FileNotFoundError`` on a real Darwin host, so a regression
    that hits the probe would surface visibly.
    """
    monkeypatch.setattr("opshub.core.platform.sys.platform", "darwin")

    assert detect_platform() == "macos"


def _fake_read_text(content: str) -> Any:
    """Build a typed stand-in for ``Path.read_text`` that returns ``content``.

    pyright (strict) wants concrete annotations on the lambda we pass to
    ``monkeypatch.setattr``; using ``Any`` returns keeps the helper
    untyped from pyright's perspective while still being correct for
    the runtime substitution (``Path.read_text(self, encoding=..., errors=...)``).
    """

    def _impl(self: Path, *args: Any, **kwargs: Any) -> str:
        return content

    return _impl


def test_detect_platform_returns_wsl2_when_microsoft_in_osrelease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Linux kernel whose ``osrelease`` contains 'microsoft' is WSL2.

    The string match is intentionally case-insensitive in the
    implementation (Microsoft has used both ``microsoft`` and
    ``Microsoft`` in real release strings over WSL2's history).
    """
    monkeypatch.setattr("opshub.core.platform.sys.platform", "linux")
    monkeypatch.setattr(
        Path,
        "read_text",
        _fake_read_text("5.15.167.4-microsoft-standard-WSL2\n"),
    )

    assert detect_platform() == "wsl2"


def test_detect_platform_returns_wsl2_for_capitalised_microsoft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capitalised 'Microsoft' in osrelease still detects as WSL2.

    Real WSL2 kernels have shipped with both casings; the detector must
    not regress to case-sensitive matching.
    """
    monkeypatch.setattr("opshub.core.platform.sys.platform", "linux")
    monkeypatch.setattr(
        Path,
        "read_text",
        _fake_read_text("4.19.0-Microsoft-WSL2\n"),
    )

    assert detect_platform() == "wsl2"


def test_detect_platform_returns_linux_for_pure_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Linux host without the 'microsoft' marker reports ``'linux'``.

    Pure Linux is meaningful (the box_drive connector turns it into a
    ``ConfigError`` at use time), so the detector reports it distinctly
    from ``'unsupported'``.
    """
    monkeypatch.setattr("opshub.core.platform.sys.platform", "linux")
    monkeypatch.setattr(
        Path,
        "read_text",
        _fake_read_text("6.1.0-amd64\n"),
    )

    assert detect_platform() == "linux"


def test_detect_platform_falls_back_to_linux_when_proc_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``OSError`` from /proc still resolves to ``'linux'``.

    Sandboxed test environments / restricted containers may hide
    ``/proc/sys/kernel/osrelease``. We treat that as "Linux without
    WSL2 markers" rather than ``'unsupported'`` so the box_drive
    connector can produce a Linux-specific error message.
    """
    monkeypatch.setattr("opshub.core.platform.sys.platform", "linux")

    def boom(self: Path, *args: Any, **kwargs: Any) -> str:
        raise OSError("simulated /proc denial")

    monkeypatch.setattr(Path, "read_text", boom)

    assert detect_platform() == "linux"


def test_detect_platform_returns_unsupported_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows native (``sys.platform == 'win32'``) is unsupported.

    opshub's ``pyproject.toml`` classifier pins ``Operating System ::
    POSIX``; Windows native is out of scope for both the box_drive
    connector and the project as a whole.
    """
    monkeypatch.setattr("opshub.core.platform.sys.platform", "win32")

    assert detect_platform() == "unsupported"


def test_detect_platform_returns_unsupported_on_bsd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BSD / other POSIX flavours that are neither macOS nor Linux are unsupported.

    ``sys.platform`` reports ``'freebsd13'`` / ``'openbsd7'`` etc. on
    BSDs. They share the POSIX classifier but Box Drive does not run
    there, so we report ``'unsupported'`` for clarity.
    """
    monkeypatch.setattr("opshub.core.platform.sys.platform", "freebsd13")

    assert detect_platform() == "unsupported"


# ---------------------------------------------------------------------------
# box_drive_default_root_path()
# ---------------------------------------------------------------------------


def test_default_root_path_wsl2_is_mnt_b() -> None:
    """WSL2 default is ``/mnt/b`` (operator pre-runs mountvol)."""
    assert box_drive_default_root_path("wsl2") == Path("/mnt/b")


def test_default_root_path_macos_expands_to_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS default expands ``~`` to the operator's home directory.

    We monkeypatch ``Path.home`` so the assertion does not depend on
    the test runner's actual ``$HOME`` value.
    """
    fake_home = Path("/Users/test-operator")

    def _fake_home(cls: type[Path]) -> Path:
        return fake_home

    monkeypatch.setattr(Path, "home", classmethod(_fake_home))

    assert box_drive_default_root_path("macos") == fake_home / "Box"


def test_default_root_path_linux_is_none() -> None:
    """Linux native returns ``None`` — Box Drive provides no Linux client."""
    assert box_drive_default_root_path("linux") is None


def test_default_root_path_unsupported_is_none() -> None:
    """Unsupported platforms return ``None`` rather than raising.

    The caller (the Phase 9 step B2 ``BoxDriveConnector``) turns
    ``None`` into a ``ConfigError`` with operator-actionable text;
    raising here would force every caller to re-catch the same error.
    """
    assert box_drive_default_root_path("unsupported") is None


def test_default_root_path_calls_detect_platform_when_arg_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling without an explicit platform delegates to ``detect_platform()``.

    This is the production code path — callers normally do not pass
    an explicit platform. We verify by monkeypatching the module-level
    detector so the default path is observable.
    """

    def _fake_detect() -> str:
        return "macos"

    monkeypatch.setattr("opshub.core.platform.detect_platform", _fake_detect)
    fake_home = Path("/Users/test-operator")

    def _fake_home(cls: type[Path]) -> Path:
        return fake_home

    monkeypatch.setattr(Path, "home", classmethod(_fake_home))

    assert box_drive_default_root_path() == fake_home / "Box"


# ---------------------------------------------------------------------------
# onedrive_drive_default_root_path() — Phase 11 F4-b (ADR-0019 §(j-2))
# ---------------------------------------------------------------------------


def test_onedrive_default_root_path_wsl2_is_mnt_onedrive() -> None:
    """WSL2 default is ``/mnt/onedrive`` (operator pre-mounts the OneDrive sync root)."""
    assert onedrive_drive_default_root_path("wsl2") == Path("/mnt/onedrive")


def test_onedrive_default_root_path_macos_expands_to_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """macOS default expands ``~`` to ``~/OneDrive``.

    Monkeypatching ``Path.home`` keeps the assertion independent of
    the test runner's ``$HOME`` value (mirrors the box_drive test).
    """
    fake_home = Path("/Users/test-operator")

    def _fake_home(cls: type[Path]) -> Path:
        return fake_home

    monkeypatch.setattr(Path, "home", classmethod(_fake_home))

    assert onedrive_drive_default_root_path("macos") == fake_home / "OneDrive"


def test_onedrive_default_root_path_linux_is_none() -> None:
    """Linux native → ``None`` (Microsoft provides no OneDrive Linux client)."""
    assert onedrive_drive_default_root_path("linux") is None


def test_onedrive_default_root_path_unsupported_is_none() -> None:
    """Unsupported platforms return ``None`` rather than raising.

    The caller (the F4-b ``OneDriveDriveConnector``) turns ``None``
    into a :class:`ConfigError` with operator-actionable text;
    raising here would force every caller to re-catch the same
    error.
    """
    assert onedrive_drive_default_root_path("unsupported") is None


def test_onedrive_default_root_path_calls_detect_platform_when_arg_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling without an explicit platform delegates to ``detect_platform()``."""

    def _fake_detect() -> str:
        return "macos"

    monkeypatch.setattr("opshub.core.platform.detect_platform", _fake_detect)
    fake_home = Path("/Users/test-operator")

    def _fake_home(cls: type[Path]) -> Path:
        return fake_home

    monkeypatch.setattr(Path, "home", classmethod(_fake_home))

    assert onedrive_drive_default_root_path() == fake_home / "OneDrive"


def test_onedrive_default_root_path_does_not_collide_with_box_drive() -> None:
    """The two local-FS connectors yield distinct platform defaults.

    Pinning the non-collision protects against a future refactor that
    accidentally folds the two helpers into a single shared function
    returning ``/mnt/b`` for both — which would silently break
    operators running both connectors side-by-side.
    """
    assert box_drive_default_root_path("wsl2") != onedrive_drive_default_root_path("wsl2")
    assert box_drive_default_root_path("macos") != onedrive_drive_default_root_path("macos")
