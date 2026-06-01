"""Unit tests for the CLI progress helpers (issue #316).

Covers the enable/disable decision (flag > env var > stderr-TTY), the
no-op fallback that keeps non-TTY / ``--no-progress`` output clean, and
the rich-backed reporters' counting behaviour for both the indeterminate
(spinner) and determinate (bar) shapes.

The module's internals (``_enabled`` / ``_NoOpReporter`` / ``_RichReporter``)
are intentionally private; this test probes them directly, so they are
aliased once at module scope with a single ``reportPrivateUsage`` waiver
each rather than peppering every assertion with one.
"""

from __future__ import annotations

import io
from collections.abc import Iterator

import pytest
from rich.console import Console
from rich.progress import Task

from opshub.cli import _progress

# Private-surface aliases (one waiver each instead of one per call site).
_enabled = _progress._enabled  # pyright: ignore[reportPrivateUsage]
_NoOpReporter = _progress._NoOpReporter  # pyright: ignore[reportPrivateUsage]
_RichReporter = _progress._RichReporter  # pyright: ignore[reportPrivateUsage]


def _first_task(reporter: _progress._RichReporter) -> Task:  # pyright: ignore[reportPrivateUsage]
    """The single rich task driven by ``reporter`` (probes a protected attr)."""
    return reporter._progress.tasks[0]  # pyright: ignore[reportPrivateUsage]


@pytest.fixture(autouse=True)
def _reset_preference() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Isolate the module-global preference between tests."""
    _progress.set_preference(None)
    yield
    _progress.set_preference(None)


def _forced_terminal_console() -> Console:
    """A rich Console that renders to an in-memory buffer as if a TTY."""
    return Console(file=io.StringIO(), force_terminal=True, width=80)


def test_disabled_when_stderr_not_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_progress.sys.stderr, "isatty", lambda: False)
    monkeypatch.delenv("OPSHUB_PROGRESS", raising=False)
    assert _enabled() is False


def test_enabled_when_stderr_is_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_progress.sys.stderr, "isatty", lambda: True)
    monkeypatch.delenv("OPSHUB_PROGRESS", raising=False)
    assert _enabled() is True


def test_flag_overrides_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPSHUB_PROGRESS", raising=False)
    # --no-progress wins over a TTY.
    monkeypatch.setattr(_progress.sys.stderr, "isatty", lambda: True)
    _progress.set_preference(False)
    assert _enabled() is False
    # --progress wins over a non-TTY.
    monkeypatch.setattr(_progress.sys.stderr, "isatty", lambda: False)
    _progress.set_preference(True)
    assert _enabled() is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("on", True),
        ("YES", True),
        ("0", False),
        ("false", False),
        ("off", False),
    ],
)
def test_env_var_overrides_tty(monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
    # Set the TTY to the *opposite* of the expected outcome so a pass
    # proves the env var (not the TTY) decided.
    monkeypatch.setattr(_progress.sys.stderr, "isatty", lambda: not expected)
    monkeypatch.setenv("OPSHUB_PROGRESS", value)
    assert _enabled() is expected


def test_env_var_ignored_when_flag_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSHUB_PROGRESS", "1")
    _progress.set_preference(False)
    assert _enabled() is False


def test_indeterminate_is_noop_when_disabled() -> None:
    _progress.set_preference(False)
    with _progress.indeterminate("syncing slack") as reporter:
        reporter.advance(3)
        reporter.update(description="still syncing")
    assert isinstance(reporter, _NoOpReporter)


def test_determinate_is_noop_when_disabled() -> None:
    _progress.set_preference(False)
    with _progress.determinate(10, "rebuilding") as reporter:
        reporter.advance()
        reporter.update(total=20)
    assert isinstance(reporter, _NoOpReporter)


def test_indeterminate_counts_advances_when_enabled() -> None:
    _progress.set_preference(True)
    console = _forced_terminal_console()
    with _progress.indeterminate("syncing", console=console) as reporter:
        assert isinstance(reporter, _RichReporter)
        reporter.advance(2)
        reporter.advance(1)
        task = _first_task(reporter)
        assert task.completed == 3
        # Unknown total ⇒ no percentage / finished flag.
        assert task.total is None


def test_determinate_tracks_total_and_completion() -> None:
    _progress.set_preference(True)
    console = _forced_terminal_console()
    with _progress.determinate(4, "rebuilding", console=console) as reporter:
        assert isinstance(reporter, _RichReporter)
        reporter.advance(4)
        task = _first_task(reporter)
        assert task.total == 4
        assert task.completed == 4
        assert task.finished


def test_rich_reporter_update_changes_total() -> None:
    _progress.set_preference(True)
    console = _forced_terminal_console()
    with _progress.determinate(1, "rebuilding", console=console) as reporter:
        assert isinstance(reporter, _RichReporter)
        reporter.update(total=10, description="rebuilding tasks")
        task = _first_task(reporter)
        assert task.total == 10
        assert task.description == "rebuilding tasks"
